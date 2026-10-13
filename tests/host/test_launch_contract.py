"""The launch receipt is independent of telemetry and refuses unsafe endpoints."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnigent.cli import cli
from omnigent.host.launch_contract import LaunchContractError, launch_contract


def test_receipt_reports_actual_runtime_without_telemetry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://unavailable.invalid")
    monkeypatch.setenv("SECRET_TOKEN", "do-not-emit")
    receipt = launch_contract("http://127.0.0.1:8123", probe=False)
    assert receipt["schema_version"] == 1
    assert receipt["endpoint"] == "http://127.0.0.1:8123"
    assert receipt["availability"] == "unverified"
    runtime = receipt["runtime"]
    assert isinstance(runtime, dict)
    assert Path(runtime["executable"]).is_absolute()
    assert Path(runtime["executable"]).is_file()
    assert runtime["generation"].startswith("sha256:")
    assert "do-not-emit" not in json.dumps(receipt)
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in receipt["client_environment"]
    assert receipt["telemetry"] == {"owner": "external", "admission": "external_policy"}


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://user:secret@localhost:8",
        "file:///tmp/x",
        "http://localhost:8/?token=x",
        "http://localhost:8/#x",
        "http://localhost:99999",
    ],
)
def test_invalid_endpoint_refuses_without_echoing_credentials(endpoint: str) -> None:
    with pytest.raises(LaunchContractError) as caught:
        launch_contract(endpoint, probe=False)
    assert caught.value.category == "invalid_endpoint"
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize(
    "status,category", [(401, "auth_denied"), (403, "auth_denied"), (503, "endpoint_unavailable")]
)
def test_health_errors_are_structured(
    monkeypatch: pytest.MonkeyPatch, status: int, category: str
) -> None:
    import httpx

    monkeypatch.setattr(httpx, "get", lambda *args, **kwargs: httpx.Response(status))
    with pytest.raises(LaunchContractError) as caught:
        launch_contract("http://127.0.0.1:8123")
    assert caught.value.category == category


def test_cli_emits_canonical_receipt_and_does_not_start_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx

    monkeypatch.setattr(
        httpx, "get", lambda *args, **kwargs: httpx.Response(200, json={"status": "ok"})
    )
    result = CliRunner().invoke(
        cli, ["runtime", "launch-contract", "--server", "http://127.0.0.1:8123", "--json"]
    )
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)
    assert receipt["availability"] == "ready"
    assert result.output == json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n"


def test_harness_commands_are_executable_cli_commands() -> None:
    receipt = launch_contract("http://127.0.0.1:8123", probe=False)
    rows = {row["id"]: row for row in receipt["harnesses"]}
    assert rows["codex-native"]["command"] == "codex"
    assert rows["claude-native"]["command"] == "claude"
    for row in rows.values():
        assert row["command"] in cli.commands


def test_durable_capability_only_admits_evidenced_harnesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.runner import custody

    monkeypatch.setattr(custody, "SUPPORTED_HARNESSES", frozenset({"codex-native"}))
    rows = launch_contract("http://127.0.0.1:8123", probe=False)["harnesses"]
    assert {row["id"] for row in rows if row["durable_custody"]} == {"codex-native"}


@pytest.mark.parametrize("form", ["uv_trampoline", "foreign_interpreter", "shell_injection"])
def test_runtime_qualifies_exact_owned_launcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, form: str
) -> None:
    import omnigent
    from omnigent.host import launch_contract as module

    bin_dir = tmp_path / ("long-" + "x" * 140) / "bin"
    bin_dir.mkdir(parents=True)
    python = bin_dir / "python3"
    python.write_text("synthetic interpreter")
    interpreter = str(python) if form != "foreign_interpreter" else str(bin_dir / "foreign-python")
    command = f'\'{interpreter}\' "$0" "$@"'
    if form == "shell_injection":
        command += " ; exit 0"
    launcher = bin_dir / "omnigent"
    launcher.write_text("#!/bin/sh\n'''exec' " + command + "\n' '''\nimport sys\n")
    launcher.chmod(0o700)
    package = tmp_path / "package"
    package.mkdir()
    (package / "__init__.py").write_text("# fixture package\n")
    monkeypatch.setattr(module.sys, "executable", str(python))
    monkeypatch.setattr(omnigent, "__file__", str(package / "__init__.py"))
    if form == "uv_trampoline":
        receipt = module.runtime_identity()
        assert receipt["python"] == str(python)
        assert receipt["executable"] == str(launcher)
    else:
        with pytest.raises(LaunchContractError, match="runtime_mismatch"):
            module.runtime_identity()
