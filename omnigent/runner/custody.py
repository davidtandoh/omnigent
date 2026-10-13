"""Stable session ownership with replaceable, exclusively claimed host attachments."""

from __future__ import annotations

import hashlib
import itertools
import os
import stat
import subprocess
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Literal

import psutil
from filelock import FileLock, Timeout
from pydantic import BaseModel, ConfigDict, Field

from omnigent._platform import IS_DARWIN
from omnigent.host.daemon_lifecycle import record_flock_is_held
from omnigent.native.launch_environment import native_launch_environment_digest
from omnigent.native.owner_claim import ProcessIdentity, current_boot_id, current_pid_namespace
from omnigent.native.state_files import canonical_json, read_regular_file, write_state_file

CUSTODY_ENV_VAR = "OMNIGENT_SESSION_CUSTODY"
# Admit each harness/platform only after its real native survival proof.
SUPPORTED_HARNESSES = frozenset({"codex-native"}) if IS_DARWIN else frozenset()


class CustodyError(ValueError):
    """A refusal category; no resource is signalled or deleted on refusal."""


class ConfigFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str
    digest: str


class TerminalReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str
    session_key: str
    socket: str
    device: int
    inode: int
    tmux: ProcessIdentity
    pane: ProcessIdentity


class CustodyContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    session_id: str = Field(min_length=1)
    runner_id: str = Field(min_length=1)
    harness: Literal["codex-native"]
    host_id: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    workspace: str
    config_digest: str
    environment_digest: str
    owner_runtime_generation: str
    config_files: list[ConfigFile]


class CustodyRecord(CustodyContext):
    schema_version: Literal[1] = 1
    owner_generation: str
    owner: ProcessIdentity
    state: Literal["starting", "live", "ended", "transferred"] = "starting"
    terminals: list[TerminalReceipt] = Field(default_factory=list)
    helpers: dict[str, ProcessIdentity] = Field(default_factory=dict)
    resources: dict[str, ConfigFile] = Field(default_factory=dict)


class SessionBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    session_id: str
    runner_id: str
    harness: Literal["codex-native"]
    owner_generation: str
    host_id: str
    endpoint: str
    workspace: str
    config_digest: str
    environment_digest: str
    owner: ProcessIdentity
    native: ProcessIdentity
    terminal: TerminalReceipt


class AttachmentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    generation: str
    process: ProcessIdentity
    epoch: int = Field(ge=1)
    state: Literal["attached", "detached"]
    owner_generation: str


def custody_session_key(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()


def _session_dir(base: Path, session_id: str, *, create: bool = False) -> Path:
    key = custody_session_key(session_id)
    root = base.resolve() / "custody"
    for directory in (root, root / key):
        if create:
            try:
                directory.mkdir(mode=0o700)
            except FileExistsError:
                if directory != root and not (directory / "session.json").exists():
                    raise CustodyError("record_mismatch") from None
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise CustodyError("record_mismatch")
    return root / key


def _read_record(path: Path) -> CustodyRecord:
    try:
        return CustodyRecord.model_validate_json(read_regular_file(path, limit=256 * 1024))
    except (OSError, ValueError) as exc:
        raise CustodyError("record_mismatch") from exc


def _lock(path: Path) -> FileLock:
    if path.is_symlink():
        raise CustodyError("record_mismatch")
    lock = FileLock(path, mode=0o600, thread_local=False)
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        raise CustodyError("owner_busy") from exc
    return lock


def _require_live(identity: ProcessIdentity) -> psutil.Process:
    try:
        return identity.require_live()
    except (ValueError, psutil.Error) as exc:
        raise CustodyError("record_mismatch") from exc


def verify_resources(record: CustodyRecord) -> None:
    """Verify recorded sockets/processes/config without taking cleanup authority."""
    if record.state == "transferred":
        raise CustodyError("scope_mismatch")
    _require_live(record.owner)
    for config in [*record.config_files, *record.resources.values()]:
        try:
            digest = hashlib.sha256(
                read_regular_file(Path(config.path), limit=4 * 1024 * 1024)
            ).hexdigest()
        except (OSError, ValueError) as exc:
            raise CustodyError("config_mismatch") from exc
        if digest != config.digest:
            raise CustodyError("config_mismatch")
    for terminal in record.terminals:
        try:
            info = Path(terminal.socket).lstat()
        except OSError as exc:
            raise CustodyError("foreign_socket") from exc
        if not stat.S_ISSOCK(info.st_mode) or (info.st_dev, info.st_ino) != (
            terminal.device,
            terminal.inode,
        ):
            raise CustodyError("foreign_socket")
        _require_live(terminal.tmux)
        _require_live(terminal.pane)
    for helper in record.helpers.values():
        _require_live(helper)


class SessionCustody:
    """Runner-owned lease; host death cannot release this resource owner."""

    def __init__(self, directory: Path, record: CustodyRecord, lock: FileLock) -> None:
        self.directory = directory
        self.record_path = directory / "session.json"
        self.record = record
        self._lock = lock
        self._updates = threading.RLock()
        info = Path(lock.lock_file).lstat()
        self._lock_identity = (info.st_dev, info.st_ino)
        self._context = CustodyContext.model_validate(
            record.model_dump(include=set(CustodyContext.model_fields))
        )

    @classmethod
    def create(cls, base: Path, context: dict[str, object]) -> SessionCustody:
        try:
            parsed = CustodyContext.model_validate(context)
        except ValueError as exc:
            raise CustodyError("unsupported_custody") from exc
        directory = _session_dir(base, parsed.session_id, create=True)
        path = directory / "session.json"
        if path.exists() or path.is_symlink():
            _read_record(path)
        lock = _lock(directory / "owner.lock")
        try:
            if path.exists() or path.is_symlink():
                previous = _read_record(path)
                if previous.state != "ended":
                    raise CustodyError("stale_attachment")
            record = CustodyRecord(
                **parsed.model_dump(),
                owner_generation=uuid.uuid4().hex,
                owner=ProcessIdentity.capture(os.getpid()),
            )
            write_state_file(path, record.model_dump(mode="json"))
            return cls(directory, record, lock)
        except BaseException:
            lock.release()
            raise

    def __enter__(self) -> SessionCustody:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.finish()

    def still_owner(self) -> bool:
        with self._updates:
            try:
                current = _read_record(self.record_path)
                info = Path(self._lock.lock_file).lstat()
                return (
                    self._lock.is_locked
                    and (info.st_dev, info.st_ino) == self._lock_identity
                    and CustodyContext.model_validate(
                        current.model_dump(include=set(CustodyContext.model_fields))
                    )
                    == self._context
                    and current.owner == self.record.owner
                    and current.owner_generation == self.record.owner_generation
                    and current.state not in {"ended", "transferred"}
                )
            except (CustodyError, OSError, ValueError):
                return False

    def _publish(self) -> None:
        if not self.still_owner():
            raise CustodyError("record_mismatch")
        write_state_file(self.record_path, self.record.model_dump(mode="json"))

    def publish_terminal(
        self, name: str, session_key: str, socket: Path, tmux_pid: int, pane_pid: int
    ) -> None:
        info = socket.lstat()
        if not stat.S_ISSOCK(info.st_mode):
            raise CustodyError("foreign_socket")
        receipt = TerminalReceipt(
            name=name,
            session_key=session_key,
            socket=str(socket),
            device=info.st_dev,
            inode=info.st_ino,
            tmux=ProcessIdentity.capture(tmux_pid),
            pane=ProcessIdentity.capture(pane_pid),
        )
        with self._updates:
            self.record.terminals = [
                r for r in self.record.terminals if (r.name, r.session_key) != (name, session_key)
            ] + [receipt]
            self.record.state = "live"
            self._publish()

    def publish_helper(self, name: str, pid: int) -> None:
        with self._updates:
            self.record.helpers[name] = ProcessIdentity.capture(pid)
            self._publish()

    def publish_resource(self, name: str, path: Path) -> None:
        resource = ConfigFile(
            path=str(path),
            digest=hashlib.sha256(read_regular_file(path, limit=4 * 1024 * 1024)).hexdigest(),
        )
        with self._updates:
            self.record.resources[name] = resource
            self._publish()

    def retire_terminal(self, receipt: TerminalReceipt) -> None:
        with self._updates:
            if receipt in self.record.terminals:
                self.record.terminals.remove(receipt)
                self._publish()

    def retire_helper(self, name: str, identity: ProcessIdentity) -> None:
        with self._updates:
            if self.record.helpers.get(name) == identity:
                del self.record.helpers[name]
                self._publish()

    def retire_resource(self, name: str, resource: ConfigFile) -> None:
        with self._updates:
            if self.record.resources.get(name) == resource:
                del self.record.resources[name]
                self._publish()

    def require_session_scope(self, session_id: str) -> None:
        with self._updates:
            if session_id == self.record.session_id:
                return
            try:
                directory = _session_dir(self.directory.parent.parent, session_id)
            except FileNotFoundError:
                return
            record = _read_record(directory / "session.json")
            if record.state == "transferred":
                raise CustodyError("scope_mismatch")

    def transfer_session(self, source: str, target: str, socket: Path) -> None:
        with self._updates:
            if self.record.session_id != source or not self.still_owner():
                raise CustodyError("scope_mismatch")
            if not any(
                (r.name, r.session_key, r.socket) == ("codex", "main", str(socket))
                for r in self.record.terminals
            ):
                raise CustodyError("binding_mismatch")
            operation = _lock(self.directory / "operation.lock")
            target_lock = None
            try:
                if not self.still_owner():
                    raise CustodyError("scope_mismatch")
                verify_resources(self.record)
                directory = _session_dir(self.directory.parent.parent, target, create=True)
                if (directory / "session.json").exists():
                    raise CustodyError("owner_busy")
                target_lock = _lock(directory / "owner.lock")
                retired = self.record.model_copy(update={"state": "transferred"})
                successor = self.record.model_copy(update={"session_id": target}, deep=True)
                write_state_file(self.record_path, retired.model_dump(mode="json"))
                write_state_file(directory / "session.json", successor.model_dump(mode="json"))
                old_lock = self._lock
                self.directory = directory
                self.record_path = directory / "session.json"
                self.record = successor
                self._lock = target_lock
                target_lock = None
                info = Path(self._lock.lock_file).lstat()
                self._lock_identity = (info.st_dev, info.st_ino)
                self._context = CustodyContext.model_validate(
                    successor.model_dump(include=set(CustodyContext.model_fields))
                )
                old_lock.release()
            finally:
                if target_lock is not None:
                    target_lock.release()
                operation.release()

    def finish(self) -> None:
        with self._updates:
            if self.still_owner():
                self.record.state = "ended"
                write_state_file(self.record_path, self.record.model_dump(mode="json"))
            self._lock.release()


class SessionAttachment:
    """One host generation may attach to an unchanged live session owner."""

    def __init__(self, base: Path, session_id: str, *, generation: str) -> None:
        self.directory = _session_dir(base, session_id)
        self.generation = generation
        self.session_id = session_id
        self._lock: FileLock | None = None
        self.record: CustodyRecord | None = None

    def __enter__(self) -> SessionAttachment:
        self._lock = _lock(self.directory / "attachment.lock")
        return self

    def adopt(self, *, host_id: str, endpoint: str, config_digest: str) -> CustodyRecord:
        if self._lock is None or not self._lock.is_locked:
            raise CustodyError("owner_busy")
        record = _read_record(self.directory / "session.json")
        if (
            record.session_id != self.session_id
            or record.host_id != host_id
            or record.endpoint != endpoint
        ):
            raise CustodyError("scope_mismatch")
        if record.config_digest != config_digest:
            raise CustodyError("config_mismatch")
        if (
            record.state == "ended"
            or record_flock_is_held(self.directory / "owner.lock") is not True
        ):
            raise CustodyError("stale_attachment")
        verify_resources(record)
        attachment_path = self.directory / "attachment.json"
        previous: AttachmentRecord | None = None
        if attachment_path.exists() or attachment_path.is_symlink():
            try:
                previous = AttachmentRecord.model_validate_json(
                    read_regular_file(attachment_path, limit=65536)
                )
            except (OSError, ValueError) as exc:
                raise CustodyError("record_mismatch") from exc
        if (
            previous is not None
            and previous.generation == self.generation
            and previous.state == "attached"
        ):
            if (
                previous.process != ProcessIdentity.capture(os.getpid())
                or previous.owner_generation != record.owner_generation
            ):
                raise CustodyError("record_mismatch")
            self.record = record
            return record
        if previous is not None and previous.state == "attached":
            if (
                previous.process.pid_ns != current_pid_namespace()
                or previous.process.boot_id != current_boot_id()
            ):
                raise CustodyError("record_mismatch")
            try:
                previous.process.require_live()
            except psutil.NoSuchProcess:
                pass
            except ValueError:
                # A reused PID is absent evidence for this attachment, never signal it.
                pass
            except psutil.Error as exc:
                raise CustodyError("unavailable_probe") from exc
            else:
                raise CustodyError("owner_busy")
        attachment = AttachmentRecord(
            generation=self.generation,
            process=ProcessIdentity.capture(os.getpid()),
            epoch=previous.epoch + 1 if previous is not None else 1,
            state="attached",
            owner_generation=record.owner_generation,
        )
        write_state_file(attachment_path, attachment.model_dump(mode="json"))
        self.record = record
        return record

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._lock is None:
            return
        try:
            if self.record is not None:
                path = self.directory / "attachment.json"
                current = AttachmentRecord.model_validate_json(
                    read_regular_file(path, limit=65536)
                )
                if current.generation != self.generation:
                    raise CustodyError("record_mismatch")
                current.state = "detached"
                write_state_file(path, current.model_dump(mode="json"))
        finally:
            self._lock.release()
            self._lock = None


class CustodyProcess:
    """Popen-shaped adapter for an exactly verified non-child session owner."""

    def __init__(self, record: CustodyRecord, *, record_path: Path | None = None) -> None:
        self.identity = record.owner
        self.pid = record.owner.pid
        self.returncode: int | None = None
        self._record_path = record_path
        self._owner_generation = record.owner_generation

    def _exit_status(self) -> int:
        if self._record_path is not None:
            try:
                record = _read_record(self._record_path)
                if (
                    record.owner == self.identity
                    and record.owner_generation == self._owner_generation
                    and record.state == "ended"
                ):
                    return 0
            except CustodyError:
                pass
        return 254

    def poll(self) -> int | None:
        try:
            self.identity.require_live()
        except (ValueError, psutil.NoSuchProcess):
            self.returncode = self._exit_status()
        return self.returncode

    def terminate(self) -> None:
        _require_live(self.identity).terminate()

    def kill(self) -> None:
        _require_live(self.identity).kill()

    def wait(self, timeout: float | None = None) -> int:
        try:
            process = self.identity.require_live()
        except (ValueError, psutil.NoSuchProcess):
            self.returncode = self._exit_status()
            return self.returncode
        try:
            process.wait(timeout=timeout)
        except psutil.TimeoutExpired as exc:
            assert timeout is not None
            raise subprocess.TimeoutExpired("session-custodian", timeout) from exc
        self.returncode = self._exit_status()
        return self.returncode


_current_custody: SessionCustody | None = None


def current_custody() -> SessionCustody | None:
    return _current_custody


def install_custody(base: Path, context: dict[str, object]) -> SessionCustody:
    global _current_custody
    if _current_custody is not None:
        raise CustodyError("owner_busy")
    _current_custody = SessionCustody.create(base, context)
    return _current_custody


def host_config_digest() -> str:
    """Qualify attachment policy/config without recording environment values."""
    from omnigent.config import global_config_path

    digest = hashlib.sha256()
    names = ("OMNIGENT_CONFIG_HOME", "OMNIGENT_DATA_DIR", "CODEX_HOME", "OMNIGENT_CODEX_PATH")
    digest.update(canonical_json({name: os.environ.get(name) for name in names}).encode())
    path = global_config_path()
    digest.update(str(path.absolute()).encode())
    if path.exists() or path.is_symlink():
        digest.update(read_regular_file(path, limit=4 * 1024 * 1024))
    return digest.hexdigest()


def make_custody_context(
    *,
    session_id: str,
    runner_id: str,
    host_id: str,
    endpoint: str,
    workspace: str,
    native_env: dict[str, str],
    inference_path: Path | None,
) -> dict[str, object]:
    from omnigent.host.launch_contract import runtime_identity

    configs = []
    if inference_path is not None:
        configs.append(
            {
                "path": str(inference_path),
                "digest": hashlib.sha256(
                    read_regular_file(inference_path, limit=4 * 1024 * 1024)
                ).hexdigest(),
            }
        )
    return {
        "session_id": session_id,
        "runner_id": runner_id,
        "harness": "codex-native",
        "host_id": host_id,
        "endpoint": endpoint,
        "workspace": workspace,
        "config_digest": host_config_digest(),
        "environment_digest": native_launch_environment_digest(native_env),
        "owner_runtime_generation": runtime_identity()["generation"],
        "config_files": configs,
    }


def custody_records(base: Path, *, refusals: dict[str, str] | None = None) -> list[CustodyRecord]:
    root = base.resolve() / "custody"
    if not root.exists() and not root.is_symlink():
        return []
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise CustodyError("record_mismatch")
    children = list(itertools.islice(root.iterdir(), 10001))
    if len(children) > 10000:
        raise CustodyError("unavailable_probe")
    records = []
    for child in sorted(children):
        try:
            info = child.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise CustodyError("record_mismatch")
            record = _read_record(child / "session.json")
            if _session_dir(base, record.session_id) != child:
                raise CustodyError("scope_mismatch")
        except (CustodyError, OSError) as exc:
            if refusals is not None:
                refusals[child.name] = (
                    str(exc) if isinstance(exc, CustodyError) else "record_mismatch"
                )
            continue
        if record.state == "transferred":
            if refusals is not None:
                refusals[child.name] = "scope_mismatch"
            continue
        records.append(record)
    return [record for record in records if record.state != "ended"]


def _verified_session_record(base: Path, session_id: str) -> CustodyRecord:
    directory = _session_dir(base, session_id)
    record = _read_record(directory / "session.json")
    if record.session_id != session_id or record.state == "transferred":
        raise CustodyError("scope_mismatch")
    if record.state != "ended":
        if record_flock_is_held(directory / "owner.lock") is not True:
            raise CustodyError("stale_attachment")
        verify_resources(record)
    return record


def session_contract(base: Path, session_id: str) -> dict[str, object]:
    record = _verified_session_record(base, session_id)
    directory = _session_dir(base, session_id)
    attachment_path = directory / "attachment.json"
    attachment = None
    if attachment_path.exists() or attachment_path.is_symlink():
        try:
            attachment = AttachmentRecord.model_validate_json(
                read_regular_file(attachment_path, limit=65536)
            ).model_dump(mode="json")
        except (OSError, ValueError) as exc:
            raise CustodyError("record_mismatch") from exc
    return {**record.model_dump(mode="json"), "reattachment": attachment}


def session_binding(base: Path, session_id: str) -> SessionBinding:
    if "codex-native" not in SUPPORTED_HARNESSES:
        raise CustodyError("unsupported_custody")
    record = _verified_session_record(base, session_id)
    if record.state != "live":
        raise CustodyError("stale_attachment")
    terminals = [r for r in record.terminals if (r.name, r.session_key) == ("codex", "main")]
    native = record.helpers.get("codex_app_server")
    if len(terminals) != 1 or native is None:
        raise CustodyError("binding_mismatch")
    fields = set(SessionBinding.model_fields) - {"native", "terminal"}
    return SessionBinding(
        **{field: getattr(record, field) for field in fields}, native=native, terminal=terminals[0]
    )


def verify_session_binding(base: Path, expected: SessionBinding) -> SessionBinding:
    actual = session_binding(base, expected.session_id)
    for field in ("session_id", "runner_id", "host_id", "endpoint", "workspace", "harness"):
        if getattr(actual, field) != getattr(expected, field):
            raise CustodyError("scope_mismatch")
    if (
        actual.config_digest != expected.config_digest
        or actual.environment_digest != expected.environment_digest
    ):
        raise CustodyError("config_mismatch")
    if actual.config_digest != host_config_digest():
        raise CustodyError("config_mismatch")
    if actual != expected:
        raise CustodyError("binding_mismatch")
    return actual


@contextmanager
def locked_session_owner(base: Path, session_id: str) -> Iterator[CustodyRecord]:
    directory = _session_dir(base, session_id)
    lock = _lock(directory / "operation.lock")
    try:
        yield _verified_session_record(base, session_id)
    finally:
        lock.release()


def end_session(
    base: Path,
    session_id: str,
    *,
    owner_generation: str,
    host_id: str,
    endpoint: str,
    config_digest: str,
) -> dict[str, object]:
    """End one exactly scoped session owner; keep its receipt as a tombstone."""
    directory = _session_dir(base, session_id)
    with locked_session_owner(base, session_id) as record:
        if (
            record.session_id != session_id
            or record.state == "transferred"
            or record.owner_generation != owner_generation
            or record.host_id != host_id
            or record.endpoint != endpoint
        ):
            raise CustodyError("scope_mismatch")
        if record.config_digest != config_digest:
            raise CustodyError("config_mismatch")
        if record.state == "ended":
            return {
                "schema_version": 1,
                "session_id": session_id,
                "owner_generation": owner_generation,
                "state": "ended",
            }
        process = CustodyProcess(record)
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired as exc:
            raise CustodyError("process_pending") from exc
        final = _read_record(directory / "session.json")
        if final.owner_generation != owner_generation or final.state != "ended":
            raise CustodyError("process_pending")
        return {
            "schema_version": 1,
            "session_id": session_id,
            "owner_generation": owner_generation,
            "state": "ended",
        }
