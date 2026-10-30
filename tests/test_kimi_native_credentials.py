"""Tests for the per-session KIMI_CODE_HOME builder."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from omnigent.harnesses.kimi_native.credentials import (
    KIMI_CODE_HOME_ENV_VAR,
    build_kimi_session_home,
    render_kimi_hooks_toml,
)
from omnigent.runner.identity import strip_runner_service_env


def test_generated_hooks_restore_service_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = {
        "OMNIGENT_CONFIG_HOME": str(tmp_path / "config 'quoted'"),
        "OMNIGENT_DATA_DIR": str(tmp_path / 'data "quoted" \\ path'),
        "OMNIGENT_RUNNER_SLICE_KEY": "replica-a",
    }
    for name, value in expected.items():
        monkeypatch.setenv(name, value)
    probe = tmp_path / "python-probe"
    script = (
        "import json, os; "
        f"print(json.dumps({{key: os.environ.get(key) for key in {list(expected)!r}}}))"
    )
    probe.write_text(f"#!/bin/sh\nexec {shlex.join([sys.executable, '-c', script])}\n")
    probe.chmod(0o700)
    config = tomllib.loads(
        render_kimi_hooks_toml(bridge_dir=tmp_path, python_executable=str(probe))
    )
    pane_env = strip_runner_service_env(os.environ)
    for hook in config["hooks"]:
        result = subprocess.run(
            ["/bin/sh", "-c", hook["command"]],
            env=pane_env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(result.stdout) == expected
    assert all(name not in pane_env for name in expected)


def _fake_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``resolve_user_kimi_home`` at a populated fake global home."""
    user_home = tmp_path / "user-kimi"
    user_home.mkdir()
    (user_home / "config.toml").write_text(
        'default_model = "kimi-code/x"\n[providers."managed"]\ntype = "kimi"\n', encoding="utf-8"
    )
    (user_home / "oauth").mkdir()
    (user_home / "oauth" / "token").write_text("secret", encoding="utf-8")
    (user_home / "sessions").mkdir()
    (user_home / "sessions" / "wd_g").mkdir()
    (user_home / "session_index.jsonl").write_text('{"sessionDir": "x"}\n', encoding="utf-8")
    monkeypatch.setenv(KIMI_CODE_HOME_ENV_VAR, str(user_home))
    return user_home


def test_render_hooks_toml_is_valid_and_complete() -> None:
    toml = render_kimi_hooks_toml(bridge_dir=Path("/tmp/b r"), python_executable="/py")
    parsed = tomllib.loads(toml)
    events = {h["event"] for h in parsed["hooks"]}
    assert events == {"PreToolUse", "PermissionRequest"}
    for hook in parsed["hooks"]:
        assert "omnigent.harnesses.kimi_native.hook" in hook["command"]
        assert "/tmp/b r" in hook["command"]  # space-bearing path round-trips
        # ``-I`` (isolated mode) is mandatory: kimi runs the hook with cwd set to
        # the session workspace, so without it a workspace containing its own
        # ``omnigent/`` shadows the install and the hook dies on ImportError
        # before publishing the approval card.
        assert " -I -m omnigent.harnesses.kimi_native.hook" in hook["command"]
        # Pinned above kimi's 30s default so the permission hook survives a slow
        # web verdict (else the injected keystroke never lands); 600 is kimi's
        # ceiling.
        assert hook["timeout"] == 600


def test_build_session_home_preserves_user_config_and_appends_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_user_home(tmp_path, monkeypatch)
    session_home = tmp_path / "session-home"
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    env = build_kimi_session_home(session_home, bridge_dir=bridge_dir)

    assert env == {KIMI_CODE_HOME_ENV_VAR: str(session_home)}
    parsed = tomllib.loads((session_home / "config.toml").read_text(encoding="utf-8"))
    # User config preserved …
    assert parsed["default_model"] == "kimi-code/x"
    assert "managed" in parsed["providers"]
    # … and the Omnigent hooks appended.
    assert {h["event"] for h in parsed["hooks"]} == {"PreToolUse", "PermissionRequest"}


def test_build_session_home_symlinks_auth_but_not_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_user_home(tmp_path, monkeypatch)
    session_home = tmp_path / "session-home"
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    build_kimi_session_home(session_home, bridge_dir=bridge_dir)

    # oauth is symlinked through to the user's tokens (auth keeps working) …
    oauth_link = session_home / "oauth"
    assert oauth_link.is_symlink()
    assert (oauth_link / "token").read_text(encoding="utf-8") == "secret"
    # … but config.toml is a real file (we own its content), not a symlink.
    assert not (session_home / "config.toml").is_symlink()


def test_build_session_home_keeps_sessions_store_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sessions store must not be shared, or parallel kimi sessions launched
    near-simultaneously can adopt each other's wire logs (crossed transcripts)."""
    _fake_user_home(tmp_path, monkeypatch)
    session_home = tmp_path / "session-home"
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    build_kimi_session_home(session_home, bridge_dir=bridge_dir)

    sessions = session_home / "sessions"
    assert sessions.is_dir() and not sessions.is_symlink()
    assert list(sessions.iterdir()) == []  # empty private store, not the global one
    assert not (session_home / "session_index.jsonl").exists()


def test_build_session_home_drops_stale_global_sessions_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session home built before the store went private gets its global
    ``sessions`` / index symlinks replaced by a private store on rebuild."""
    user_home = _fake_user_home(tmp_path, monkeypatch)
    session_home = tmp_path / "session-home"
    session_home.mkdir()
    (session_home / "sessions").symlink_to(user_home / "sessions")
    (session_home / "session_index.jsonl").symlink_to(user_home / "session_index.jsonl")
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    build_kimi_session_home(session_home, bridge_dir=bridge_dir)

    sessions = session_home / "sessions"
    assert sessions.is_dir() and not sessions.is_symlink()
    assert not (session_home / "session_index.jsonl").exists()
    # The user's global store is untouched.
    assert (user_home / "sessions" / "wd_g").is_dir()
    assert (user_home / "session_index.jsonl").read_text(encoding="utf-8")


def test_build_session_home_without_user_home_writes_hooks_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KIMI_CODE_HOME_ENV_VAR, str(tmp_path / "does-not-exist"))
    session_home = tmp_path / "session-home"
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    build_kimi_session_home(session_home, bridge_dir=bridge_dir)

    parsed = tomllib.loads((session_home / "config.toml").read_text(encoding="utf-8"))
    assert {h["event"] for h in parsed["hooks"]} == {"PreToolUse", "PermissionRequest"}
