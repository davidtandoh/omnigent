"""Exact intentional end reconciles only proven-released owners and survives config drift."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import click
import psutil
import pytest
from click.testing import CliRunner

from omnigent import cli
from omnigent.host.connect import HostProcess
from omnigent.host.identity import HostIdentity
from omnigent.native.state_files import read_regular_file, write_state_file
from omnigent.runner import custody
from tests.runner.custody_owner_fixture import ENDPOINT, HOST_ID
from tests.runner.test_session_custody import context

_REPO = Path(__file__).resolve().parents[2]
_SESSION = "conv_owner_end"


@pytest.fixture
def state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    data = tmp_path / "state"
    home.mkdir()
    data.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(data))
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(data))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("OMNIGENT_CODEX_PATH", raising=False)
    (data / "config.yaml").write_text("{}\n")
    return data


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    # macOS limits Unix socket paths to 104 bytes; pytest tmp paths are longer.
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="custody-end-") as short:
        yield Path(short)


def _record_path(state: Path, session_id: str = _SESSION) -> Path:
    return state / "custody" / custody.custody_session_key(session_id) / "session.json"


def _current(state: Path, session_id: str = _SESSION) -> custody.CustodyRecord:
    return custody.CustodyRecord.model_validate_json(
        read_regular_file(_record_path(state, session_id), limit=262144)
    )


@contextlib.contextmanager
def _owner(
    state: Path,
    socket_dir: Path,
    mode: str,
    *,
    session_id: str = _SESSION,
    helper_pid: int | None = None,
) -> Iterator[tuple[subprocess.Popen[str], custody.CustodyRecord]]:
    args = [
        sys.executable,
        "-m",
        "tests.runner.custody_owner_fixture",
        mode,
        session_id,
        str(socket_dir),
    ]
    if helper_pid is not None:
        args.append(str(helper_pid))
    process = subprocess.Popen(
        args,
        cwd=_REPO,
        env=dict(os.environ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    ready = state / ("ready-" + session_id + ".json")
    try:
        deadline = time.monotonic() + 30
        while not ready.exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                raise AssertionError("isolated owner did not publish readiness")
            time.sleep(0.01)
        record = custody.CustodyRecord.model_validate_json(read_regular_file(ready, limit=262144))
        if mode.startswith("dead-"):
            assert process.wait(timeout=10) == 0
            assert not psutil.pid_exists(record.owner.pid)
        yield process, record
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        process.communicate(timeout=5)


def _end(state: Path, record: custody.CustodyRecord, **overrides: str) -> dict[str, object]:
    scope = {
        "owner_generation": record.owner_generation,
        "host_id": record.host_id,
        "endpoint": record.endpoint,
        **overrides,
    }
    return custody.end_session(state, record.session_id, **scope)


def _daemon_teardown() -> None:
    cli._end_daemon_custodied_sessions(
        SimpleNamespace(
            mode="local", resolved_server_url=ENDPOINT, server_url=ENDPOINT, host_id=HOST_ID
        )
    )


def _lease_is_free(path: Path) -> bool:
    try:
        probe = custody._lock(path)
    except custody.CustodyError:
        return False
    probe.release()
    return True


@pytest.mark.parametrize("mode", ["dead-starting", "dead-released"])
def test_exact_end_reconciles_absent_owner_after_release_proof(
    state: Path, socket_dir: Path, mode: str
) -> None:
    with _owner(state, socket_dir, mode) as (_, record):
        assert record.state == ("live" if mode == "dead-released" else "starting")
        result = _end(state, record)
        assert result == {
            "schema_version": 1,
            "session_id": _SESSION,
            "owner_generation": record.owner_generation,
            "state": "ended",
        }
        assert _current(state).model_copy(update={"state": record.state}) == record
        assert _current(state).state == "ended"
        assert _end(state, record) == result
        assert _lease_is_free(_record_path(state).parent / "owner.lock")
        replacement = custody.SessionCustody.create(
            state,
            {**context(), "session_id": _SESSION, "config_digest": custody.host_config_digest()},
        )
        replacement.finish()


@pytest.mark.parametrize("resource", ["helper", "terminal"])
def test_finish_keeps_unretired_resources_unresolved_until_release_proven(
    state: Path, socket_dir: Path, resource: str
) -> None:
    with subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ) as helper:
        try:
            with _owner(
                state, socket_dir, "dead-unretired-" + resource, helper_pid=helper.pid
            ) as (_, record):
                assert _current(state) == record
                assert record.state == ("starting" if resource == "helper" else "live")
                assert (
                    custody.record_flock_is_held(_record_path(state).parent / "owner.lock")
                    is False
                )
                with pytest.raises(custody.CustodyError, match="stale_attachment"):
                    custody.SessionCustody.create(state, {**context(), "session_id": _SESSION})
                with pytest.raises(custody.CustodyError, match="process_pending"):
                    _end(state, record)
                assert _current(state) == record
                assert helper.poll() is None
                if resource == "helper":
                    helper.terminate()
                    helper.wait(timeout=5)
                else:
                    (socket_dir / (_SESSION + ".sock")).unlink()
                assert _end(state, record)["state"] == "ended"
                assert _current(state).state == "ended"
        finally:
            if helper.poll() is None:
                helper.terminate()
                helper.wait(timeout=5)


def test_finish_allows_retained_policy_resources(state: Path) -> None:
    with custody.SessionCustody.create(state, {**context(), "session_id": _SESSION}) as owner:
        policy = state / "policy.json"
        write_state_file(policy, {"policy": "synthetic-deny"})
        owner.publish_resource("tool_policy_bridge", policy)
        owner.finish()
        assert _current(state).state == "ended"
        assert policy.exists()
        assert _lease_is_free(owner.directory / "owner.lock")


def _rewrite(data_dir: Path, **changes: object) -> None:
    data = json.loads(read_regular_file(_record_path(data_dir), limit=262144))
    for key, value in changes.items():
        if key.startswith("owner."):
            data["owner"][key.removeprefix("owner.")] = value
        else:
            data[key] = value
    write_state_file(_record_path(data_dir), data)


@pytest.mark.parametrize(
    "case,category",
    [
        ("live-helper", "process_pending"),
        ("retained-policy", "process_pending"),
        ("retained-socket", "process_pending"),
        ("foreign-socket", "foreign_socket"),
        ("wrong-generation", "scope_mismatch"),
        ("foreign-host", "scope_mismatch"),
        ("foreign-endpoint", "scope_mismatch"),
        ("reused-pid", "record_mismatch"),
        ("unknown-identity", "record_mismatch"),
        ("missing-lease", "record_mismatch"),
        ("held-lease", "record_mismatch"),
    ],
)
def test_absent_owner_without_full_release_proof_refuses_without_mutation(
    state: Path, socket_dir: Path, case: str, category: str
) -> None:
    helper = None
    listener = None
    held = None
    overrides: dict[str, str] = {}
    mode = {
        "live-helper": "dead-helper",
        "retained-policy": "dead-policy",
        "retained-socket": "dead-retained-socket",
        "foreign-socket": "dead-retained-socket",
    }.get(case, "dead-starting")
    if case == "live-helper":
        helper = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    try:
        with _owner(
            state, socket_dir, mode, helper_pid=helper.pid if helper is not None else None
        ) as (_, record):
            directory = _record_path(state).parent
            sock = socket_dir / (_SESSION + ".sock")
            if case == "foreign-socket":
                sock.rename(socket_dir / "original.sock")
                listener = socket.socket(socket.AF_UNIX)
                listener.bind(str(sock))
            elif case == "wrong-generation":
                overrides["owner_generation"] = "foreign-generation"
            elif case == "foreign-host":
                overrides["host_id"] = "host_foreign"
            elif case == "foreign-endpoint":
                overrides["endpoint"] = "http://127.0.0.1:9"
            elif case == "reused-pid":
                live = psutil.Process(os.getpid())
                _rewrite(state, **{"owner.pid": live.pid})
            elif case == "unknown-identity":
                _rewrite(state, **{"owner.pid_ns": "foreign-namespace"})
            elif case == "missing-lease":
                (directory / "owner.lock").unlink()
            elif case == "held-lease":
                held = custody._lock(directory / "owner.lock")
            before = read_regular_file(_record_path(state), limit=262144)
            socket_before = sock.lstat() if sock.exists() else None
            with pytest.raises(custody.CustodyError, match=category):
                _end(state, record, **overrides)
            assert read_regular_file(_record_path(state), limit=262144) == before
            assert directory.is_dir()
            if case == "missing-lease":
                assert not (directory / "owner.lock").exists()
            if helper is not None:
                assert helper.poll() is None
            if case == "retained-policy":
                assert (state / (_SESSION + "-policy.json")).exists()
            if socket_before is not None:
                assert sock.lstat().st_ino == socket_before.st_ino
    finally:
        if held is not None:
            held.release()
        if listener is not None:
            listener.close()
        if helper is not None:
            helper.terminate()
            helper.wait(timeout=5)


def test_absent_owner_record_change_during_proof_refuses_tombstone(
    state: Path, socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _owner(state, socket_dir, "dead-starting") as (_, record):
        original = custody._require_released

        def contradict(identity: object) -> None:
            _rewrite(state, runner_id="runner_contradictory")
            original(identity)

        monkeypatch.setattr(custody, "_require_released", contradict)
        with pytest.raises(custody.CustodyError, match="record_mismatch"):
            _end(state, record)
        assert _current(state).state == "starting"
        assert _current(state).runner_id == "runner_contradictory"


def test_concurrent_and_repeated_end_of_absent_owner_is_exclusive(
    state: Path, socket_dir: Path
) -> None:
    with _owner(state, socket_dir, "dead-starting") as (_, record):
        operation = custody._lock(_record_path(state).parent / "operation.lock")
        try:
            before = read_regular_file(_record_path(state), limit=262144)
            with pytest.raises(custody.CustodyError, match="owner_busy"):
                _end(state, record)
            assert read_regular_file(_record_path(state), limit=262144) == before
        finally:
            operation.release()
        barrier = threading.Barrier(4)
        outcomes: list[str] = []

        def end_once() -> None:
            barrier.wait()
            try:
                outcomes.append(str(_end(state, record)["state"]))
            except custody.CustodyError as error:
                outcomes.append(str(error))

        threads = [threading.Thread(target=end_once) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert "ended" in outcomes
        assert set(outcomes) <= {"ended", "owner_busy"}
        assert _current(state).state == "ended"
        assert _end(state, record)["state"] == "ended"


@pytest.mark.parametrize("drift", ["config", "CODEX_HOME", "OMNIGENT_CODEX_PATH"])
def test_config_drift_keeps_adoption_strict_but_permits_exact_end(
    state: Path, socket_dir: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    with _owner(state, socket_dir, "live") as (process, record):
        if drift == "config":
            (state / "config.yaml").write_text("ui:\n  theme: synthetic-dark\n")
        else:
            monkeypatch.setenv(drift, str(state / "other-selector"))
        digest = custody.host_config_digest()
        assert digest != record.config_digest
        with custody.SessionAttachment(state, _SESSION, generation="drift") as attachment:
            with pytest.raises(custody.CustodyError, match="config_mismatch"):
                attachment.adopt(
                    host_id=record.host_id, endpoint=record.endpoint, config_digest=digest
                )
        for overrides in (
            {"owner_generation": "foreign-generation"},
            {"host_id": "host_foreign"},
            {"endpoint": "http://127.0.0.1:9"},
        ):
            with pytest.raises(custody.CustodyError, match="scope_mismatch"):
                _end(state, record, **overrides)
            assert process.poll() is None
        assert _end(state, record)["state"] == "ended"
        assert process.wait(timeout=30) == 0
        assert _current(state).state == "ended"


def test_daemon_teardown_reconciles_absent_owner_and_ignores_config_bytes(
    state: Path, socket_dir: Path
) -> None:
    with (
        _owner(state, socket_dir, "dead-starting", session_id="conv_dead") as (_, dead),
        _owner(state, socket_dir, "live", session_id="conv_live") as (process, live),
    ):
        (state / "config.yaml").write_text("ui:\n  theme: synthetic-dark\n")
        _daemon_teardown()
        assert process.wait(timeout=30) == 0
        assert _current(state, dead.session_id).state == "ended"
        assert _current(state, live.session_id).state == "ended"


def test_daemon_teardown_refusal_is_isolated_per_session(state: Path, socket_dir: Path) -> None:
    helper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with (
            _owner(
                state, socket_dir, "dead-helper", session_id="conv_held", helper_pid=helper.pid
            ) as (_, held),
            _owner(state, socket_dir, "live", session_id="conv_live") as (process, _),
        ):
            before = read_regular_file(_record_path(state, held.session_id), limit=262144)
            with pytest.raises(click.ClickException, match="process_pending") as caught:
                _daemon_teardown()
            assert str(helper.pid) not in caught.value.message
            assert process.wait(timeout=30) == 0
            assert _current(state, "conv_live").state == "ended"
            assert read_regular_file(_record_path(state, held.session_id), limit=262144) == before
            assert helper.poll() is None
    finally:
        helper.terminate()
        helper.wait(timeout=5)


@pytest.mark.parametrize("command", [["server", "stop"], ["stop"]])
@pytest.mark.parametrize("invalid", ["malformed", "unreadable"])
def test_daemon_stop_processes_valid_sessions_and_reports_scan_refusals(
    state: Path,
    socket_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    invalid: str,
) -> None:
    from unittest.mock import Mock

    with custody.SessionCustody.create(
        state, {**context(), "session_id": "conv_invalid"}
    ) as invalid_owner:
        invalid_path = invalid_owner.record_path
    if invalid == "malformed":
        invalid_path.write_text('{"private-details":')
    else:
        original_read = custody.read_regular_file

        def unreadable(path: Path, *, limit: int) -> bytes:
            if path == invalid_path:
                raise PermissionError("private-details")
            return original_read(path, limit=limit)

        monkeypatch.setattr(custody, "read_regular_file", unreadable)
    before = read_regular_file(invalid_path, limit=262144)
    record = SimpleNamespace(
        mode="local", resolved_server_url=ENDPOINT, server_url=ENDPOINT, host_id=HOST_ID
    )
    monkeypatch.setattr(cli, "_find_daemon_record", lambda target: record)
    monkeypatch.setattr(cli, "_list_daemon_records", lambda: [record])
    monkeypatch.setattr(cli, "local_server_url_if_healthy", lambda: ENDPOINT)
    terminate = Mock()
    stop_server = Mock()
    monkeypatch.setattr(cli, "_terminate_daemon", terminate)
    monkeypatch.setattr(cli, "stop_local_omnigent_server", stop_server)
    monkeypatch.setattr(cli, "stop_untracked_local_server", lambda: None)
    with _owner(state, socket_dir, "live") as (process, _):
        result = CliRunner().invoke(cli.cli, command)
        assert process.wait(timeout=30) == 0
        assert _current(state).state == "ended"
    assert read_regular_file(invalid_path, limit=262144) == before
    stop_server.assert_called_once_with()
    assert result.exit_code == 1, result.output
    assert "Session teardown refused: record_mismatch" in result.output
    assert "private-details" not in result.output
    assert str(invalid_path) not in result.output
    terminate.assert_not_called()


def test_global_stop_ends_owner_after_host_maintenance_detach(
    state: Path, socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import Mock

    record = SimpleNamespace(
        mode="local",
        resolved_server_url=ENDPOINT,
        server_url=ENDPOINT,
        host_id=HOST_ID,
        target=ENDPOINT,
        pid=999999,
    )
    records = [record]
    monkeypatch.setattr(cli, "_selected_daemon_records", lambda **kwargs: records)
    monkeypatch.setattr(cli, "_list_daemon_records", lambda: list(records))
    monkeypatch.setattr(cli, "_terminate_daemon", lambda record, **kwargs: records.remove(record))
    monkeypatch.setattr(cli, "local_server_url_if_healthy", lambda: None)
    stop_server = Mock()
    monkeypatch.setattr(cli, "stop_local_omnigent_server", stop_server)
    monkeypatch.setattr(cli, "stop_untracked_local_server", lambda: None)
    with _owner(state, socket_dir, "live") as (process, _):
        detached = CliRunner().invoke(
            cli.cli, ["host", "maintenance-detach", "--server", ENDPOINT]
        )
        assert detached.exit_code == 0, detached.output
        assert records == []
        assert process.poll() is None
        assert _current(state).state != "ended"

        result = CliRunner().invoke(cli.cli, ["stop"])

        assert result.exit_code == 0, result.output
        assert "1 detached session(s)" in result.output
        assert process.wait(timeout=10) == 0
        assert _current(state).state == "ended"
        stop_server.assert_called_once_with()


def test_global_stop_reports_scan_refusal_without_daemon_records(
    state: Path, socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import Mock

    monkeypatch.setattr(cli, "_list_daemon_records", list)
    monkeypatch.setattr(cli, "local_server_url_if_healthy", lambda: None)
    monkeypatch.setattr(cli, "stop_local_omnigent_server", Mock())
    monkeypatch.setattr(cli, "stop_untracked_local_server", lambda: None)
    with _owner(state, socket_dir, "live") as (process, _):
        invalid = _record_path(state, "conv_invalid")
        invalid.parent.mkdir(mode=0o700)
        invalid.write_text('{"private-details":')
        before = invalid.read_bytes()
        result = CliRunner().invoke(cli.cli, ["stop"])
        assert result.exit_code == 1, result.output
        assert "Session teardown refused: record_mismatch" in result.output
        assert "private-details" not in result.output
        assert process.wait(timeout=10) == 0
        assert _current(state).state == "ended"
        assert invalid.read_bytes() == before


@pytest.mark.parametrize("refused_scope", ["covered", "detached"])
def test_global_stop_excludes_covered_sessions_and_isolates_refusals(
    state: Path, monkeypatch: pytest.MonkeyPatch, refused_scope: str
) -> None:
    from unittest.mock import Mock

    covered = custody.CustodyRecord(
        **context(),
        owner_generation="generation",
        owner=custody.ProcessIdentity.capture(os.getpid()),
    )
    detached = covered.model_copy(update={"session_id": "conv_detached", "host_id": "detached"})
    daemon = SimpleNamespace(
        mode="local", resolved_server_url=covered.endpoint, host_id=covered.host_id
    )
    monkeypatch.setattr(cli, "_list_daemon_records", lambda: [daemon])
    monkeypatch.setattr(custody, "custody_records", lambda *args, **kwargs: [covered, detached])
    ended = []

    def end(base: Path, session_id: str, **scope: str) -> None:
        ended.append((session_id, scope))
        refused = covered if refused_scope == "covered" else detached
        if session_id == refused.session_id:
            raise custody.CustodyError("process_pending")

    monkeypatch.setattr(custody, "end_session", end)
    terminate = Mock()
    monkeypatch.setattr(cli, "_terminate_daemon", terminate)
    monkeypatch.setattr(cli, "local_server_url_if_healthy", lambda: None)
    monkeypatch.setattr(cli, "stop_local_omnigent_server", Mock())
    monkeypatch.setattr(cli, "stop_untracked_local_server", lambda: None)

    result = CliRunner().invoke(cli.cli, ["stop"])

    assert result.exit_code == 1, result.output
    assert "Session teardown refused: process_pending" in result.output
    assert ended == [
        (
            r.session_id,
            {
                "owner_generation": r.owner_generation,
                "host_id": r.host_id,
                "endpoint": r.endpoint,
            },
        )
        for r in (covered, detached)
    ]
    if refused_scope == "covered":
        terminate.assert_not_called()
    else:
        terminate.assert_called_once_with(daemon, force=False)


def test_host_stop_waits_for_verified_owner_cleanup_budget(state: Path, socket_dir: Path) -> None:
    with _owner(state, socket_dir, "slow-stop") as (process, record):
        adapter = custody.CustodyProcess(record, record_path=_record_path(state))
        assert 6 < adapter.stop_grace_s < 60
        HostProcess._stop_runner_proc(adapter)
        assert process.wait(timeout=10) == 0
        final = _current(state)
        assert final.state == "ended"
        assert final.owner_generation == record.owner_generation


def test_owner_stop_grace_tracks_recorded_helpers_and_terminals() -> None:
    from omnigent.harnesses.codex_native.app_server import (
        APP_SERVER_STDERR_DRAIN_S,
        APP_SERVER_STOP_TIMEOUT_S,
    )
    from omnigent.runner.native.orchestration import _AUTO_FORWARDER_CANCEL_TIMEOUT_S
    from omnigent.terminals.registry import _CLOSE_TIMEOUT_S

    record = custody.CustodyRecord(
        **context(),
        owner_generation="generation",
        owner=custody.ProcessIdentity.capture(os.getpid()),
    )
    base = custody.owner_stop_grace_s(record)
    identity = custody.ProcessIdentity.capture(os.getpid())
    with_helper = record.model_copy(update={"helpers": {"codex_app_server": identity}})
    terminal = custody.TerminalReceipt(
        name="codex",
        session_key="main",
        socket="/s",
        device=1,
        inode=1,
        tmux=identity,
        pane=identity,
    )
    with_terminals = with_helper.model_copy(update={"terminals": [terminal, terminal]})
    assert (
        base < custody.owner_stop_grace_s(with_helper) < custody.owner_stop_grace_s(with_terminals)
    )
    assert custody.owner_stop_grace_s(with_terminals) < 60
    assert custody.owner_stop_grace_s(with_helper) == (
        base
        + _AUTO_FORWARDER_CANCEL_TIMEOUT_S
        + APP_SERVER_STOP_TIMEOUT_S
        + APP_SERVER_STDERR_DRAIN_S
    )
    assert custody.owner_stop_grace_s(with_terminals) == (
        custody.owner_stop_grace_s(with_helper) + 2 * _CLOSE_TIMEOUT_S
    )


@pytest.mark.parametrize("command", [["server", "stop"], ["stop"]])
@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize(
    "error,category",
    [
        (custody.CustodyError("process_pending"), "process_pending"),
        (OSError("private-path-and-details"), "unavailable_probe"),
    ],
)
def test_stop_reports_custody_refusal_after_api_cleanup(
    state: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    force: bool,
    error: Exception,
    category: str,
) -> None:
    from unittest.mock import Mock

    record = SimpleNamespace(
        mode="local", resolved_server_url=ENDPOINT, server_url=ENDPOINT, host_id=HOST_ID
    )
    monkeypatch.setattr(cli, "_find_daemon_record", lambda target: record)
    monkeypatch.setattr(cli, "_list_daemon_records", lambda: [record])
    monkeypatch.setattr(cli, "local_server_url_if_healthy", lambda: ENDPOINT)
    monkeypatch.setattr(custody, "custody_records", Mock(side_effect=error))
    terminate = Mock()
    cleanup = Mock()
    monkeypatch.setattr(cli, "_terminate_daemon", terminate)
    monkeypatch.setattr(cli, "stop_local_omnigent_server", cleanup.managed)
    monkeypatch.setattr(cli, "stop_untracked_local_server", cleanup.orphan)
    cleanup.orphan.return_value = None

    result = CliRunner().invoke(cli.cli, command + (["--force"] if force else []))

    assert result.exit_code == 1, result.output
    assert "Session teardown refused: " + category in result.output
    assert "private-path-and-details" not in result.output
    terminate.assert_not_called()
    cleanup.managed.assert_called_once_with()
    cleanup.orphan.assert_called_once_with()


@pytest.mark.parametrize("corruption", ["malformed", "foreign-generation"])
def test_host_cleanup_isolates_attachment_refusal(
    state: Path, corruption: str, caplog: pytest.LogCaptureFixture
) -> None:
    owners = []
    attachments = []
    try:
        for index in range(2):
            session = "conv_detach_" + str(index)
            owners.append(
                custody.SessionCustody.create(state, {**context(), "session_id": session})
            )
            attachment = custody.SessionAttachment(
                state, session, generation="attachment_" + str(index)
            )
            attachment.__enter__()
            attachment.adopt(
                host_id="host_synthetic", endpoint="http://127.0.0.1:8123", config_digest="a" * 64
            )
            attachments.append(attachment)
        first = attachments[0].directory / "attachment.json"
        if corruption == "malformed":
            first.write_text('{"schema_version":1,"secret":"do-not-log"}')
        else:
            data = json.loads(read_regular_file(first, limit=65536))
            data["generation"] = "foreign-generation"
            first.write_text(json.dumps(data))
        contradictory = read_regular_file(first, limit=65536)
        host = HostProcess(
            HostIdentity(host_id="host_synthetic", name="isolated"),
            "http://127.0.0.1:8123",
            interactive_shells=["bash"],
        )
        host._custody_attachments = {
            "conv_detach_0": attachments[0],
            "conv_detach_1": attachments[1],
        }
        with caplog.at_level(logging.WARNING):
            host._cleanup_runners()
        assert host._custody_attachments == {}
        for attachment in attachments:
            assert _lease_is_free(attachment.directory / "attachment.lock")
        assert read_regular_file(first, limit=65536) == contradictory
        second = json.loads(
            read_regular_file(attachments[1].directory / "attachment.json", limit=65536)
        )
        assert second["state"] == "detached"
        assert "record_mismatch" in caplog.text
        assert "do-not-log" not in caplog.text
        assert "validation error" not in caplog.text.lower()
    finally:
        for attachment in attachments:
            if attachment._lock is not None:
                attachment._lock.release()
        for owner in owners:
            owner.finish()
