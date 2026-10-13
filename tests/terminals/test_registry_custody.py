from __future__ import annotations

import asyncio
import errno
import json
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.inner import terminal as terminal_module
from omnigent.inner.datamodel import TerminalEnvSpec
from omnigent.inner.terminal import TerminalCreateResult, TerminalInstance
from omnigent.runner import custody
from omnigent.terminals import registry as registry_module
from tests.runner.test_session_custody import context


@pytest.fixture
def owned_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with (
        tempfile.TemporaryDirectory(dir="/tmp", prefix="custody-probe-") as short,
        socket.socket(socket.AF_UNIX) as sock,
        custody.SessionCustody.create(tmp_path, context()) as owner,
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]) as process,
    ):
        try:
            path = Path(short) / "tmux.sock"
            sock.bind(str(path))
            instance = TerminalInstance("codex", "main", path, Path(short), running=True)
            monkeypatch.setattr(instance, "launch", AsyncMock())
            monkeypatch.setattr(instance, "is_alive", AsyncMock(return_value=True))
            monkeypatch.setattr(
                terminal_module,
                "subprocess",
                SimpleNamespace(
                    run=Mock(
                        return_value=SimpleNamespace(
                            returncode=0, stdout=str(process.pid).encode(), stderr=b""
                        )
                    )
                ),
            )
            monkeypatch.setattr(
                registry_module,
                "psutil",
                SimpleNamespace(
                    Process=lambda pid: SimpleNamespace(ppid=lambda: process.pid),
                    Error=custody.psutil.Error,
                ),
            )
            monkeypatch.setattr(custody, "_current_custody", owner)
            create = Mock(return_value=TerminalCreateResult(instance, tmp_path))
            monkeypatch.setattr(registry_module, "create_terminal_instance", create)

            async def killed():
                process.terminate()
                process.wait(timeout=5)
                return b"", b""

            spawn = AsyncMock(
                return_value=SimpleNamespace(
                    returncode=0, communicate=AsyncMock(side_effect=killed)
                )
            )
            monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
            registry = registry_module.TerminalRegistry()
            args = ("conv_synthetic", "codex", "main", TerminalEnvSpec(command="bash"))
            yield SimpleNamespace(
                owner=owner,
                instance=instance,
                process=process,
                create=create,
                spawn=spawn,
                registry=registry,
                args=args,
            )
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


@pytest.mark.parametrize("close_fails", [False, True])
@pytest.mark.parametrize(
    "failure", ["eagain", "runtime", "oserror", "tmux_probe", "publication", "verification"]
)
async def test_launch_failure_never_exposes_unpublished_terminal(
    owned_terminal, monkeypatch: pytest.MonkeyPatch, failure: str, close_fails: bool
) -> None:
    t = owned_terminal
    category = "unavailable_probe"
    if failure == "eagain":
        terminal_module.subprocess.run.side_effect = OSError(errno.EAGAIN, "private diagnostics")
    elif failure in {"runtime", "oserror"}:
        error = RuntimeError if failure == "runtime" else OSError
        monkeypatch.setattr(
            t.instance, "pane_pid_sync", Mock(side_effect=error("private diagnostics"))
        )
    elif failure == "tmux_probe":
        monkeypatch.setattr(
            registry_module.psutil, "Process", Mock(side_effect=OSError("private diagnostics"))
        )
    elif failure == "publication":
        publish = t.owner._publish

        def fail_completed_publication():
            if any(r.tmux is not None for r in t.owner.record.terminals):
                raise OSError("private diagnostics")
            publish()

        monkeypatch.setattr(t.owner, "_publish", fail_completed_publication)
    else:
        category = "record_mismatch"
        monkeypatch.setattr(
            t.owner, "require_terminal_published", Mock(side_effect=custody.CustodyError(category))
        )
    if close_fails:
        t.spawn.side_effect = OSError(errno.EAGAIN, "private diagnostics")

    with pytest.raises(RuntimeError) as refused:
        await t.registry.launch(*t.args)
    assert str(refused.value) == f"Session custody refused: {category}"
    assert t.registry.get(*t.args[:3]) is None
    assert t.registry.list_for_conversation(t.args[0]) == []
    assert t.registry.get_instance_lock(*t.args[:3]) is None
    t.spawn.assert_awaited_once()
    assert t.spawn.call_args.args[-1] == "kill-server"
    assert (t.process.poll() is None) == close_fails

    unresolved = close_fails or failure not in {"publication", "verification"}
    if unresolved:
        assert len(t.owner.record.terminals) == 1
        persisted = json.loads(t.owner.record_path.read_bytes())
        assert len(persisted["terminals"]) == 1
        with pytest.raises(RuntimeError, match="Session custody refused: process_pending"):
            await t.registry.launch(*t.args)
        t.create.assert_called_once()
        if close_fails and failure in {"publication", "verification"}:
            custody.verify_resources(t.owner.record)
            assert t.instance.running
            assert t.instance.socket_path.exists()
            assert t.instance.private_dir.exists()
        else:
            with pytest.raises(custody.CustodyError):
                custody.verify_resources(t.owner.record)
    else:
        assert t.owner.record.terminals == []
    t.owner.finish()
    assert (json.loads(t.owner.record_path.read_bytes())["state"] == "ended") != unresolved


async def test_published_terminal_reuse_and_close(owned_terminal) -> None:
    t = owned_terminal
    assert await t.registry.launch(*t.args) is t.instance
    assert await t.registry.launch(*t.args) is t.instance
    assert t.registry.get(*t.args[:3]) is t.instance
    assert t.registry.list_for_conversation(t.args[0])[0].instance is t.instance
    assert t.registry.native_panes() == [(t.args[0], "codex", t.instance.socket_path)]
    t.create.assert_called_once()
    receipt = t.owner.require_terminal_published("codex", "main", t.instance.socket_path)
    assert receipt.tmux is not None and receipt.tmux.pid == t.process.pid
    assert await t.registry.close(*t.args[:3])
    assert t.owner.record.terminals == []
    t.owner.finish()
    assert json.loads(t.owner.record_path.read_bytes())["state"] == "ended"


@pytest.mark.parametrize("operation", ["launch", "get", "list", "native", "transfer"])
@pytest.mark.parametrize("damage", ["missing", "unpublished", "generation", "lease"])
async def test_registry_refuses_unverified_existing_terminal(
    owned_terminal, operation: str, damage: str
) -> None:
    t = owned_terminal
    await t.registry.launch(*t.args)
    if damage == "missing":
        t.owner.record.terminals.clear()
        t.owner._publish()
    elif damage == "unpublished":
        record = json.loads(t.owner.record_path.read_bytes())
        record["terminals"] = []
        t.owner.record_path.write_text(json.dumps(record))
    elif damage == "generation":
        record = json.loads(t.owner.record_path.read_bytes())
        record["owner_generation"] = "foreign"
        t.owner.record_path.write_text(json.dumps(record))
    else:
        t.owner._lock.release()
    before = t.owner.record_path.read_bytes()
    with pytest.raises(RuntimeError, match="Session custody refused:"):
        if operation == "launch":
            await t.registry.launch(*t.args)
        elif operation == "get":
            t.registry.get(*t.args[:3])
        elif operation == "list":
            t.registry.list_for_conversation(t.args[0])
        elif operation == "native":
            t.registry.native_panes()
        else:
            t.registry.transfer(t.args[0], "conv_new", "codex", "main")
    t.create.assert_called_once()
    t.spawn.assert_not_awaited()
    assert t.owner.record_path.read_bytes() == before
    assert t.process.poll() is None


@pytest.mark.parametrize("operation", ["close", "cleanup", "shutdown"])
async def test_cleanup_retains_evidence_for_missing_receipt(
    owned_terminal, operation: str
) -> None:
    t = owned_terminal
    await t.registry.launch(*t.args)
    t.owner.record.terminals.clear()
    t.owner._publish()
    t.spawn.side_effect = OSError(errno.EAGAIN, "private diagnostics")
    if operation == "close":
        with pytest.raises(RuntimeError, match="tmux command could not start"):
            await t.registry.close(*t.args[:3])
    elif operation == "cleanup":
        await t.registry.cleanup_conversation(t.args[0])
    else:
        await t.registry.shutdown()
    assert t.registry.active_conversation_ids() == [t.args[0]]
    assert t.registry.get_instance_lock(*t.args[:3]) is not None
    assert t.instance.running
    assert t.instance.private_dir.exists()
    assert t.process.poll() is None
    assert len(t.owner.record.terminals) == 1
    assert t.owner.record.terminals[0].tmux is None
    t.owner.finish()
    assert json.loads(t.owner.record_path.read_bytes())["state"] != "ended"


async def test_helper_transfer_cannot_escape_custody(owned_terminal) -> None:
    t = owned_terminal
    t.instance.name = "bash"
    t.instance.session_key = "helper"
    args = (t.args[0], "bash", "helper", t.args[3])
    await t.registry.launch(*args)
    before = t.owner.record_path.read_bytes()
    with pytest.raises(RuntimeError, match="Session custody refused: scope_mismatch"):
        t.registry.transfer(args[0], "conv_unmanaged", "bash", "helper")
    assert t.registry.get(*args[:3]) is t.instance
    assert t.registry.get("conv_unmanaged", "bash", "helper") is None
    assert t.owner.record_path.read_bytes() == before
    t.spawn.assert_not_awaited()


async def test_immediate_helper_exit_reaches_observation_cleanup(
    owned_terminal, monkeypatch
) -> None:
    from omnigent.runner.resource_registry import SessionResourceRegistry
    from omnigent.runner.tool_dispatch import _publish_terminal_created_event

    t = owned_terminal
    t.instance.name = "bash"
    t.instance.session_key = "proof"
    args = (t.args[0], "bash", "proof", t.args[3])
    await t.registry.launch(*args)
    t.process.terminate()
    t.process.wait(timeout=5)
    t.instance.socket_path.unlink()
    monkeypatch.setattr(t.instance, "is_alive", AsyncMock(return_value=False))
    assert t.registry.get(*args[:3]) is t.instance
    assert t.registry.list_for_conversation(args[0])[0].instance is t.instance
    resources = SessionResourceRegistry(terminal_registry=t.registry)
    await _publish_terminal_created_event(
        conversation_id=args[0],
        terminal_name="bash",
        session_key="proof",
        terminal_registry=t.registry,
        resource_registry=resources,
        publish_event=Mock(),
    )
    assert t.registry.get(*args[:3]) is None
    assert t.owner.record.terminals == []
    t.spawn.assert_awaited_once()
    t.owner.finish()
    assert json.loads(t.owner.record_path.read_bytes())["state"] == "ended"


@pytest.mark.parametrize("boundary", ["admission", "reattachment", "transfer", "end"])
async def test_foreign_terminal_socket_still_refuses_authoritative_operations(
    owned_terminal, boundary: str
) -> None:
    t = owned_terminal
    await t.registry.launch(*t.args)
    t.instance.socket_path.unlink()
    t.instance.socket_path.write_text("foreign")
    before = t.owner.record_path.read_bytes()
    # A custody-owned terminal never changes session scope, so transfer refuses
    # before socket inspection; every other boundary reaches the socket proof.
    expected = "scope_mismatch" if boundary == "transfer" else "foreign_socket"
    with pytest.raises((RuntimeError, custody.CustodyError), match=expected):
        if boundary == "admission":
            await t.registry.launch(*t.args)
        elif boundary == "reattachment":
            custody.session_contract(t.owner.directory.parent.parent, t.args[0])
        elif boundary == "transfer":
            t.registry.transfer(t.args[0], "conv_new", "codex", "main")
        else:
            custody.end_session(
                t.owner.directory.parent.parent,
                t.args[0],
                owner_generation=t.owner.record.owner_generation,
                host_id=t.owner.record.host_id,
                endpoint=t.owner.record.endpoint,
            )
    assert t.owner.record_path.read_bytes() == before
    t.spawn.assert_not_awaited()


@pytest.mark.parametrize("operation", ["close", "cleanup", "shutdown"])
@pytest.mark.parametrize("failure", ["eagain", "command", "timeout"])
async def test_ordinary_cleanup_retains_live_terminal_until_retry(
    owned_terminal, monkeypatch: pytest.MonkeyPatch, operation: str, failure: str
) -> None:
    t = owned_terminal
    monkeypatch.setattr(custody, "_current_custody", None)
    await t.registry.launch(*t.args)
    instance_lock = t.registry.get_instance_lock(*t.args[:3])
    environment = Mock()
    t.instance.os_env = environment
    if failure == "eagain":
        t.spawn.side_effect = OSError(errno.EAGAIN, "resource unavailable")
    elif failure == "command":
        t.spawn.return_value = SimpleNamespace(
            returncode=1, communicate=AsyncMock(return_value=(b"", b"permission denied"))
        )
    else:
        t.spawn.side_effect = asyncio.TimeoutError()
    if operation == "close":
        with pytest.raises((RuntimeError, asyncio.TimeoutError)):
            await t.registry.close(*t.args[:3])
    elif operation == "cleanup":
        await t.registry.cleanup_conversation(t.args[0])
    else:
        await t.registry.shutdown()
    assert t.registry.get(*t.args[:3]) is t.instance
    assert t.registry.get_instance_lock(*t.args[:3]) is instance_lock
    assert t.instance.running
    assert t.instance.private_dir.exists()
    assert t.instance.socket_path.exists()
    assert t.process.poll() is None
    environment.close.assert_not_called()

    async def killed():
        t.process.terminate()
        t.process.wait(timeout=5)
        return b"", b""

    t.spawn.side_effect = None
    t.spawn.return_value = SimpleNamespace(returncode=0, communicate=AsyncMock(side_effect=killed))
    assert await t.registry.close(*t.args[:3])
    assert t.registry.get(*t.args[:3]) is None
    assert t.registry.get_instance_lock(*t.args[:3]) is None
    assert not t.instance.running
    assert not t.instance.private_dir.exists()
    environment.close.assert_called_once()
