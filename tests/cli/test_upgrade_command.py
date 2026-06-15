"""Tests for the ``omni upgrade`` command (omnigent.cli.upgrade)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from omnigent.cli import cli
from omnigent.host.local_server import LocalServerInfo
from omnigent.update_check import _InstalledWheelInfo


def _uv_registry_info() -> _InstalledWheelInfo:
    """A registry uv-tool install → ``uv tool upgrade omnigent`` (runnable)."""
    return _InstalledWheelInfo(
        install_time_epoch=0.0,
        installer="uv",
        vcs_url=None,
        commit_sha=None,
        is_editable=False,
        package_version="0.1.0",
        detected_installer="uv",
    )


@pytest.fixture
def _wheel_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the running interpreter look like a registry uv-tool install.

    Pins the resolved versions and stubs the install-shape detectors so
    every upgrade test starts from "installed v0.1.0 via uv, not a clone".
    The PyPI lookup and the server-stop side effects are stubbed per test.
    """
    import importlib.metadata

    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "0.1.0")
    monkeypatch.setattr("omnigent.update_check._find_repo_root", lambda: None)
    monkeypatch.setattr("omnigent.update_check._read_installed_wheel_info", _uv_registry_info)
    # Neutralize the process side effects unless a test opts in to assert them.
    monkeypatch.setattr(
        "omnigent.cli.local_server_status",
        lambda: LocalServerInfo(running=False, pid=None, port=None, url=None),
    )
    monkeypatch.setattr("omnigent.cli._stop_local_server_and_daemon", lambda *, force: False)


def test_upgrade_up_to_date(monkeypatch: pytest.MonkeyPatch, _wheel_install: None) -> None:
    """Latest == installed → reports up to date, exit 0, nothing stopped/run."""
    monkeypatch.setattr("omnigent.update_check.fetch_latest_pypi_version", lambda: "0.1.0")

    def _must_not_run(*_a: object, **_k: object) -> int:
        raise AssertionError("upgrade command ran while already up to date")

    monkeypatch.setattr("omnigent.update_check._run_upgrade_command", _must_not_run)

    result = CliRunner().invoke(cli, ["upgrade"])

    assert result.exit_code == 0, result.output
    assert "up to date" in result.output
    assert "0.1.0" in result.output


def test_upgrade_check_reports_newer_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """``--check`` with a newer release → prints the delta and exits non-zero, no upgrade."""
    monkeypatch.setattr("omnigent.update_check.fetch_latest_pypi_version", lambda: "0.2.0")

    def _must_not_run(*_a: object, **_k: object) -> int:
        raise AssertionError("--check must not run the upgrade")

    monkeypatch.setattr("omnigent.update_check._run_upgrade_command", _must_not_run)

    result = CliRunner().invoke(cli, ["upgrade", "--check"])

    assert result.exit_code == 1, result.output
    assert "v0.1.0 → v0.2.0" in result.output


def test_upgrade_runs_installer_and_drains_first(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """A newer release → drain (no force), stop the server, run the uv command."""
    monkeypatch.setattr("omnigent.update_check.fetch_latest_pypi_version", lambda: "0.2.0")

    events: list[str] = []
    monkeypatch.setattr(
        "omnigent.cli._wait_for_local_sessions_to_drain", lambda: events.append("drained")
    )

    def _stop(*, force: bool) -> bool:
        events.append(f"stop(force={force})")
        return True

    monkeypatch.setattr("omnigent.cli._stop_local_server_and_daemon", _stop)

    ran: list[str] = []

    def _run(command: str, _console: object) -> int:
        ran.append(command)
        return 0

    monkeypatch.setattr("omnigent.update_check._run_upgrade_command", _run)

    result = CliRunner().invoke(cli, ["upgrade"])

    assert result.exit_code == 0, result.output
    # Drain happened before the stop, before the install ran.
    assert events == ["drained", "stop(force=False)"]
    assert ran == ["uv tool upgrade omnigent"]
    assert "Upgraded to v0.2.0" in result.output


def test_upgrade_force_skips_drain_and_force_stops(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """``--force`` skips the drain wait and force-stops the server."""
    monkeypatch.setattr("omnigent.update_check.fetch_latest_pypi_version", lambda: "0.2.0")

    def _no_drain() -> None:
        raise AssertionError("--force must not wait for sessions to drain")

    monkeypatch.setattr("omnigent.cli._wait_for_local_sessions_to_drain", _no_drain)

    stop_calls: list[bool] = []
    monkeypatch.setattr(
        "omnigent.cli._stop_local_server_and_daemon",
        lambda *, force: stop_calls.append(force) or False,
    )
    monkeypatch.setattr("omnigent.update_check._run_upgrade_command", lambda *_a, **_k: 0)

    result = CliRunner().invoke(cli, ["upgrade", "--force"])

    assert result.exit_code == 0, result.output
    assert stop_calls == [True]


def test_upgrade_install_failure_surfaces(
    monkeypatch: pytest.MonkeyPatch, _wheel_install: None
) -> None:
    """A non-zero installer exit → ClickException naming the status, exit non-zero."""
    monkeypatch.setattr("omnigent.update_check.fetch_latest_pypi_version", lambda: "0.2.0")
    monkeypatch.setattr("omnigent.update_check._run_upgrade_command", lambda *_a, **_k: 3)

    result = CliRunner().invoke(cli, ["upgrade"])

    assert result.exit_code != 0
    assert "exited with status 3" in result.output


def test_upgrade_pypi_unreachable(monkeypatch: pytest.MonkeyPatch, _wheel_install: None) -> None:
    """PyPI unreachable → clear error, no upgrade attempted."""
    monkeypatch.setattr("omnigent.update_check.fetch_latest_pypi_version", lambda: None)

    result = CliRunner().invoke(cli, ["upgrade"])

    assert result.exit_code != 0
    assert "PyPI" in result.output


def test_upgrade_rejects_dev_clone(monkeypatch: pytest.MonkeyPatch, _wheel_install: None) -> None:
    """A source checkout is redirected to ``git pull``, not upgraded."""
    monkeypatch.setattr("omnigent.update_check._find_repo_root", lambda: Path("/repo"))

    result = CliRunner().invoke(cli, ["upgrade"])

    assert result.exit_code != 0
    assert "git pull" in result.output


def test_upgrade_rejects_editable(monkeypatch: pytest.MonkeyPatch, _wheel_install: None) -> None:
    """An editable install is redirected to ``git pull``, not upgraded."""
    editable = _InstalledWheelInfo(
        install_time_epoch=0.0,
        installer="uv",
        vcs_url="file:///Users/me/omnigent",
        commit_sha=None,
        is_editable=True,
        package_version="0.1.0",
        detected_installer="uv",
    )
    monkeypatch.setattr("omnigent.update_check._read_installed_wheel_info", lambda: editable)

    result = CliRunner().invoke(cli, ["upgrade"])

    assert result.exit_code != 0
    assert "git pull" in result.output
