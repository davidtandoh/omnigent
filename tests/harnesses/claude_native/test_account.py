"""Selected-account launch checks use the child's environment, without a cache."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnigent.harnesses.claude_native.account import (
    ensure_claude_account_signed_in,
    ensure_spec_account_selected,
)


@pytest.mark.parametrize("config_dir", ["/accounts/work", "/accounts/cafe\u0301/"])
def test_selected_account_probe_preserves_child_environment(
    config_dir: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {"CLAUDE_CONFIG_DIR": config_dir, "USER": "alice", "LOGNAME": "alice"}
    probe = Mock(return_value=subprocess.CompletedProcess([], 0, '{"loggedIn":true}', ""))
    monkeypatch.setattr("omnigent.harnesses.claude_native.account.subprocess.run", probe)
    for _ in range(2):
        ensure_claude_account_signed_in("/bin/claude", config_dir, env=env, cwd=tmp_path)
    assert probe.call_count == 2
    assert probe.call_args.args == (["/bin/claude", "auth", "status"],)
    assert probe.call_args.kwargs["env"] == env
    assert probe.call_args.kwargs["cwd"] == tmp_path
    assert probe.call_args.kwargs["stdin"] == subprocess.DEVNULL
    assert probe.call_args.kwargs["capture_output"] is True
    assert probe.call_args.kwargs["timeout"] == 30


@pytest.mark.parametrize("actual", ["/accounts/personal", None])
def test_account_mismatch_refused_before_probe(
    actual: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = Mock()
    monkeypatch.setattr("omnigent.harnesses.claude_native.account.subprocess.run", probe)
    env = {"CLAUDE_CONFIG_DIR": actual} if actual is not None else {}
    with pytest.raises(RuntimeError, match="different CLAUDE_CONFIG_DIR"):
        ensure_claude_account_signed_in("claude", "/accounts/work", env=env, cwd=tmp_path)
    probe.assert_not_called()


@pytest.mark.parametrize(
    ("status", "returncode", "signed_out"),
    [
        ({"loggedIn": False}, 1, True),
        ({"loggedIn": False}, 0, True),
        ({}, 0, False),
        ({"loggedIn": "false"}, 0, False),
        ([], 0, False),
        ({"loggedIn": True}, 2, False),
        ({}, 2, False),
    ],
)
def test_status_verdict_is_structural_and_output_is_private(
    status: object,
    returncode: int,
    signed_out: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    probe = Mock(
        return_value=subprocess.CompletedProcess([], returncode, json.dumps(status), "private")
    )
    monkeypatch.setattr("omnigent.harnesses.claude_native.account.subprocess.run", probe)
    if signed_out:
        with pytest.raises(RuntimeError, match="signed out") as caught:
            ensure_claude_account_signed_in(
                "claude",
                "/accounts/work",
                env={"CLAUDE_CONFIG_DIR": "/accounts/work"},
                cwd=tmp_path,
            )
        assert "private" not in str(caught.value)
    else:
        ensure_claude_account_signed_in(
            "claude", "/accounts/work", env={"CLAUDE_CONFIG_DIR": "/accounts/work"}, cwd=tmp_path
        )
        assert "could not verify" in caplog.text
        assert "/accounts/work" in caplog.text
        assert "continuing" in caplog.text
    assert "private" not in caplog.text


@pytest.mark.parametrize(
    "result",
    [
        OSError("private spawn error"),
        subprocess.TimeoutExpired("private command", 10),
        UnicodeDecodeError("utf-8", b"private\xff", 7, 8, "invalid byte"),
        subprocess.CompletedProcess([], 0, "private malformed output", "private stderr"),
    ],
)
def test_failed_probe_does_not_claim_signed_out_or_expose_output(
    result: object,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    probe = (
        Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
    )
    monkeypatch.setattr("omnigent.harnesses.claude_native.account.subprocess.run", probe)
    ensure_claude_account_signed_in(
        "claude", "/accounts/work", env={"CLAUDE_CONFIG_DIR": "/accounts/work"}, cwd=tmp_path
    )
    assert "could not verify" in caplog.text
    assert "/accounts/work" in caplog.text
    assert "continuing" in caplog.text
    assert "private" not in caplog.text


@pytest.mark.parametrize(
    ("launch_env", "process_env", "expected"),
    [
        ({"CLAUDE_CONFIG_DIR": "/accounts/work"}, "/accounts/other", "/accounts/work"),
        ({"CLAUDE_CONFIG_DIR": ""}, "/accounts/other", None),
        ({}, "/accounts/other", "/accounts/other"),
        ({}, None, None),
    ],
)
def test_runner_launch_account_selects_transcript_and_session_dirs(
    launch_env: dict[str, str],
    process_env: str | None,
    expected: str | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Resume transcripts and session files follow the account the child runs on."""
    from omnigent.harnesses.claude_native import main as claude_native
    from omnigent.harnesses.claude_native.status_file import sessions_dir
    from omnigent.native import launch_environment

    monkeypatch.setattr(launch_environment, "_installed_native_launch_environment", launch_env)
    if process_env is None:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", process_env)
    default_projects = tmp_path / "default-projects"
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", default_projects)

    if expected is None:
        assert claude_native._claude_projects_dir() == default_projects
        assert sessions_dir() == Path.home() / ".claude" / "sessions"
    else:
        assert claude_native._claude_projects_dir() == Path(expected) / "projects"
        assert sessions_dir() == Path(expected) / "sessions"


def _write_user_config(home: Path, body: str) -> None:
    (home / "config.yaml").write_text(body)


def test_signed_out_refusal_names_the_account_and_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_user_config(
        tmp_path,
        "host:\n  name: mac-mini\nproviders:\n  claude-work:\n    kind: subscription\n"
        "    cli: claude\n    config_dir: /accounts/work\n",
    )
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    probe = Mock(return_value=subprocess.CompletedProcess([], 1, '{"loggedIn":false}', ""))
    monkeypatch.setattr("omnigent.harnesses.claude_native.account.subprocess.run", probe)
    with pytest.raises(RuntimeError) as caught:
        ensure_claude_account_signed_in(
            "claude", "/accounts/work", env={"CLAUDE_CONFIG_DIR": "/accounts/work"}, cwd=tmp_path
        )
    message = str(caught.value)
    assert "Claude account 'work' on host 'mac-mini' is signed out" in message
    assert "setup" in message


_SPEC_ACCOUNT_CONFIG = {
    "providers": {
        "claude-work": {"kind": "subscription", "cli": "claude", "config_dir": "/accounts/work"},
        "claude-gateway": {
            "kind": "gateway",
            "anthropic": {"base_url": "https://gw.example/anthropic", "api_key": "$GW"},
        },
    }
}


@pytest.mark.parametrize(
    ("provider", "launch_dir"),
    [
        (None, None),
        ("claude-work", "/accounts/work"),
        ("anthropic", None),
        ("claude-subscription", None),
        ("claude-gateway", None),
    ],
)
def test_spec_account_guard_allows_matching_or_unrelated_providers(
    provider: str | None, launch_dir: str | None
) -> None:
    ensure_spec_account_selected(
        provider,
        launch_dir,
        config=_SPEC_ACCOUNT_CONFIG,
        session_account="work" if provider == "claude-work" else None,
    )


@pytest.mark.parametrize(
    ("provider", "launch_dir"),
    [
        ("claude-work", None),
        ("claude-work", "/accounts/other"),
        ("claude-missing", None),
    ],
)
def test_spec_account_guard_never_runs_a_named_account_elsewhere(
    provider: str, launch_dir: str | None
) -> None:
    """An agent that names an account runs on it or is refused, never on the default."""
    with pytest.raises(RuntimeError, match="runs on Claude account"):
        ensure_spec_account_selected(provider, launch_dir, config=_SPEC_ACCOUNT_CONFIG)


@pytest.mark.parametrize(
    ("account", "launch_dir", "matches"),
    [
        ("work", "/accounts/work", True),
        ("work", None, False),
        ("work", "/accounts/other", False),
        (None, "/accounts/work", False),
        (None, None, True),
    ],
)
def test_recorded_session_account_must_match_the_runner(
    account: str | None, launch_dir: str | None, matches: bool
) -> None:
    if matches:
        ensure_spec_account_selected(
            None, launch_dir, session_account=account, config=_SPEC_ACCOUNT_CONFIG
        )
    else:
        with pytest.raises(RuntimeError, match="config_mismatch") as caught:
            ensure_spec_account_selected(
                None, launch_dir, session_account=account, config=_SPEC_ACCOUNT_CONFIG
            )
        assert (repr(account) if account else "default Claude login") in str(caught.value)


@pytest.mark.parametrize(
    "provider",
    [
        {"kind": "subscription", "cli": "claude"},
        _SPEC_ACCOUNT_CONFIG["providers"]["claude-gateway"],
    ],
)
def test_recorded_account_cannot_use_a_provider_without_a_sign_in_folder(
    provider: dict[str, object],
) -> None:
    from omnigent.onboarding.claude_accounts import AccountUnavailableError

    with pytest.raises(AccountUnavailableError, match="'work' is not set up on host"):
        ensure_spec_account_selected(
            None,
            None,
            session_account="work",
            config={"providers": {"claude-work": provider}},
        )
