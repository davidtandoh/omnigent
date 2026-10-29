"""Selected-account launch checks use the child's environment, without a cache."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnigent.harnesses.claude_native.account import ensure_claude_account_signed_in


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
    assert probe.call_args.kwargs["timeout"] > 0


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
    ("status", "returncode", "error"),
    [
        ({"loggedIn": False}, 1, "not signed in"),
        ({"loggedIn": False}, 0, "not signed in"),
        ({}, 0, "could not verify"),
        ({"loggedIn": "false"}, 0, "could not verify"),
        ([], 0, "could not verify"),
        ({"loggedIn": True}, 2, "could not verify"),
    ],
)
def test_status_verdict_is_structural_and_output_is_private(
    status: object, returncode: int, error: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = Mock(
        return_value=subprocess.CompletedProcess([], returncode, json.dumps(status), "private")
    )
    monkeypatch.setattr("omnigent.harnesses.claude_native.account.subprocess.run", probe)
    with pytest.raises(RuntimeError, match=error) as caught:
        ensure_claude_account_signed_in(
            "claude", "/accounts/work", env={"CLAUDE_CONFIG_DIR": "/accounts/work"}, cwd=tmp_path
        )
    assert "private" not in str(caught.value)


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
    result: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = (
        Mock(side_effect=result) if isinstance(result, Exception) else Mock(return_value=result)
    )
    monkeypatch.setattr("omnigent.harnesses.claude_native.account.subprocess.run", probe)
    with pytest.raises(RuntimeError, match="could not verify") as caught:
        ensure_claude_account_signed_in(
            "claude", "/accounts/work", env={"CLAUDE_CONFIG_DIR": "/accounts/work"}, cwd=tmp_path
        )
    assert "private" not in str(caught.value)
