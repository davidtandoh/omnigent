"""Public Codex bindings refuse relaunch and preserve the exact live owner."""

from __future__ import annotations

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
