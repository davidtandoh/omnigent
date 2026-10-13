"""Public Codex bindings refuse relaunch and preserve the exact live owner."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from click.testing import CliRunner

from omnigent import cli
from omnigent.harnesses.codex_native import main as codex
from omnigent.runner import custody
from tests.runner.test_session_custody import context


@pytest.fixture
def bound_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from omnigent import _startup_events

    monkeypatch.setattr(_startup_events._logger, "propagate", False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(custody, "SUPPORTED_HARNESSES", frozenset({"codex-native"}))
    forbidden = Mock(side_effect=AssertionError("reattachment must not start a backend"))
    monkeypatch.setattr(cli, "_ensure_backend", forbidden)
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="binding-") as short:
        path = Path(short) / "native.sock"
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(path))
            with custody.SessionCustody.create(
                tmp_path, {**context(), "config_digest": custody.host_config_digest()}
            ) as owner:
                owner.publish_helper("codex_app_server", os.getpid())
                owner.publish_terminal("codex", "main", path, os.getpid(), os.getpid())
                binding = custody.session_binding(tmp_path, owner.record.session_id)
                binding_path = tmp_path / "binding.json"
                binding_path.write_text(binding.model_dump_json())
                yield owner, binding, binding_path
    forbidden.assert_not_called()


@pytest.mark.parametrize(
    "field",
    [
        "owner_generation",
        "runner_id",
        "host_id",
        "endpoint",
        "workspace",
        "config_digest",
        "environment_digest",
    ],
)
def test_cli_reattachment_refuses_expected_identity_mismatch_without_mutation(
    bound_session, field: str
) -> None:
    owner, binding, path = bound_session
    path.write_text(binding.model_copy(update={field: "foreign"}).model_dump_json())
    before = owner.record_path.read_bytes()
    result = CliRunner().invoke(
        cli.cli, ["codex", "--reattach-binding", str(path), "--binding-json"]
    )
    assert result.exit_code == 1, result.output
    assert json.loads(result.stdout)["error"]["category"] in {
        "scope_mismatch",
        "config_mismatch",
        "binding_mismatch",
    }
    assert owner.record_path.read_bytes() == before
    assert owner.still_owner()


def test_cli_binding_reuses_typed_terminal_without_launch(
    bound_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner, binding, path = bound_session
    attach = AsyncMock()
    monkeypatch.setattr(codex, "_attach_terminal_resource", attach)
    for _ in range(2):
        result = CliRunner().invoke(cli.cli, ["codex", "--reattach-binding", str(path)])
        assert result.exit_code == 0, result.output
    assert attach.await_count == 2
    prepared = attach.call_args.kwargs["prepared"]
    assert str(prepared.tmux_socket) == binding.terminal.socket
    assert prepared.session_id == binding.session_id and prepared.reattached
    assert custody.session_binding(owner.directory.parent.parent, binding.session_id) == binding


@pytest.mark.parametrize("state", ["missing", "stale", "ended", "duplicate", "malformed"])
def test_cli_binding_refuses_unavailable_owner_without_fallback(bound_session, state: str) -> None:
    owner, binding, path = bound_session
    if state == "missing":
        path.write_text(binding.model_copy(update={"session_id": "absent"}).model_dump_json())
    elif state == "stale":
        owner._lock.release()
    elif state == "ended":
        owner.finish()
    elif state == "duplicate":
        owner.record.terminals.append(binding.terminal)
        owner._publish()
    else:
        path.write_text('{"schema_version":1,"unexpected":true}')
    before = owner.record_path.read_bytes()
    result = CliRunner().invoke(
        cli.cli, ["codex", "--reattach-binding", str(path), "--binding-json"]
    )
    assert result.exit_code == 1, result.output
    assert "error" in json.loads(result.stdout)
    assert owner.record_path.read_bytes() == before


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS is the admitted custody platform")
def test_concurrent_executable_binding_requests_preserve_one_owner(bound_session) -> None:
    owner, binding, path = bound_session
    before = owner.record_path.read_bytes()

    def request():
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "omnigent.cli",
                "codex",
                "--reattach-binding",
                str(path),
                "--binding-json",
            ],
            env=dict(os.environ),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: request(), range(2)))
    for result in results:
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == binding.model_dump(mode="json")
    assert owner.record_path.read_bytes() == before
    assert owner.still_owner()
    path.write_text(binding.model_copy(update={"owner_generation": "foreign"}).model_dump_json())
    refused = request()
    assert refused.returncode == 1
    assert json.loads(refused.stdout)["error"]["category"] == "binding_mismatch"
    assert owner.record_path.read_bytes() == before


@pytest.mark.asyncio
async def test_terminal_transfer_retires_binding_and_preserves_native_owner(
    bound_session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from types import SimpleNamespace

    import httpx

    from omnigent.harnesses.codex_native.forwarder import _create_thread_replacement_session
    from omnigent.host.frames import HostStopRunnerFrame, decode_host_frame
    from omnigent.host.identity import HostIdentity
    from omnigent.runner import create_runner_app
    from omnigent.runner.resource_registry import SessionResourceRegistry
    from omnigent.server.routes import sessions
    from omnigent.server.routes._sessions.orchestration import _archive_stop
    from omnigent.terminals import TerminalRegistry
    from tests.host.test_connect import _cleanup_host, _make_host_process
    from tests.runner.helpers import NullServerClient, make_test_terminal_instance

    owner, binding, path = bound_session
    monkeypatch.setattr(custody, "_current_custody", owner)
    registry = TerminalRegistry()
    terminal = make_test_terminal_instance("codex", "main", tmp_path)
    terminal.socket_path = Path(binding.terminal.socket)
    monkeypatch.setattr(terminal, "set_conversation_link", AsyncMock())
    registry._by_conversation[binding.session_id] = {("codex", "main"): terminal}
    app = create_runner_app(
        server_client=NullServerClient(),
        resource_registry=SessionResourceRegistry(terminal_registry=registry),
    )
    host = _make_host_process()
    host._identity = HostIdentity(host_id=binding.host_id, name="Synthetic")
    host._server_url = binding.endpoint
    monkeypatch.setattr("omnigent.host.connect.host_config_digest", lambda: binding.config_digest)
    signals = Mock(side_effect=AssertionError("retired scope must not signal successor"))
    monkeypatch.setattr(custody.CustodyProcess, "terminate", signals)
    host._adopt_custodied_runners()
    original_handle = host._runners[binding.runner_id]
    before = owner.record.model_copy(deep=True)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runner"
        ) as client:

            async def server_request(request):
                if request.method == "GET":
                    return httpx.Response(
                        200, json={"agent_id": "codex", "runner_id": binding.runner_id}
                    )
                if request.url.path == "/v1/sessions":
                    return httpx.Response(200, json={"id": "conv_successor"})
                if request.url.path.endswith("/transfer"):
                    return await client.post(request.url.path, json=json.loads(request.content))
                if request.method == "PATCH" and request.url.path.endswith(binding.session_id):
                    raise httpx.ConnectError(
                        "API maintenance interrupted clear-binding", request=request
                    )
                return httpx.Response(200, json={})

            bridge_dir = tmp_path / "codex-bridge"
            bridge_dir.mkdir()
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(server_request), base_url="http://api"
            ) as api:
                with pytest.raises(httpx.ConnectError, match="maintenance interrupted"):
                    await _create_thread_replacement_session(
                        client=api,
                        old_session_id=binding.session_id,
                        bridge_dir=bridge_dir,
                        app_server_url="ws://127.0.0.1:8124",
                        new_thread_id="successor-thread",
                    )
        assert registry.get(binding.session_id, "codex", "main") is None
        assert registry.get("conv_successor", "codex", "main") is terminal
        assert owner.still_owner()
        successor = custody.session_binding(tmp_path, "conv_successor")
        assert successor == binding.model_copy(update={"session_id": "conv_successor"})
        assert owner.record.helpers == before.helpers
        assert owner.record.terminals == before.terminals
        for command in (
            ["codex", "--reattach-binding", str(path), "--binding-json"],
            [
                "runtime",
                "end-session",
                "--session",
                binding.session_id,
                "--owner-generation",
                binding.owner_generation,
                "--host-id",
                binding.host_id,
                "--server",
                binding.endpoint,
                "--json",
            ],
        ):
            result = await asyncio.to_thread(CliRunner().invoke, cli.cli, command)
            assert result.exit_code == 1, result.output
            assert json.loads(result.stdout)["error"]["category"] == "scope_mismatch"
        connection = SimpleNamespace(pending_stops={})
        stop_frames = []

        async def deliver_stop(payload):
            frame = decode_host_frame(payload)
            stop_frames.append(frame)
            assert isinstance(frame, HostStopRunnerFrame)
            assert frame.session_id == binding.session_id
            result = await host._handle_stop(frame)
            assert result.status == "failed" and result.error == "scope_mismatch"
            connection.pending_stops.pop(frame.request_id).set_result(
                {"status": result.status, "error": result.error}
            )

        host_registry = SimpleNamespace(
            get=lambda host_id: connection,
            send_text=lambda conn, payload: asyncio.create_task(deliver_stop(payload)),
        )
        assert not await sessions._stop_session_host_runner(
            binding.session_id,
            binding.host_id,
            binding.runner_id,
            host_registry,
        )
        monkeypatch.setattr(sessions, "_best_effort_stop", AsyncMock())
        store = SimpleNamespace(
            get_conversation=lambda session_id: SimpleNamespace(
                archived=True,
                host_id=binding.host_id,
                runner_id=binding.runner_id,
            )
        )
        await _archive_stop(binding.session_id, store, None, host_registry)
        assert len(stop_frames) == 2
        assert host._runners[binding.runner_id] is original_handle
        assert not original_handle.stop_requested
        signals.assert_not_called()
        path.write_text(successor.model_dump_json())
        result = await asyncio.to_thread(
            CliRunner().invoke,
            cli.cli,
            ["codex", "--reattach-binding", str(path), "--binding-json"],
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == successor.model_dump(mode="json")
        host._adopt_custodied_runners()
        assert host._runners[binding.runner_id] is original_handle
        assert original_handle.session_id == "conv_successor"
        assert set(host._custody_attachments) == {"conv_successor"}
        host._cleanup_runners()
        host._adopt_custodied_runners()
        assert host._runners[binding.runner_id].session_id == "conv_successor"
        assert host._runners[binding.runner_id].proc.identity == binding.owner
        assert [r.session_id for r in custody.custody_records(tmp_path)] == ["conv_successor"]

        stopped = []

        def stop_current(proc):
            with pytest.raises(custody.CustodyError, match="owner_busy"):
                owner.transfer_session(
                    "conv_successor", "conv_third", Path(binding.terminal.socket)
                )
            stopped.append(proc.identity)

        monkeypatch.setattr(host, "_stop_runner_proc", stop_current)
        result = await host._handle_stop(
            HostStopRunnerFrame(
                request_id="current-stop",
                runner_id=binding.runner_id,
                session_id="conv_successor",
            )
        )
        assert result.status == "stopped"
        assert stopped == [binding.owner]
        assert binding.runner_id not in host._runners
    finally:
        host._cleanup_runners()
        _cleanup_host(host)


def test_terminal_transfer_refuses_existing_successor_without_retiring_source(
    bound_session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.terminals import TerminalRegistry
    from tests.runner.helpers import make_test_terminal_instance

    owner, binding, _ = bound_session
    monkeypatch.setattr(custody, "_current_custody", owner)
    registry = TerminalRegistry()
    terminal = make_test_terminal_instance("codex", "main", tmp_path)
    terminal.socket_path = Path(binding.terminal.socket)
    registry._by_conversation[binding.session_id] = {("codex", "main"): terminal}
    with custody.SessionCustody.create(tmp_path, {**context(), "session_id": "occupied"}) as other:
        before = owner.record_path.read_bytes()
        other_before = other.record_path.read_bytes()
        with pytest.raises(RuntimeError, match="owner_busy"):
            registry.transfer(binding.session_id, "occupied", "codex", "main")
        assert registry.get(binding.session_id, "codex", "main") is terminal
        assert registry.get("occupied", "codex", "main") is None
        assert owner.record_path.read_bytes() == before
        assert other.record_path.read_bytes() == other_before
        assert owner.still_owner()


@pytest.mark.asyncio
async def test_shared_bridge_handoff_retires_only_superseded_receipt(
    bound_session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import httpx

    from omnigent.harnesses.claude_native import bridge
    from tests.runner.test_comment_relay import (
        _launch_bridged,
        _spec_without_terminals,
        _switch_app,
        _SwitchableServerClient,
    )

    owner, binding, _ = bound_session
    monkeypatch.setattr(custody, "_current_custody", owner)
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "bridges")
    monkeypatch.setattr(bridge, "post_tools_changed", lambda *args, **kwargs: None)
    bridge_dir = bridge.prepare_bridge_dir("shared", workspace=tmp_path)
    server = _SwitchableServerClient("agent", bridge_id="shared")
    app = _switch_app(tmp_path, server, {"agent": _spec_without_terminals()})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        try:
            await _launch_bridged(client, binding.session_id)
            source_name = f"tool_policy_bridge:{binding.session_id}"
            previous = owner.record.resources[source_name]
            owner.transfer_session(
                binding.session_id, "conv_successor", Path(binding.terminal.socket)
            )
            await _launch_bridged(client, "conv_successor")
            target_name = "tool_policy_bridge:conv_successor"
            assert source_name not in owner.record.resources
            successor = owner.record.resources[target_name]
            assert successor.path == previous.path and successor.digest != previous.digest
            custody.verify_resources(owner.record)
            before = (bridge_dir / "tool_relay.json").read_bytes()
            assert json.loads(before)["session_id"] == "conv_successor"
            owner.retire_resource(source_name, previous)
            owner.retire_resource(target_name, previous)
            for method, suffix in (
                ("DELETE", ""),
                ("DELETE", "/resources"),
                ("POST", "/reset-state"),
            ):
                response = await client.request(
                    method, f"/v1/sessions/{binding.session_id}{suffix}"
                )
                assert response.status_code == 409
                assert response.json()["detail"] == "scope_mismatch"
            assert owner.record.resources[target_name] == successor
            assert (bridge_dir / "tool_relay.json").read_bytes() == before
            custody.verify_resources(owner.record)
        finally:
            await client.delete("/v1/sessions/conv_successor")
