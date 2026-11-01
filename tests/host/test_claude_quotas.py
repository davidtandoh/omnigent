"""Quota reads execute only the selected account and publish an allowlist."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from omnigent.host import claude_quotas
from omnigent.host.frames import HostClaudeQuotasResultFrame, decode_host_frame, encode_host_frame


@pytest.fixture
def quota_host(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.delenv("OMNIGENT_CONFIG_HOME", raising=False)
    monkeypatch.delenv("OMNIGENT_DATA_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    config = {
        "providers": {
            "claude-work": {
                "kind": "subscription",
                "cli": "claude",
                "config_dir": str(tmp_path / "work"),
            }
        }
    }
    monkeypatch.setattr("omnigent.config.load_global_config", lambda: config)
    binary = tmp_path / "quota-axi"
    binary.write_text(f"""#!{sys.executable}
import json, os, sys, time
assert sys.argv[1:] == [
    "--provider", "claude", "--json", "--no-credential-refresh", "--max-age", "60s"
]
folder = os.environ.get("CLAUDE_CONFIG_DIR")
if folder:
    assert folder == {str(tmp_path / "work")!r}
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ
    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in os.environ
mode = os.environ.get("FAKE_QUOTA_MODE")
if mode == "slow":
    time.sleep(4)
if mode == "timeout":
    time.sleep(30)
if mode == "exit":
    print("PRIVATE OUTPUT", file=sys.stderr)
    sys.exit(1)
if mode == "json":
    print("PRIVATE OUTPUT")
    sys.exit(0)
used = 70 if folder else 10
windows = [
    {{"id": key, "percentUsed": used,
      "resetsAt": "2026-11-01T01:00:00Z", "private": "secret"}}
    for key in ("five_hour", "seven_day")
]
windows.append({{"id": "model:claude-opus", "label": "Opus week", "kind": "model",
                "percentUsed": 40, "resetsAt": "2026-11-02T01:00:00Z"}})
windows.append({{"id": "model:share", "label": "Sonnet week", "kind": "model",
                "percentUsed": 5, "resetsAt": "2026-11-02T01:00:00Z", "shareOf": "seven_day"}})
if mode in ("missing", "stale"):
    windows.pop(1)
if mode == "invalid":
    windows[0]["percentUsed"] = -1
if mode == "reset":
    windows[0]["resetsAt"] = "secret"
print(json.dumps({{"providers": [{{
    "provider": "claude",
    "state": {{
        "status": {{"auth": "auth_required", "stale": "stale"}}.get(mode, "fresh"),
        "refreshedAt": "2026-10-31T08:00:00Z",
        "error": "PRIVATE ERROR",
    }},
    "windows": windows,
    "account": {{"email": "private@example.com"}},
}}]}}))
""")
    binary.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    return binary


def test_quota_windows_are_separate_and_sanitized(quota_host, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "private-token")
    monkeypatch.setenv("CLAUDE_SECURESTORAGE_CONFIG_DIR", "other-account")
    readings = asyncio.run(claude_quotas.read_claude_quotas())
    assert [q.account for q in readings] == ["default", "work"]
    assert [q.five_hour.percent_used for q in readings] == [10, 70]
    assert [q.seven_day.percent_used for q in readings] == [10, 70]
    assert all(q.five_hour.resets_at.isoformat() == "2026-11-01T01:00:00+00:00" for q in readings)
    assert all(q.status == "available" for q in readings)
    assert all(q.observed_at.isoformat() == "2026-10-31T08:00:00+00:00" for q in readings)
    # Independent model limits are kept; shares of another window are not.
    assert [(w.model, w.percent_used) for w in readings[0].model_windows] == [("Opus", 40)]
    frame = HostClaudeQuotasResultFrame("request", readings)
    wire = encode_host_frame(frame)
    assert decode_host_frame(wire) == frame
    assert "private" not in wire and "secret" not in wire and str(quota_host.parent) not in wire
    assert "PRIVATE" not in wire


def test_slow_quota_readings_are_available(quota_host, monkeypatch):
    monkeypatch.setenv("FAKE_QUOTA_MODE", "slow")
    config = {
        "providers": {
            f"claude-work{index}": {
                "kind": "subscription",
                "cli": "claude",
                "config_dir": str(quota_host.parent / "work"),
            }
            for index in range(4)
        }
    }
    monkeypatch.setattr("omnigent.config.load_global_config", lambda: config)
    started = time.monotonic()
    readings = asyncio.run(claude_quotas.read_claude_quotas())
    assert [q.status for q in readings] == ["available"] * 5
    assert time.monotonic() - started < 7


def test_stale_reading_keeps_surviving_windows_and_snapshot_time(quota_host, monkeypatch):
    monkeypatch.setenv("FAKE_QUOTA_MODE", "stale")
    readings = asyncio.run(claude_quotas.read_claude_quotas())
    assert [(q.status, q.five_hour is None, q.seven_day) for q in readings] == [
        ("stale", False, None),
        ("stale", False, None),
    ]
    assert readings[0].observed_at.isoformat() == "2026-10-31T08:00:00+00:00"


_FIVE_HOUR = {"id": "five_hour", "percentUsed": 1, "resetsAt": "2026-11-01T01:00:00Z"}


@pytest.mark.parametrize(
    ("state", "windows"),
    [
        ({"status": "stale"}, [_FIVE_HOUR]),
        ({"status": "stale", "refreshedAt": "2026-10-31T08:00:00Z"}, []),
        ({"status": "error", "refreshedAt": "2026-10-31T08:00:00Z"}, []),
    ],
)
def test_unprovable_snapshots_are_unavailable(state, windows):
    report = {"providers": [{"provider": "claude", "state": state, "windows": windows}]}
    assert claude_quotas.parse_quota_report("work", report).status == "unavailable"


@pytest.mark.parametrize(
    "mode", ["timeout", "exit", "json", "missing", "invalid", "reset", "auth", "absent"]
)
def test_quota_failures_are_unavailable(quota_host, monkeypatch, capsys, mode):
    monkeypatch.setenv("FAKE_QUOTA_MODE", mode)
    monkeypatch.setattr(claude_quotas, "QUOTA_TIMEOUT_SECONDS", 0.1)
    if mode == "absent":
        quota_host.unlink()
    assert [
        (q.account, q.status, q.five_hour, q.seven_day)
        for q in asyncio.run(claude_quotas.read_claude_quotas())
    ] == [("default", "unavailable", None, None), ("work", "unavailable", None, None)]
    assert capsys.readouterr().out == ""
