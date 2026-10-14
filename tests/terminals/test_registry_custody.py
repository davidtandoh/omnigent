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

from omnigent.errors import OmnigentError
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

    with pytest.raises((RuntimeError, OmnigentError)) as refused:
        await t.registry.launch(*t.args)
    unresolved = close_fails or failure not in {"publication", "verification"}
    if unresolved:
        assert isinstance(refused.value, OmnigentError)
        assert refused.value.code == "unavailable_probe"
    else:
        assert str(refused.value) == f"Session custody refused: {category}"
    assert t.registry.get(*t.args[:3]) is None
    assert t.registry.list_for_conversation(t.args[0]) == []
    assert t.registry.get_instance_lock(*t.args[:3]) is None
    t.spawn.assert_awaited_once()
    assert t.spawn.call_args.args[-1] == "kill-server"
    assert (t.process.poll() is None) == close_fails

    if unresolved:
        assert len(t.owner.record.terminals) == 1
        persisted = json.loads(t.owner.record_path.read_bytes())
        assert len(persisted["terminals"]) == 1
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
    with pytest.raises(OmnigentError, match="unavailable_probe"):
        if operation == "close":
            await t.registry.close(*t.args[:3])
        elif operation == "cleanup":
            await t.registry.cleanup_conversation(t.args[0])
        else:
            await t.registry.shutdown()
    assert t.registry.active_conversation_ids() == []
    assert t.registry.get_instance_lock(*t.args[:3]) is None
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
async def test_ordinary_cleanup_refuses_without_claiming_live_terminal(
    owned_terminal, monkeypatch: pytest.MonkeyPatch, operation: str, failure: str
) -> None:
    t = owned_terminal
    monkeypatch.setattr(custody, "_current_custody", None)
    await t.registry.launch(*t.args)
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
    with pytest.raises(OmnigentError):
        if operation == "close":
            await t.registry.close(*t.args[:3])
        elif operation == "cleanup":
            await t.registry.cleanup_conversation(t.args[0])
        else:
            await t.registry.shutdown()
    assert t.registry.get(*t.args[:3]) is None
    assert t.registry.get_instance_lock(*t.args[:3]) is None
    assert t.instance.running
    assert t.instance.private_dir.exists()
    assert t.instance.socket_path.exists()
    assert t.process.poll() is None
    environment.close.assert_not_called()


async def test_failed_start_preserves_resources_without_registration(
    owned_terminal, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.runner.resource_registry import SessionResourceRegistry

    t = owned_terminal
    monkeypatch.setattr(custody, "_current_custody", None)
    t.instance.keep_alive_after_exit = True
    environment = Mock()
    t.instance.os_env = environment

    async def exited() -> bool:
        t.instance.running = False
        return False

    monkeypatch.setattr(t.instance, "is_alive", exited)
    t.spawn.side_effect = OSError(errno.EAGAIN, "synthetic exhaustion")
    resources = SessionResourceRegistry(terminal_registry=t.registry)
    with pytest.raises(OmnigentError, match="unavailable_probe"):
        await resources.launch_required_terminal(*t.args)
    assert t.registry.active_conversation_ids() == []
    assert t.registry.get_instance_lock(*t.args[:3]) is None
    assert t.registry.list_for_conversation(t.args[0]) == []
    assert t.instance.private_dir.exists()
    assert t.instance.socket_path.exists()
    assert t.process.poll() is None
    environment.close.assert_not_called()
    t.create.assert_called_once()


@pytest.mark.parametrize("route", ["session", "resources"])
@pytest.mark.parametrize("durable", [False, True])
async def test_session_cleanup_retains_supporting_resources_until_terminal_exits(
    owned_terminal, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, route: str, durable: bool
) -> None:
    import shutil

    import httpx

    from omnigent.runner import app as runner_app
    from omnigent.runner.resource_registry import SessionResourceRegistry
    from tests.runner.helpers import NullServerClient

    t = owned_terminal
    if not durable:
        monkeypatch.setattr(custody, "_current_custody", None)
    await t.registry.launch(*t.args)
    before = t.owner.record_path.read_bytes()
    resources = SessionResourceRegistry(terminal_registry=t.registry)
    primary_dir = tmp_path / "primary"
    primary_dir.mkdir()
    (primary_dir / "workspace.txt").write_text("workspace")
    primary = Mock(close=Mock(side_effect=lambda: shutil.rmtree(primary_dir)))
    resources._primary_envs[t.args[0]] = primary
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "bridge.json").write_text("{}")

    async def delete_bridges(**_kwargs):
        shutil.rmtree(bridge_dir)

    cleanup_bridges = AsyncMock(side_effect=delete_bridges)
    monkeypatch.setattr(runner_app, "_delete_native_bridge_dirs", cleanup_bridges)
    manager = Mock(release=AsyncMock(), forward_cancel=AsyncMock())
    app = runner_app.create_runner_app(
        terminal_registry=t.registry,
        resource_registry=resources,
        process_manager=manager,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    path = f"/v1/sessions/{t.args[0]}" + ("/resources" if route == "resources" else "")
    skills_directory = tempfile.TemporaryDirectory(dir=tmp_path)
    with skills_directory as skills:
        resources._codex_skills_dirs[t.args[0]] = skills_directory
        skill = Path(skills) / "SKILL.md"
        skill.write_text("retained skill")
        t.spawn.side_effect = OSError(errno.EAGAIN, "synthetic resource exhaustion")
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            refused = await client.delete(path)
            assert refused.status_code == 500
            assert refused.json()["error"]["code"] == "unavailable_probe"
            assert "cleaned" not in refused.json()
            assert t.registry.get(*t.args[:3]) is None
            assert t.registry.get_instance_lock(*t.args[:3]) is None
            assert resources.has_primary_env(t.args[0])
            assert (primary_dir / "workspace.txt").exists()
            assert skill.exists()
            assert (bridge_dir / "bridge.json").exists()
            assert t.owner.record_path.read_bytes() == before
            assert t.instance.private_dir.exists()
            assert t.process.poll() is None
            primary.close.assert_not_called()
            cleanup_bridges.assert_not_awaited()
            manager.release.assert_not_awaited()


@pytest.mark.parametrize("caller", ["reaper", "reset", "claude-native", "cursor-native"])
async def test_native_cleanup_refusal_preserves_dependents_and_deletion_events(
    owned_terminal, monkeypatch: pytest.MonkeyPatch, caller: str
) -> None:
    import httpx

    from omnigent.errors import OmnigentError
    from omnigent.runner import app as runner_app
    from omnigent.runner import tool_dispatch
    from omnigent.runner.native import interrupt, orchestration
    from omnigent.runner.resource_registry import SessionResourceRegistry
    from omnigent.terminals.pane_reaper import PaneRef
    from tests.runner.helpers import NullServerClient
    from tests.runner.test_native_interrupt_runner import _make_runner

    t = owned_terminal
    await t.registry.launch(*t.args)
    receipt = t.owner.record_path.read_bytes()
    resources = SessionResourceRegistry(terminal_registry=t.registry)
    deleted = Mock()
    monkeypatch.setattr(tool_dispatch, "_publish_terminal_deleted_event", deleted)
    app = runner_app.create_runner_app(
        terminal_registry=t.registry,
        resource_registry=resources,
        server_client=NullServerClient(),
    )
    app_server = SimpleNamespace(listen_url="ws://runner.test/codex", close=AsyncMock())
    t.instance.args = ["--remote", app_server.listen_url]
    forwarder = asyncio.create_task(asyncio.Event().wait())
    monkeypatch.setitem(orchestration._AUTO_CODEX_APP_SERVERS, t.args[0], app_server)
    monkeypatch.setitem(orchestration._AUTO_FORWARDER_TASKS, t.args[0], forwarder)
    runner, captured = _make_runner(resource_registry=resources)
    if caller in {"claude-native", "cursor-native"}:
        import importlib

        bridge = importlib.import_module(f"omnigent.harnesses.{caller.replace('-', '_')}.bridge")
        monkeypatch.setattr(bridge, "kill_session", Mock())
        if caller == "claude-native":
            monkeypatch.setattr(
                interrupt, "_claude_native_bridge_id_for_session", AsyncMock(return_value="test")
            )
            monkeypatch.setattr(
                bridge, "bridge_dir_for_bridge_id", lambda _: t.instance.private_dir
            )
        else:
            monkeypatch.setattr(
                bridge, "bridge_dir_for_session_id", lambda _: t.instance.private_dir
            )

    async def cleanup() -> None:
        if caller == "reaper":
            await app.state.native_pane_reaper._reap(
                PaneRef(t.args[0], "terminal_codex_main", "codex", t.instance.socket_path)
            )
        elif caller == "reset":
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
                response = await client.post(f"/v1/sessions/{t.args[0]}/reset-state")
            if t.spawn.side_effect is not None:
                assert response.status_code == 500
                assert response.json()["error"]["code"] == "unavailable_probe"
                assert "reset" not in response.json()
            else:
                assert response.status_code == 200, response.text
        else:
            response = await runner.stop(caller, t.args[0])
            assert response.status_code == 204

    try:
        t.spawn.side_effect = OSError(errno.EAGAIN, "synthetic resource exhaustion")
        if caller == "reset":
            await cleanup()
        else:
            with pytest.raises(OmnigentError) as refusal:
                await cleanup()
            assert refusal.value.code == "unavailable_probe"
        assert t.registry.get(*t.args[:3]) is None
        assert t.registry.get_instance_lock(*t.args[:3]) is None
        assert t.owner.record_path.read_bytes() == receipt
        assert t.instance.private_dir.exists()
        assert t.process.poll() is None
        assert not forwarder.done()
        app_server.close.assert_not_awaited()
        deleted.assert_not_called()
        assert captured["published"] == []
        assert captured["wakes"] == []

    finally:
        forwarder.cancel()
        await asyncio.gather(forwarder, return_exceptions=True)


async def test_native_reaper_closes_healthy_session_after_unregistered_launch_failure(
    owned_terminal, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.runner import app as runner_app
    from omnigent.runner.resource_registry import (
        CODEX_NATIVE_TERMINAL_ROLE,
        SessionResourceRegistry,
    )
    from tests.runner.helpers import NullServerClient

    t = owned_terminal
    resources = SessionResourceRegistry(terminal_registry=t.registry)
    monkeypatch.setattr(
        t.instance, "is_alive", AsyncMock(side_effect=[True, asyncio.CancelledError])
    )
    t.spawn.side_effect = OSError(errno.EAGAIN, "synthetic resource exhaustion")
    with pytest.raises(OmnigentError, match="unavailable_probe"):
        await resources.launch_auxiliary_terminal(
            *t.args, resource_role=CODEX_NATIVE_TERMINAL_ROLE
        )
    assert t.registry.list_for_conversation(t.args[0]) == []
    receipt = t.owner.record_path.read_bytes()
    healthy = TerminalInstance("codex", "main", tmp_path / "healthy.sock", tmp_path, running=True)
    healthy.launch = AsyncMock()
    healthy.is_alive = AsyncMock(return_value=True)
    healthy.close = AsyncMock()
    t.create.return_value = TerminalCreateResult(healthy, tmp_path)
    await resources.launch_auxiliary_terminal(
        "conv_healthy", "codex", "main", t.args[3], resource_role=CODEX_NATIVE_TERMINAL_ROLE
    )
    app = runner_app.create_runner_app(
        terminal_registry=t.registry,
        resource_registry=resources,
        server_client=NullServerClient(),
    )
    reaper = app.state.native_pane_reaper
    reaper._is_busy = AsyncMock(return_value=False)
    reaper._idle_timeout_s = 0
    await reaper._scan_once()
    await reaper._scan_once()
    healthy.close.assert_awaited_once()
    assert t.registry.list_for_cleanup("conv_healthy") == []
    assert t.registry.get_instance_lock("conv_healthy", "codex", "main") is None
    assert t.registry.list_for_conversation(t.args[0]) == []
    assert t.registry.get_instance_lock(*t.args[:3]) is None
    assert t.owner.record_path.read_bytes() == receipt
    assert t.instance.private_dir.exists()
    assert t.process.poll() is None


@pytest.mark.parametrize("fails", [False, True])
async def test_observed_exit_publishes_only_after_proven_cleanup(
    owned_terminal, monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    from omnigent.errors import OmnigentError
    from omnigent.runner.resource_registry import SessionResourceRegistry, TerminalLifecycle

    t = owned_terminal
    monkeypatch.setattr(custody, "_current_custody", None)
    resources = SessionResourceRegistry(terminal_registry=t.registry)
    resources._start_terminal_activity_watcher = Mock()
    published = Mock()
    resources.set_terminal_exit_publisher(published)
    await resources.launch_required_terminal(*t.args)
    environment = Mock()
    t.instance.os_env = environment
    if fails:
        t.spawn.side_effect = OSError(errno.EAGAIN, "synthetic exhaustion")
    call = resources._handle_terminal_exit(
        session_id=t.args[0],
        terminal_name="codex",
        session_key="main",
        lifecycle=TerminalLifecycle.REQUIRED,
        instance=t.instance,
    )
    if fails:
        with pytest.raises(OmnigentError) as refused:
            await call
        assert refused.value.code == "unavailable_probe"
        published.assert_not_called()
        environment.close.assert_not_called()
        assert t.instance.private_dir.exists()
        assert t.process.poll() is None
    else:
        await call
        published.assert_called_once()
        environment.close.assert_called_once()
        assert t.process.poll() is not None
    assert t.registry.list_for_conversation(t.args[0]) == []
    assert t.registry.get_instance_lock(*t.args[:3]) is None
