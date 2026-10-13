"""Real Codex, tmux, host and API processes against a synthetic local model."""

from __future__ import annotations

import json
import shutil
import signal
import time
from pathlib import Path

import httpx
import psutil
import pytest
import yaml
from websockets.sync.client import connect

from omnigent._platform import IS_DARWIN
from omnigent.runner.custody import CustodyProcess, custody_records, session_contract
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests.e2e.conftest import configure_mock_llm, release_mock_gate, set_fallback_mock_llm
from tests.e2e.test_codex_native_terminal_recovery_e2e import _wait_for
from tests.e2e.test_host_codex_native_e2e import (
    _codex_native_agent_id,
    _online_host_id,
    _poll_for_assistant_marker,
    _poll_for_terminal_resource,
    _send_user_text,
)
from tests.e2e.test_local_server_lifecycle_e2e import (
    _find_free_port,
    _isolated_env,
    _Procs,
    _wait_for_pidfile_server,
)
from tests.e2e.test_pi_native_subagent_startup_lifecycle import _mcp
from tests.server.integration.test_sessions_policy_evaluate import _tool_call_request


@pytest.mark.skipif(
    not IS_DARWIN or shutil.which("codex") is None or shutil.which("tmux") is None,
    reason="requires the admitted macOS Codex and tmux platform",
)
@pytest.mark.timeout(300)
@pytest.mark.parametrize("outage_s", [0, 65])
def test_codex_inflight_turn_survives_host_death_and_single_adoption(
    tmp_path: Path, isolated_mock_llm_server_url: str, outage_s: int
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config_home = home / ".omnigent"
    config_home.mkdir()
    model = "mock-custody"
    config = {
        "providers": {
            "synthetic": {
                "kind": "gateway",
                "openai": {
                    "base_url": isolated_mock_llm_server_url + "/v1",
                    "api_key_ref": "env:OPENAI_API_KEY",
                    "wire_api": "responses",
                    "models": {"default": model},
                },
            }
        },
        "inference": {
            "harnesses": {"codex-native": {"provider": "synthetic", "default_model": model}}
        },
    }
    (config_home / "config.yaml").write_text(yaml.safe_dump(config))
    env = _isolated_env(home)
    for name in list(env):
        if name.startswith(("OTEL_", "MLFLOW_", "DATABRICKS_")):
            env.pop(name)
    port = _find_free_port()
    env.update(
        {
            "OMNIGENT_CONFIG_HOME": str(config_home),
            "OMNIGENT_DATA_DIR": str(config_home),
            "OMNIGENT_AUTH_PROVIDER": "header",
            "OMNIGENT_LOCAL_SINGLE_USER": "1",
            "OMNIGENT_LOCAL_SERVER_PORT": str(port),
            "OMNIGENT_SKIP_WEB_UI": "true",
            "OMNIGENT_SKIP_UPDATE_CHECK": "1",
            "CODEX_HOME": str(home / "codex"),
            "OPENAI_API_KEY": "mock-key",
            "OPENAI_BASE_URL": isolated_mock_llm_server_url + "/v1",
        }
    )
    procs = _Procs()
    procs.track_pidfile(config_home / "local_server.pid")
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30)
    try:
        server_log = tmp_path / "server.log"
        server = procs.spawn(["server"], env=env, cwd=home, log=server_log)
        _, port = _wait_for_pidfile_server(home, server, server_log)
        client.close()
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=30)
        env["OMNIGENT_LOCAL_SERVER_PORT"] = str(port)
        host = procs.spawn(
            [
                "host",
                "--server",
                str(client.base_url).rstrip("/"),
                "--no-open",
                "--non-interactive",
            ],
            env=env,
            cwd=home,
            log=tmp_path / "host.log",
        )
        host_id = _online_host_id(client, timeout=60)
        create = client.post(
            "/v1/sessions",
            json={
                "agent_id": _codex_native_agent_id(client),
                "host_id": host_id,
                "workspace": str(home),
            },
        )
        create.raise_for_status()
        session_id = create.json()["id"]
        _poll_for_terminal_resource(
            client, session_id=session_id, resource_id="terminal_codex_main", timeout=60
        )
        launched = _mcp(
            client, session_id, "sys_terminal_launch", {"terminal": "bash", "session": "proof"}
        )
        assert "error" not in json.loads(launched), launched
        policy = client.post(
            f"/v1/sessions/{session_id}/policies",
            json={
                "name": "custody-limit",
                "type": "python",
                "handler": "omnigent.policies.builtins.safety.max_tool_calls_per_session",
                "factory_params": {"limit": 100},
            },
        )
        policy.raise_for_status()
        assert (
            client.post(
                f"/v1/sessions/{session_id}/policies/evaluate", json=_tool_call_request("Read")
            ).json()["result"]
            == "POLICY_ACTION_ALLOW"
        )
        receipt = _wait_for(
            lambda: session_contract(config_home, session_id), "live custody receipt"
        )
        assert receipt["state"] == "live"
        assert "codex_app_server" in receipt["helpers"]
        native_before = receipt["helpers"]["codex_app_server"]
        terminal_before = receipt["terminals"]
        runner_before = receipt["owner"]
        from omnigent.native.owner_claim import ProcessIdentity

        native_process = ProcessIdentity(**native_before).require_live()
        assert "codex" in native_process.name().lower()
        assert "app-server" in native_process.cmdline()
        prompt = "Synthetic custody survival request"
        marker = "CUSTODY_TURN_CONTINUED"
        configure_mock_llm(
            isolated_mock_llm_server_url,
            [
                {
                    "block": True,
                    "tool_calls": [
                        {
                            "call_id": "custody_mcp_rename",
                            "namespace": "mcp__omnigent",
                            "name": "sys_session_rename",
                            "arguments": json.dumps({"title": "CUSTODY_NATIVE_MCP_OK"}),
                        }
                    ],
                },
                {"text": marker},
            ],
            match=prompt,
        )
        set_fallback_mock_llm(isolated_mock_llm_server_url, "default", marker)
        set_fallback_mock_llm(isolated_mock_llm_server_url, prompt, marker)
        _send_user_text(client, session_id=session_id, text=prompt)
        _wait_for(
            lambda: httpx.get(
                isolated_mock_llm_server_url + "/gate/pending", trust_env=False
            ).json()["pending"],
            "in-flight native model request",
        )
        captured = httpx.get(
            isolated_mock_llm_server_url + "/mock/requests", trust_env=False
        ).json()["requests"]
        resources_before = session_contract(config_home, session_id)["resources"]
        assert resources_before
        for request in captured:
            if isinstance(request.get("model"), str):
                set_fallback_mock_llm(isolated_mock_llm_server_url, request["model"], marker)
        (tmp_path / "mock-tool-shape.json").write_text(
            json.dumps(
                [
                    {
                        "type": tool.get("type"),
                        "name": tool.get("name"),
                        "namespace": tool.get("namespace"),
                        "children": [
                            {"type": child.get("type"), "name": child.get("name")}
                            for child in tool.get("tools", [])
                        ],
                    }
                    for request in captured
                    for tool in request.get("tools", [])
                ]
            )
        )
        host.send_signal(signal.SIGKILL)
        host.wait(timeout=10)
        replacement = procs.spawn(
            [
                "host",
                "--server",
                str(client.base_url).rstrip("/"),
                "--no-open",
                "--non-interactive",
            ],
            env=env,
            cwd=home,
            log=tmp_path / "replacement.log",
        )
        _online_host_id(client, timeout=60)

        def reattached() -> dict[str, object] | None:
            current = session_contract(config_home, session_id)
            attachment = current["reattachment"]
            if attachment and attachment["process"]["pid"] == replacement.pid:
                return current
            return None

        after = _wait_for(reattached, "reattached custody")
        assert after["owner"] == runner_before
        assert after["helpers"]["codex_app_server"] == native_before
        assert after["terminals"] == terminal_before
        assert after["reattachment"]["process"]["pid"] == replacement.pid
        assert len(custody_records(config_home)) == 1
        assert httpx.get(isolated_mock_llm_server_url + "/gate/pending", trust_env=False).json()[
            "pending"
        ]
        detached = procs.spawn(
            ["host", "maintenance-detach", "--server", str(client.base_url).rstrip("/")],
            env=env,
            cwd=home,
            log=tmp_path / "host-detach.log",
        )
        assert detached.wait(timeout=30) == 0, (tmp_path / "host-detach.log").read_text()
        replacement.wait(timeout=10)
        assert session_contract(config_home, session_id)["owner"] == runner_before
        replacement = procs.spawn(
            [
                "host",
                "--server",
                str(client.base_url).rstrip("/"),
                "--no-open",
                "--non-interactive",
            ],
            env=env,
            cwd=home,
            log=tmp_path / "reattachment.log",
        )
        second_attach = _wait_for(reattached, "maintenance host reattachment")
        assert second_attach["owner"] == runner_before
        assert second_attach["helpers"]["codex_app_server"] == native_before
        assert second_attach["resources"] == resources_before
        assert second_attach["reattachment"]["epoch"] == after["reattachment"]["epoch"] + 1
        maintenance = procs.spawn(
            ["server", "maintenance-stop", "--json"],
            env=env,
            cwd=home,
            log=tmp_path / "maintenance-stop.log",
        )
        assert maintenance.wait(timeout=30) == 0, (tmp_path / "maintenance-stop.log").read_text()
        outage_started = time.monotonic()
        while time.monotonic() - outage_started < outage_s:
            assert replacement.poll() is None
            during = session_contract(config_home, session_id)
            assert during["owner"] == runner_before
            assert during["helpers"]["codex_app_server"] == native_before
            assert during["terminals"] == terminal_before
            time.sleep(2)
        restart = procs.spawn(
            ["server", "maintenance-start", "--json"],
            env=env,
            cwd=home,
            log=tmp_path / "maintenance-start.log",
        )
        assert restart.wait(timeout=30) == 0, (tmp_path / "maintenance-start.log").read_text()
        _online_host_id(client, timeout=60)
        _poll_for_terminal_resource(
            client, session_id=session_id, resource_id="terminal_codex_main", timeout=60
        )
        terminal_ws = (
            str(client.base_url).rstrip("/").replace("http://", "ws://")
            + f"/v1/sessions/{session_id}/resources/terminals/terminal_bash_proof/attach"
        )
        command = b"printf 'phone-once\\n' >> phone-proof.txt; printf 'PHONE_INPUT_OK\\n'\n"
        with connect(terminal_ws, origin=OMNIGENT_INTERNAL_WS_ORIGIN, open_timeout=10) as phone:
            phone.recv(timeout=10)
            phone.send(command)
            output = b""
            while b"PHONE_INPUT_OK" not in output:
                frame = phone.recv(timeout=10)
                output += frame.encode() if isinstance(frame, str) else frame
        assert _wait_for(lambda: (home / "phone-proof.txt").exists(), "phone input executed")
        assert (home / "phone-proof.txt").read_text() == "phone-once\n"
        with connect(terminal_ws, origin=OMNIGENT_INTERNAL_WS_ORIGIN, open_timeout=10) as phone:
            phone.recv(timeout=10)
        assert (home / "phone-proof.txt").read_text() == "phone-once\n"
        assert "error" not in json.loads(
            _mcp(client, session_id, "sys_terminal_read", {"terminal": "bash", "session": "proof"})
        )
        assert (
            client.post(
                f"/v1/sessions/{session_id}/policies/evaluate", json=_tool_call_request("Read")
            ).json()["result"]
            == "POLICY_ACTION_ALLOW"
        )
        release_mock_gate(isolated_mock_llm_server_url)
        _poll_for_assistant_marker(client, session_id=session_id, marker=marker, timeout=60)
        assert session_contract(config_home, session_id)["owner"] == runner_before
        snapshots = client.get(
            f"/v1/sessions/{session_id}/items", params={"order": "asc", "limit": 100}
        ).json()["data"]
        assert (
            sum(prompt in json.dumps(item) for item in snapshots if item.get("role") == "user")
            == 1
        )
        native_requests = httpx.get(
            isolated_mock_llm_server_url + "/mock/requests", trust_env=False
        ).json()["requests"]
        native_results = [
            item
            for request in native_requests
            for item in request.get("input", [])
            if isinstance(item, dict)
            and item.get("type") == "function_call_output"
            and item.get("call_id") == "custody_mcp_rename"
        ]
        assert native_results and any(
            "CUSTODY_NATIVE_MCP_OK" in json.dumps(item.get("output")) for item in native_results
        ), "retained native MCP did not complete the framework-approved rename"
        client.post(
            f"/v1/sessions/{session_id}/policies",
            json={
                "name": "custody-deny",
                "type": "python",
                "handler": "omnigent.policies.builtins.safety.max_tool_calls_per_session",
                "factory_params": {"limit": 0},
            },
        ).raise_for_status()
        assert (
            client.post(
                f"/v1/sessions/{session_id}/policies/evaluate", json=_tool_call_request("Read")
            ).json()["result"]
            == "POLICY_ACTION_DENY"
        )
        denied_prompt = prompt + " denied"
        denied_marker = "CUSTODY_NATIVE_POLICY_DENIED"
        configure_mock_llm(
            isolated_mock_llm_server_url,
            [
                {
                    "tool_calls": [
                        {
                            "call_id": "custody_mcp_deny",
                            "namespace": "mcp__omnigent",
                            "name": "sys_session_rename",
                            "arguments": json.dumps({"title": "UNAUTHORIZED_CUSTODY_RENAME"}),
                        }
                    ]
                },
                {"text": denied_marker},
            ],
            match=denied_prompt,
        )
        set_fallback_mock_llm(isolated_mock_llm_server_url, denied_prompt, denied_marker)
        _send_user_text(client, session_id=session_id, text=denied_prompt)
        _poll_for_assistant_marker(client, session_id=session_id, marker=denied_marker, timeout=60)
        assert client.get(f"/v1/sessions/{session_id}").json()["title"] == "CUSTODY_NATIVE_MCP_OK"
        deny_requests = httpx.get(
            isolated_mock_llm_server_url + "/mock/requests", trust_env=False
        ).json()["requests"]
        assert any(
            "Exceeded 0 tool calls" in json.dumps(item.get("output"))
            for request in deny_requests
            for item in request.get("input", [])
            if isinstance(item, dict) and item.get("call_id") == "custody_mcp_deny"
        )
        end = procs.spawn(
            [
                "runtime",
                "end-session",
                "--session",
                session_id,
                "--owner-generation",
                receipt["owner_generation"],
                "--host-id",
                host_id,
                "--server",
                str(client.base_url).rstrip("/"),
                "--json",
            ],
            env=env,
            cwd=home,
            log=tmp_path / "end-session.log",
        )
        assert end.wait(timeout=30) == 0, (tmp_path / "end-session.log").read_text()
        assert session_contract(config_home, session_id)["state"] == "ended"
        with pytest.raises((ValueError, psutil.NoSuchProcess)):
            ProcessIdentity(**native_before).require_live()
        for terminal in terminal_before:
            _wait_for(
                lambda terminal=terminal: not Path(terminal["socket"]).exists(),
                "intentional terminal teardown",
            )
    finally:
        release_mock_gate(isolated_mock_llm_server_url)
        try:
            for record in custody_records(config_home):
                process = CustodyProcess(record)
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
        finally:
            client.close()
            procs.teardown()
