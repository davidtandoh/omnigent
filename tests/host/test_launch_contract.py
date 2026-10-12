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
