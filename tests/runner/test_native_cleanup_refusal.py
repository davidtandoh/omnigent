from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from omnigent.harnesses.codex_native import bridge as codex_bridge
from omnigent.harnesses.codex_native.forwarder import _create_thread_replacement_session
from omnigent.runner.native import orchestration
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals.registry import TerminalCleanupRefused, TerminalRegistry
from tests.runner.conftest import _build_app_for_spec, _runner_client
from tests.runner.helpers import make_test_terminal_instance


@pytest.mark.parametrize("lost_response", [False, True])
async def test_delete_source_requires_transferred_terminal_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lost_response: bool
) -> None:
    source, successor = "conv_source", "conv_successor"
    url = "ws://runner.test/codex"
    bridge_dir = tmp_path / source
    bridge_dir.mkdir()
    codex_bridge.write_bridge_state(
        bridge_dir,
        codex_bridge.CodexNativeBridgeState(
            session_id=source,
            socket_path=url,
            thread_id="thread_old",
            codex_home=str(tmp_path / "home"),
        ),
    )
    monkeypatch.setattr(codex_bridge, "bridge_dir_for_bridge_id", lambda _sid: bridge_dir)
    registry = TerminalRegistry()
    terminal = make_test_terminal_instance("codex", "main", tmp_path)
    terminal.args = ["--remote", url]
    helper = make_test_terminal_instance("bash", "helper", tmp_path)
    monkeypatch.setattr(terminal, "_tmux", AsyncMock())
    monkeypatch.setattr(helper, "_tmux", AsyncMock())
    registry._by_conversation[source] = {
        ("codex", "main"): terminal,
        ("bash", "helper"): helper,
    }
    server = SimpleNamespace(listen_url=url, close=AsyncMock())
    monkeypatch.setattr(orchestration, "_AUTO_CODEX_APP_SERVERS", {source: server})
    monkeypatch.setattr(orchestration, "_AUTO_FORWARDER_TASKS", {})
    routers = [AsyncMock(), AsyncMock()]
    monkeypatch.setattr(orchestration, "_shutdown_session_router_async", routers[0])
    monkeypatch.setattr(orchestration, "_shutdown_session_turn_router_async", routers[1])

    async def response(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"agent_id": "agent", "runner_id": "runner"})
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"id": successor})
        if request.url.path.endswith("/transfer"):
            assert registry.transfer(source, successor, "codex", "main")
            raise httpx.ReadTimeout("applied transfer; lost response", request=request)
        return httpx.Response(200, json={})

    if lost_response:
        async with httpx.AsyncClient(
            base_url="http://server", transport=httpx.MockTransport(response)
        ) as client:
            with pytest.raises(httpx.ReadTimeout):
                await _create_thread_replacement_session(
                    client=client,
                    old_session_id=source,
                    bridge_dir=bridge_dir,
                    app_server_url=url,
                    new_thread_id="thread_new",
                )
        assert codex_bridge.read_bridge_state(bridge_dir).session_id == source

    spec = AgentSpec(spec_version=1, name="test", executor=ExecutorSpec(type="omnigent"))
    app, _ = await _build_app_for_spec(spec, terminal_registry=registry)
    monkeypatch.setattr(orchestration, "_delete_native_bridge_dirs", AsyncMock())
    async with _runner_client(app) as client:
        result = await client.delete(f"/v1/sessions/{source}")
        if lost_response:
            assert result.status_code >= 400, result.text
            assert "process_pending" in result.text
            assert terminal.running
            assert not helper.running
            assert registry.get(successor, "codex", "main") is terminal
            server.close.assert_not_awaited()
            for router in routers:
                router.assert_not_awaited()
            assert orchestration._AUTO_CODEX_APP_SERVERS[source] is server
            result = await client.delete(f"/v1/sessions/{successor}")
        assert result.status_code == 200, result.text
    server.close.assert_awaited_once()
    for router in routers:
        router.assert_awaited_once_with(source)
    assert not terminal.running
    assert not orchestration._AUTO_CODEX_APP_SERVERS


@pytest.mark.parametrize("cleanup_fails", [False, True])
@pytest.mark.parametrize("cancel_stage", ["launch", "observation"])
async def test_opencode_launch_cancellation_preserves_refused_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_fails: bool,
    cancel_stage: str,
) -> None:
    from omnigent.harnesses.opencode_native import app_server, bridge, forwarder, provider
    from omnigent.inner.terminal import TerminalCreateResult, TerminalInstance
    from omnigent.terminals import registry as registry_mod

    session_id = "conv_opencode"
    monkeypatch.setattr(orchestration, "_AUTO_OPENCODE_SERVERS", {})
    monkeypatch.setattr(orchestration, "_AUTO_FORWARDER_TASKS", {})
    monkeypatch.setattr(orchestration, "_AUTO_CODEX_APP_SERVERS", {})
    monkeypatch.setattr(
        orchestration,
        "_opencode_native_launch_config",
        AsyncMock(
            return_value=SimpleNamespace(
                workspace=tmp_path,
                model_override=None,
                terminal_launch_args=None,
                external_session_id=None,
                fork_carry_history=False,
            )
        ),
    )
    monkeypatch.setattr(bridge, "prepare_bridge_dir", lambda _sid: tmp_path)
    monkeypatch.setattr(bridge, "seed_opencode_auth", lambda _dir: None)
    monkeypatch.setattr(provider, "resolve_bound_opencode_gateway", lambda **_kw: None)
    monkeypatch.setattr(provider, "resolve_databricks_gateway", lambda *_a, **_kw: None)
    monkeypatch.setattr(provider, "managed_connect_opencode_config", lambda *_a: None)
    monkeypatch.setattr(provider, "maybe_merge_user_provider_config", lambda config: config)
    monkeypatch.delenv("RUNNER_SERVER_URL", raising=False)
    server = SimpleNamespace(
        start=AsyncMock(),
        close=AsyncMock(),
        opencode_path="opencode",
        base_url="http://runner.test/opencode",
        auth_secret="synthetic",
        xdg_data_home=tmp_path / "data",
        xdg_config_home=tmp_path / "config",
    )
    server.client = lambda: SimpleNamespace(
        create_session=AsyncMock(return_value=SimpleNamespace(id="ses_native")),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(app_server, "OpenCodeNativeServer", lambda **_kw: server)
    monkeypatch.setattr(app_server, "opencode_terminal_env", lambda _server: {})
    forwarder_started = asyncio.Event()
    forwarder_tasks = []

    async def forward() -> None:
        forwarder_tasks.append(asyncio.current_task())
        forwarder_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        forwarder, "OpenCodeNativeForwarder", lambda **_kw: SimpleNamespace(run=forward)
    )
    private_dir = tmp_path / "terminal"
    private_dir.mkdir()
    instance = TerminalInstance("opencode", "main", private_dir / "tmux.sock", private_dir)
    cancel_ready = asyncio.Event()

    async def launch(**_kw) -> None:
        instance.running = True
        await forwarder_started.wait()
        if cancel_stage == "launch":
            cancel_ready.set()
            await asyncio.Event().wait()

    probes = 0

    async def alive() -> bool:
        nonlocal probes
        probes += 1
        if probes > 1:
            cancel_ready.set()
            await asyncio.Event().wait()
        return True

    async def kill(*_args) -> None:
        if cleanup_fails:
            raise OSError("synthetic kill-server failure")

    monkeypatch.setattr(instance, "launch", launch)
    monkeypatch.setattr(instance, "is_alive", alive)
    monkeypatch.setattr(instance, "_tmux", kill)
    monkeypatch.setattr(
        registry_mod,
        "create_terminal_instance",
        Mock(return_value=TerminalCreateResult(instance, tmp_path)),
    )
    registry = TerminalRegistry()
    resources = SessionResourceRegistry(terminal_registry=registry)
    published = Mock()
    task = asyncio.create_task(
        orchestration._auto_create_opencode_terminal(
            session_id,
            resources,
            published,
            server_client=SimpleNamespace(patch=AsyncMock()),
        )
    )
    await asyncio.wait_for(cancel_ready.wait(), timeout=5)
    task.cancel()
    with pytest.raises(
        TerminalCleanupRefused if cleanup_fails else asyncio.CancelledError
    ) as raised:
        await task
    assert not registry.list_for_cleanup(session_id)
    assert not published.called
    assert forwarder_tasks[0].cancelled()
    if cleanup_fails:
        assert raised.value.code == "unavailable_probe"
        with pytest.raises(asyncio.CancelledError) as cancelled:
            forwarder_tasks[0].result()
        assert cancelled.value.args[0] is raised.value
        assert instance.running
        assert private_dir.exists()
        assert orchestration._AUTO_OPENCODE_SERVERS[session_id] is server
        server.close.assert_not_awaited()
    else:
        assert not instance.running
        assert not private_dir.exists()
        assert not orchestration._AUTO_OPENCODE_SERVERS
        server.close.assert_awaited()


@pytest.mark.parametrize("terminal_state", ["empty", "live", "cleanup_refused"])
async def test_reset_state_returns_cleanup_evidence_from_bound_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal_state: str
) -> None:
    from omnigent.runner import app as runner_app

    session_id = "conv_reset_cleanup"
    registry = TerminalRegistry()
    server = SimpleNamespace(listen_url="ws://runner.test/reset", close=AsyncMock())
    servers = {} if terminal_state == "empty" else {session_id: server}
    monkeypatch.setattr(orchestration, "_AUTO_CODEX_APP_SERVERS", servers)
    monkeypatch.setattr(orchestration, "_AUTO_FORWARDER_TASKS", {})
    monkeypatch.setattr(runner_app, "_session_agent_ids_ref", {})
    routers = [AsyncMock(), AsyncMock()]
    monkeypatch.setattr(orchestration, "_shutdown_session_router_async", routers[0])
    monkeypatch.setattr(orchestration, "_shutdown_session_turn_router_async", routers[1])
    instance = None
    if terminal_state != "empty":
        instance = make_test_terminal_instance("codex", "main", tmp_path)
        instance.args = ["--remote", server.listen_url]
        monkeypatch.setattr(
            instance,
            "_tmux",
            AsyncMock(
                side_effect=OSError("kill failed") if terminal_state == "cleanup_refused" else None
            ),
        )
        registry._by_conversation[session_id] = {("codex", "main"): instance}
    spec = AgentSpec(spec_version=1, name="test", executor=ExecutorSpec(type="omnigent"))
    app, pm = await _build_app_for_spec(spec, terminal_registry=registry)
    runner_app._session_agent_ids_ref[session_id] = "agent_before_reset"
    assert runner_app.get_session_agent_id(session_id) == "agent_before_reset"
    async with _runner_client(app) as client:
        response = await client.post(f"/v1/sessions/{session_id}/reset-state")
    assert registry.list_for_cleanup(session_id) == []
    if terminal_state == "cleanup_refused":
        assert response.status_code >= 400, response.text
        assert "unavailable_probe" in response.text
        assert instance.running
        assert runner_app.get_session_agent_id(session_id) == "agent_before_reset"
        assert session_id not in pm.released
        assert servers[session_id] is server
        server.close.assert_not_awaited()
        for router in routers:
            router.assert_not_awaited()
    else:
        assert response.status_code == 200, response.text
        assert response.json() == {
            "session_id": session_id,
            "object": "session.state_reset",
            "reset": True,
        }
        assert runner_app.get_session_agent_id(session_id) is None
        assert session_id in pm.released
        if instance is not None:
            assert not instance.running
            server.close.assert_awaited_once()
            for router in routers:
                router.assert_awaited_once_with(session_id)
        assert not servers
