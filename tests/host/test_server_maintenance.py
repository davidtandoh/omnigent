"""Maintenance affects only the exactly recorded API process."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from omnigent import cli as cli_module
from omnigent.host import local_server
from omnigent.native.owner_claim import ProcessIdentity


@pytest.fixture(autouse=True)
def isolated_maintenance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "header")
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "0")
    monkeypatch.delenv("OMNIGENT_CONFIG", raising=False)
    monkeypatch.delenv("OMNIGENT_WEB_BASE_PATH", raising=False)
    monkeypatch.delenv("OMNIGENT_LOCAL_SERVER_PORT", raising=False)
    for name in ("PID", "SIG", "LOG_REF", "OWNER", "MAINTENANCE", "BASE_PATH"):
        monkeypatch.setattr(local_server, f"_LOCAL_SERVER_{name}_PATH", tmp_path / name)


def _recipe(tmp_path: Path) -> local_server.ServerRestartRecipe:
    recipe = local_server.qualify_server_restart(
        database_uri=f"sqlite:///{tmp_path}/chat.db",
        artifact_location=str(tmp_path / "artifacts"),
        config_path=None,
    )
    assert recipe is not None
    return recipe


@pytest.mark.parametrize("drift", ["pid", "config", "port", "symlink"])
def test_maintenance_refuses_without_signals_or_record_deletion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, drift: str
) -> None:
    path = tmp_path / "owner.json"
    identity = ProcessIdentity.capture(os.getpid()).as_dict()
    if drift == "pid":
        identity["started_at"] += 1
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "recipe": _recipe(tmp_path).model_dump(mode="json"),
                "process": identity,
                "port": 8123,
                "base_path": "",
            }
        )
    )
    before = path.read_bytes()
    if drift == "config":
        monkeypatch.setenv("OMNIGENT_SKIP_WEB_UI", "changed")
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_OWNER_PATH", path)
    monkeypatch.setattr(
        local_server, "_LOCAL_SERVER_MAINTENANCE_PATH", tmp_path / "maintenance.json"
    )
    monkeypatch.setattr(local_server, "_read_local_server_pid_file", lambda: (os.getpid(), 8123))
    monkeypatch.setattr(
        local_server,
        "server_config_signature",
        lambda: "changed" if drift == "config" else "expected",
    )
    monkeypatch.setattr(
        local_server,
        "_pid_listening_on_port",
        lambda port: 99999 if drift == "port" else os.getpid(),
    )
    signals = Mock(side_effect=AssertionError("foreign signal"))
    monkeypatch.setattr("psutil.Process.terminate", signals)
    if drift == "symlink":
        link = tmp_path / "link.json"
        link.symlink_to(path)
        monkeypatch.setattr(local_server, "_LOCAL_SERVER_OWNER_PATH", link)
    with pytest.raises(local_server.ServerMaintenanceError):
        local_server.maintenance_stop_local_server()
    signals.assert_not_called()
    assert path.read_bytes() == before
    assert not (tmp_path / "maintenance.json").exists()


def test_maintenance_stop_calls_only_server_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    server_owner = Mock(return_value={"endpoint": "http://127.0.0.1:8123", "state": "detached"})
    monkeypatch.setattr(
        "omnigent.host.local_server.maintenance_stop_local_server", server_owner, raising=False
    )
    host_owner = Mock(side_effect=AssertionError("maintenance must not stop the host"))
    monkeypatch.setattr(cli_module, "_terminate_daemon", host_owner)
    result = CliRunner().invoke(cli_module.cli, ["server", "maintenance-stop", "--json"])
    assert result.exit_code == 0, result.output
    server_owner.assert_called_once()
    host_owner.assert_not_called()


@pytest.mark.parametrize("base_path", ["", "/stable"])
def test_maintenance_accepts_exited_server_waiting_for_parent_reap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, base_path: str
) -> None:
    import psutil

    record = local_server.ServerMaintenanceRecord(
        schema_version=2,
        recipe=_recipe(tmp_path),
        process=ProcessIdentity.capture(os.getpid()),
        port=8123,
        base_path=base_path,
    )
    process = Mock()
    process.wait.side_effect = psutil.TimeoutExpired(0)
    process.status.return_value = psutil.STATUS_ZOMBIE
    monkeypatch.setattr(ProcessIdentity, "require_live", lambda self: process)
    monkeypatch.setattr(local_server, "_server_maintenance_record", lambda path: record)
    monkeypatch.setattr(
        local_server, "_LOCAL_SERVER_MAINTENANCE_PATH", tmp_path / "maintenance.json"
    )
    monkeypatch.setattr(local_server, "_read_local_server_pid_file", lambda: (os.getpid(), 8123))
    monkeypatch.setattr(local_server, "server_config_signature", lambda: "expected")
    monkeypatch.setattr(local_server, "_pid_listening_on_port", lambda port: os.getpid())
    result = local_server.maintenance_stop_local_server()
    assert result == {"endpoint": f"http://127.0.0.1:8123{base_path}", "state": "detached"}
    process.terminate.assert_called_once()


def test_host_maintenance_detach_skips_ending_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    record = Mock(pid=1234, target="http://127.0.0.1:8123")
    monkeypatch.setattr(cli_module, "_selected_daemon_records", lambda **kwargs: [record])
    ended = Mock(side_effect=AssertionError("maintenance cannot end sessions"))
    monkeypatch.setattr(cli_module, "_stop_daemon_sessions", ended)
    terminated = Mock()
    monkeypatch.setattr(cli_module, "_terminate_daemon", terminated)
    result = CliRunner().invoke(
        cli_module.cli, ["host", "maintenance-detach", "--server", record.target]
    )
    assert result.exit_code == 0, result.output
    ended.assert_not_called()
    terminated.assert_called_once_with(record, force=False)


def test_maintenance_start_refuses_a_live_previous_api_even_if_port_is_free(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = local_server.ServerMaintenanceRecord(
        schema_version=2,
        recipe=_recipe(tmp_path),
        process=ProcessIdentity.capture(os.getpid()),
        port=8123,
        base_path="",
    )
    monkeypatch.setattr(
        local_server, "_LOCAL_SERVER_MAINTENANCE_PATH", tmp_path / "maintenance.json"
    )
    monkeypatch.setattr(local_server, "_server_maintenance_record", lambda path: record)
    monkeypatch.setattr(local_server, "server_config_signature", lambda: "expected")
    spawned = Mock()
    monkeypatch.setattr(local_server, "_spawn_local_server", spawned)
    monkeypatch.setattr(local_server, "_assert_server_port_bindable", lambda *args: None)
    with pytest.raises(local_server.ServerMaintenanceError, match="process_pending"):
        local_server.maintenance_start_local_server()
    spawned.assert_not_called()


@pytest.mark.parametrize("drift", ["config", "port", "pid"])
def test_maintenance_start_refuses_endpoint_or_identity_drift_without_spawning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, drift: str
) -> None:
    import click
    import psutil

    record = local_server.ServerMaintenanceRecord(
        schema_version=2,
        recipe=_recipe(tmp_path),
        process=ProcessIdentity.capture(os.getpid()),
        port=8123,
        base_path="/stable",
    )
    receipt = tmp_path / "maintenance.json"
    receipt.write_text(record.model_dump_json())
    before = receipt.read_bytes()
    if drift == "config":
        monkeypatch.setenv("OMNIGENT_SKIP_WEB_UI", "changed")
    monkeypatch.setattr(local_server, "_LOCAL_SERVER_MAINTENANCE_PATH", receipt)
    monkeypatch.delenv("OMNIGENT_CONFIG", raising=False)
    monkeypatch.delenv("OMNIGENT_WEB_BASE_PATH", raising=False)
    monkeypatch.delenv("OMNIGENT_LOCAL_SERVER_PORT", raising=False)

    def previous_gone(identity: ProcessIdentity) -> None:
        if drift == "pid":
            raise ValueError("record_mismatch")
        raise psutil.NoSuchProcess(identity.pid)

    monkeypatch.setattr(ProcessIdentity, "require_live", previous_gone)
    monkeypatch.setattr(
        local_server,
        "server_config_signature",
        lambda: "changed" if drift == "config" else "expected",
    )

    def occupied(*args: object) -> None:
        raise click.ClickException("port owned by foreign process")

    monkeypatch.setattr(local_server, "_assert_server_port_bindable", occupied)
    spawned = Mock()
    signals = Mock(side_effect=AssertionError("foreign signal"))
    monkeypatch.setattr(local_server, "_spawn_local_server", spawned)
    monkeypatch.setattr("psutil.Process.terminate", signals)
    with pytest.raises(local_server.ServerMaintenanceError):
        local_server.maintenance_start_local_server()
    signals.assert_not_called()
    spawned.assert_not_called()
    assert receipt.read_bytes() == before
    assert "OMNIGENT_WEB_BASE_PATH" not in os.environ
    assert "OMNIGENT_LOCAL_SERVER_PORT" not in os.environ


def test_maintenance_process_race_emits_safe_machine_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psutil

    def disappeared() -> None:
        raise psutil.NoSuchProcess(99999, msg="private process data")

    monkeypatch.setattr(local_server, "maintenance_stop_local_server", disappeared)
    result = CliRunner().invoke(cli_module.cli, ["server", "maintenance-stop", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "schema_version": 1,
        "error": {"category": "unavailable_probe"},
    }
    assert "private process data" not in result.output


@pytest.mark.parametrize(
    "failure,category",
    [
        ("probe-timeout", "occupied_port"),
        ("foreign-listener", "occupied_port"),
        ("owner-mismatch", "config_mismatch"),
        ("publication", "unavailable_probe"),
        ("startup", "unavailable_probe"),
        (None, None),
    ],
)
def test_maintenance_start_reaps_only_owned_child_before_publication_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str | None,
    category: str | None,
) -> None:
    import subprocess
    import sys

    import psutil

    previous = local_server.ServerMaintenanceRecord(
        schema_version=2,
        recipe=_recipe(tmp_path),
        process=ProcessIdentity.capture(os.getpid()),
        port=8123,
        base_path="/stable",
    )

    def previous_gone(identity: ProcessIdentity) -> None:
        raise psutil.NoSuchProcess(identity.pid)

    monkeypatch.setattr(ProcessIdentity, "require_live", previous_gone)
    monkeypatch.setattr(local_server, "_assert_server_port_bindable", lambda *args: None)
    with (
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]) as child,
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]) as foreign,
    ):
        try:
            replacement = previous.model_copy(
                update={
                    "process": ProcessIdentity.capture(
                        foreign.pid if failure == "owner-mismatch" else child.pid
                    )
                }
            )
            monkeypatch.setattr(
                local_server,
                "_server_maintenance_record",
                lambda path: (
                    previous
                    if path == local_server._LOCAL_SERVER_MAINTENANCE_PATH
                    else replacement
                ),
            )
            spawned = local_server._SpawnedLocalServer(
                proc=child, log_path=tmp_path / "server.log", base_url="http://127.0.0.1:8123"
            )
            monkeypatch.setattr(
                local_server, "_spawn_local_server", lambda *args, **kwargs: spawned
            )
            wait = Mock(
                side_effect=local_server.LocalServerStartupError("private-startup-diagnostics")
                if failure == "startup"
                else None
            )
            monkeypatch.setattr(local_server, "_wait_for_local_omnigent_server", wait)
            probe = Mock(
                side_effect=subprocess.TimeoutExpired("lsof", 5)
                if failure == "probe-timeout"
                else None,
                return_value=subprocess.CompletedProcess(
                    [], 0, stdout=str(foreign.pid if failure == "foreign-listener" else child.pid)
                ),
            )
            monkeypatch.setattr(local_server.subprocess, "run", probe)
            publish = Mock(
                side_effect=OSError("private-publication-details")
                if failure == "publication"
                else None
            )
            monkeypatch.setattr(local_server, "_write_local_server_record", publish)

            result = CliRunner().invoke(cli_module.cli, ["server", "maintenance-start", "--json"])

            assert foreign.poll() is None
            if failure is None:
                assert result.exit_code == 0, result.output
                assert json.loads(result.output)["state"] == "ready"
                assert child.poll() is None
            else:
                assert result.exit_code == 1, result.output
                assert json.loads(result.output) == {
                    "schema_version": 1,
                    "error": {"category": category},
                }
                assert "private-" not in result.output
                assert child.returncode is not None
                assert not psutil.pid_exists(child.pid)
            if failure in {None, "publication"}:
                publish.assert_called_once()
                assert publish.call_args.args[0] == child.pid
            else:
                publish.assert_not_called()
        finally:
            for process in (child, foreign):
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)


def test_qualified_restart_preserves_strict_auth_and_explicit_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import importlib.metadata
    import io
    from contextlib import contextmanager

    config = tmp_path / "selected.yaml"
    config.write_text("database_uri: sqlite:////selected.db\n")
    recipe = local_server.qualify_server_restart(
        database_uri=f"sqlite:///{tmp_path}/selected.db",
        artifact_location=str(tmp_path / "selected-artifacts"),
        config_path=str(config),
    )
    assert recipe is not None
    record = local_server.ServerMaintenanceRecord(
        schema_version=2,
        process=ProcessIdentity.capture(os.getpid()),
        port=8123,
        base_path="/stable",
        recipe=recipe,
    )
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "next-version")
    assert local_server._verify_restart_recipe(record) == recipe
    captured = {}

    def spawn(args, **kwargs):
        captured.update(args=args, env=kwargs["env"])
        return Mock(pid=1234)

    @contextmanager
    def logging_kwargs(env):
        yield {}

    monkeypatch.setattr(
        local_server, "open_process_log_file", lambda *a, **kw: (tmp_path / "log", io.BytesIO())
    )
    monkeypatch.setattr(local_server, "child_logging_popen_kwargs", logging_kwargs)
    monkeypatch.setattr(local_server.subprocess, "Popen", spawn)
    local_server._spawn_local_server(8123, "/stable", recipe=recipe)
    args = captured["args"]
    assert args[args.index("--config") + 1] == str(config)
    assert args[args.index("--database-uri") + 1] == recipe.database_uri
    assert args[args.index("--artifact-location") + 1] == recipe.artifact_location
    assert captured["env"]["OMNIGENT_LOCAL_SINGLE_USER"] == "0"
    assert captured["env"]["OMNIGENT_WEB_BASE_PATH"] == "/stable"
    config.write_text("database_uri: sqlite:////foreign.db\n")
    with pytest.raises(local_server.ServerMaintenanceError, match="config_mismatch"):
        local_server._verify_restart_recipe(record)


@pytest.mark.parametrize("unsupported", ["postgres", "relative", "options", "accounts"])
def test_unsupported_restart_refuses_before_stop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unsupported: str,
) -> None:
    if unsupported == "accounts":
        monkeypatch.setenv("OMNIGENT_AUTH_PROVIDER", "accounts")
    recipe = local_server.qualify_server_restart(
        database_uri="postgresql://secret@remote/db"
        if unsupported == "postgres"
        else f"sqlite:///{tmp_path}/db",
        artifact_location="relative" if unsupported == "relative" else str(tmp_path / "artifacts"),
        config_path=None,
        unsupported_options=unsupported == "options",
    )
    assert recipe is None
    record = local_server.ServerMaintenanceRecord(
        schema_version=2,
        process=ProcessIdentity.capture(os.getpid()),
        port=8123,
        base_path="",
        recipe=recipe,
    )
    monkeypatch.setattr(local_server, "_server_maintenance_record", lambda path: record)
    monkeypatch.setattr(local_server, "_read_local_server_pid_file", lambda: (os.getpid(), 8123))
    signal = Mock(side_effect=AssertionError("must refuse before signaling"))
    monkeypatch.setattr("psutil.Process.terminate", signal)
    with pytest.raises(local_server.ServerMaintenanceError, match="unsupported_restart"):
        local_server.maintenance_stop_local_server()
    signal.assert_not_called()


def test_local_mode_endpoint_detaches_exact_record(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    local = SimpleNamespace(
        target="local", mode="local", resolved_server_url="http://127.0.0.1:8123", pid=1234
    )
    foreign = SimpleNamespace(
        target="http://foreign:8123", mode="remote", resolved_server_url=None, pid=5678
    )
    monkeypatch.setattr(cli_module, "_list_daemon_records", lambda: [local, foreign])
    terminated = Mock()
    monkeypatch.setattr(cli_module, "_terminate_daemon", terminated)
    result = CliRunner().invoke(
        cli_module.cli, ["host", "maintenance-detach", "--server", local.resolved_server_url]
    )
    assert result.exit_code == 0, result.output
    terminated.assert_called_once_with(local, force=False)
    duplicate = SimpleNamespace(
        target=local.resolved_server_url, mode="remote", resolved_server_url=None
    )
    monkeypatch.setattr(cli_module, "_list_daemon_records", lambda: [local, duplicate])
    result = CliRunner().invoke(
        cli_module.cli, ["host", "maintenance-detach", "--server", local.resolved_server_url]
    )
    assert result.exit_code != 0
    assert terminated.call_count == 1


@pytest.mark.parametrize("daemon_signature", [None, "upgraded-daemon"])
@pytest.mark.parametrize("effective_drift", [False, True])
def test_maintenance_ignores_only_internal_daemon_signature(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    daemon_signature: str | None,
    effective_drift: bool,
) -> None:
    from omnigent.host.daemon_lifecycle import DAEMON_CONFIG_SIG_ENV_VAR

    monkeypatch.setenv(DAEMON_CONFIG_SIG_ENV_VAR, "original-daemon")
    record = local_server.ServerMaintenanceRecord(
        schema_version=2,
        recipe=_recipe(tmp_path),
        process=ProcessIdentity.capture(os.getpid()),
        port=8123,
        base_path="",
    )
    if daemon_signature is None:
        monkeypatch.delenv(DAEMON_CONFIG_SIG_ENV_VAR)
    else:
        monkeypatch.setenv(DAEMON_CONFIG_SIG_ENV_VAR, daemon_signature)
    if effective_drift:
        monkeypatch.setenv("OMNIGENT_DATABASE_URI", f"sqlite:///{tmp_path}/other.db")
    process = Mock()
    monkeypatch.setattr(ProcessIdentity, "require_live", lambda self: process)
    monkeypatch.setattr(local_server, "_server_maintenance_record", lambda path: record)
    monkeypatch.setattr(local_server, "_read_local_server_pid_file", lambda: (os.getpid(), 8123))
    monkeypatch.setattr(local_server, "_pid_listening_on_port", lambda port: os.getpid())
    if effective_drift:
        with pytest.raises(local_server.ServerMaintenanceError, match="config_mismatch"):
            local_server.maintenance_stop_local_server()
        process.terminate.assert_not_called()
    else:
        assert local_server.maintenance_stop_local_server()["state"] == "detached"
        process.terminate.assert_called_once()


@pytest.mark.parametrize("logging_drift", [None, "OMNIGENT_LOG_LEVEL", "OMNIGENT_LOG_TO_STDERR"])
def test_maintenance_excludes_injected_tty_descriptor_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, logging_drift: str | None
) -> None:
    from omnigent.process_logging import LOG_TTY_FD_ENV_VAR, child_logging_popen_kwargs

    monkeypatch.setenv("OMNIGENT_LOG_TO_STDERR", "1")
    monkeypatch.setenv("OMNIGENT_LOG_LEVEL", "INFO")
    monkeypatch.delenv(LOG_TTY_FD_ENV_VAR, raising=False)
    with (tmp_path / "terminal.log").open("w") as terminal:
        child_env = {**os.environ, LOG_TTY_FD_ENV_VAR: str(terminal.fileno())}
        with child_logging_popen_kwargs(child_env) as kwargs:
            assert kwargs["pass_fds"]
            with monkeypatch.context() as child:
                child.setenv(LOG_TTY_FD_ENV_VAR, child_env[LOG_TTY_FD_ENV_VAR])
                recipe = _recipe(tmp_path)
    record = local_server.ServerMaintenanceRecord(
        schema_version=2,
        recipe=recipe,
        process=ProcessIdentity.capture(os.getpid()),
        port=8123,
        base_path="",
    )
    if logging_drift:
        monkeypatch.setenv(logging_drift, "DEBUG" if logging_drift.endswith("LEVEL") else "0")
    process = Mock()
    monkeypatch.setattr(ProcessIdentity, "require_live", lambda self: process)
    monkeypatch.setattr(local_server, "_server_maintenance_record", lambda path: record)
    monkeypatch.setattr(local_server, "_read_local_server_pid_file", lambda: (os.getpid(), 8123))
    monkeypatch.setattr(local_server, "_pid_listening_on_port", lambda port: os.getpid())
    if logging_drift:
        with pytest.raises(local_server.ServerMaintenanceError, match="config_mismatch"):
            local_server.maintenance_stop_local_server()
        process.terminate.assert_not_called()
    else:
        assert local_server.maintenance_stop_local_server()["state"] == "detached"
        process.terminate.assert_called_once()


def test_managed_child_does_not_publish_competing_server_for_discovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    import httpx
    import uvicorn.server

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    class CompetingServer(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def log_message(self, *args: object) -> None:
            pass

    observed = []

    def lose_bind_race(
        server: uvicorn.server.Server, sockets: list[socket.socket] | None = None
    ) -> None:
        assert sockets is not None and len(sockets) == 1
        sockets[0].close()
        with ThreadingHTTPServer(("127.0.0.1", port), CompetingServer) as competitor:
            thread = threading.Thread(target=competitor.serve_forever)
            thread.start()
            try:
                assert (
                    httpx.get(f"http://127.0.0.1:{port}/health", trust_env=False).status_code
                    == 200
                )
                receipt = local_server._server_maintenance_record(
                    local_server._LOCAL_SERVER_OWNER_PATH
                )
                assert receipt.process == ProcessIdentity.capture(os.getpid())
                assert receipt.recipe is not None
                assert not local_server._LOCAL_SERVER_PID_PATH.exists()
                assert local_server.local_server_url_if_healthy() is None
                observed.append(receipt)
            finally:
                competitor.shutdown()
                thread.join(timeout=5)

    monkeypatch.setattr(uvicorn.server.Server, "run", lose_bind_race)
    result = CliRunner().invoke(
        cli_module.cli,
        [
            "server",
            "--managed-local",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{tmp_path}/chat.db",
            "--artifact-location",
            str(tmp_path / "artifacts"),
        ],
    )
    assert result.exit_code == 0, (result.output, result.exception)
    assert len(observed) == 1
    assert not local_server._LOCAL_SERVER_PID_PATH.exists()
