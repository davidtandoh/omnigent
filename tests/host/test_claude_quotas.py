"""Quota reads execute only the selected account and publish an allowlist."""

from __future__ import annotations

import sys
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
assert sys.argv[1:] == ["--provider", "claude", "--json", "--no-credential-refresh"]
folder = os.environ.get("CLAUDE_CONFIG_DIR")
if folder:
    assert folder == {str(tmp_path / "work")!r}
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ
    assert "CLAUDE_SECURESTORAGE_CONFIG_DIR" not in os.environ
mode = os.environ.get("FAKE_QUOTA_MODE")
if mode == "timeout":
    time.sleep(2)
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
if mode == "missing":
    windows.pop()
if mode == "invalid":
    windows[0]["percentUsed"] = -1
if mode == "reset":
    windows[0]["resetsAt"] = "secret"
print(json.dumps({{"providers": [{{
    "provider": "claude",
    "state": {{"status": "auth_required" if mode == "auth" else "fresh"}},
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
    readings = claude_quotas.read_claude_quotas()
    assert [q.account for q in readings] == ["default", "work"]
    assert [q.five_hour.percent_used for q in readings] == [10, 70]
    assert [q.seven_day.percent_used for q in readings] == [10, 70]
    assert all(q.five_hour.resets_at.isoformat() == "2026-11-01T01:00:00+00:00" for q in readings)
    frame = HostClaudeQuotasResultFrame("request", readings)
    wire = encode_host_frame(frame)
    assert decode_host_frame(wire) == frame
    assert "private" not in wire and "secret" not in wire and str(quota_host.parent) not in wire


@pytest.mark.parametrize(
    "mode", ["timeout", "exit", "json", "missing", "invalid", "reset", "auth", "absent"]
)
def test_quota_failures_are_unavailable(quota_host, monkeypatch, capsys, mode):
    monkeypatch.setenv("FAKE_QUOTA_MODE", mode)
    monkeypatch.setattr(claude_quotas, "QUOTA_TIMEOUT_SECONDS", 0.1)
    if mode == "absent":
        quota_host.unlink()
    assert [
        (q.account, q.status, q.five_hour, q.seven_day) for q in claude_quotas.read_claude_quotas()
    ] == [("default", "unavailable", None, None), ("work", "unavailable", None, None)]
    assert capsys.readouterr().out == ""
