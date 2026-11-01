"""Read-only Claude quota windows, reduced to owner-visible account data."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omnigent.host.usage_limits import AccountUsage, claude_account_folders, read_usage_limits

QUOTA_TIMEOUT_SECONDS = 20.0
MODEL_WINDOWS_MAX = 8
MODEL_LABEL_MAX_CHARS = 64


class QuotaWindow(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    percent_used: float = Field(ge=0, le=100, allow_inf_nan=False)
    resets_at: datetime


class ModelQuotaWindow(QuotaWindow):
    """A weekly limit scoped to one model family, e.g. ``"Opus"``."""

    model: str = Field(min_length=1, max_length=MODEL_LABEL_MAX_CHARS)


class ClaudeAccountQuota(BaseModel):
    """One account's quota snapshot; never carries identity or credentials.

    ``stale`` means the reader served its last successful reading because a
    fresh read failed. ``observed_at`` is when the vendor reported the
    windows, so readers can show the snapshot age.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    account: str
    status: Literal["available", "stale", "unavailable"] = "unavailable"
    five_hour: QuotaWindow | None = None
    seven_day: QuotaWindow | None = None
    model_windows: list[ModelQuotaWindow] = Field(default_factory=list)
    observed_at: datetime | None = None


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo is not None else None


def _parse_window(window: dict[str, object]) -> QuotaWindow | None:
    reset = _parse_time(window.get("resetsAt"))
    used = window.get("percentUsed")
    if reset is None or isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    return QuotaWindow(percent_used=float(used), resets_at=reset)


def _model_label(window: dict[str, object]) -> str | None:
    label = window.get("label")
    if not isinstance(label, str):
        return None
    label = label.strip().removesuffix(" week").strip()
    if not label or len(label) > MODEL_LABEL_MAX_CHARS or not label.isprintable():
        return None
    return label[0].upper() + label[1:]


def parse_quota_report(account: str, data: object) -> ClaudeAccountQuota:
    """Reduce one ``quota-axi --provider claude --json`` report to an allowlist."""
    unavailable = ClaudeAccountQuota(account=account)
    providers = data.get("providers") if isinstance(data, dict) else None
    if not isinstance(providers, list) or len(providers) != 1:
        return unavailable
    provider = providers[0]
    if not isinstance(provider, dict) or provider.get("provider") != "claude":
        return unavailable
    state = provider.get("state")
    if not isinstance(state, dict) or state.get("status") not in ("fresh", "stale"):
        return unavailable
    status = state["status"]
    windows = provider.get("windows")
    if not isinstance(windows, list):
        return unavailable
    selected: dict[str, QuotaWindow] = {}
    models: list[ModelQuotaWindow] = []
    for window in windows:
        if not isinstance(window, dict) or "shareOf" in window:
            continue
        key = window.get("id")
        if key in ("five_hour", "seven_day"):
            if key in selected or (parsed := _parse_window(window)) is None:
                return unavailable
            selected[key] = parsed
        elif window.get("kind") == "model" and len(models) < MODEL_WINDOWS_MAX:
            label = _model_label(window)
            parsed = _parse_window(window)
            if label is not None and parsed is not None:
                models.append(ModelQuotaWindow(model=label, **parsed.model_dump()))
    # A fresh reading must cover both windows; a stale one keeps what survived.
    if (status == "fresh" and set(selected) != {"five_hour", "seven_day"}) or not selected:
        return unavailable
    observed = _parse_time(state.get("refreshedAt"))
    if observed is None and status == "stale":
        return unavailable
    return ClaudeAccountQuota(
        account=account,
        status="available" if status == "fresh" else "stale",
        five_hour=selected.get("five_hour"),
        seven_day=selected.get("seven_day"),
        model_windows=models,
        observed_at=observed or datetime.now(UTC),
    )


def _read_quota(account: str, folder: str | None, command: str | None) -> ClaudeAccountQuota:
    unavailable = ClaudeAccountQuota(account=account)
    if command is None:
        return unavailable
    env = dict(os.environ)
    if folder is None:
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = folder
        env.pop("CLAUDE_CODE_OAUTH_TOKEN", None)
        env.pop("CLAUDE_SECURESTORAGE_CONFIG_DIR", None)
    try:
        result = subprocess.run(
            [
                command,
                "--provider",
                "claude",
                "--json",
                "--no-credential-refresh",
                "--max-age",
                "60s",
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=QUOTA_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode != 0:
            return unavailable
        return parse_quota_report(account, json.loads(result.stdout))
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError, ValidationError):
        return unavailable


def claude_quota_from_usage(usage: AccountUsage) -> ClaudeAccountQuota:
    """Fit a native Claude reading to the account-quota contract the web reads."""
    unavailable = ClaudeAccountQuota(account=usage.account)
    if usage.status == "unavailable":
        return unavailable
    selected: dict[str, QuotaWindow] = {}
    models: list[ModelQuotaWindow] = []
    for window in usage.windows:
        # The contract needs a reset time; a window that has not started has none.
        if window.resets_at is None:
            continue
        parsed = QuotaWindow(percent_used=window.percent_used, resets_at=window.resets_at)
        if window.kind in ("session", "weekly"):
            selected.setdefault(window.kind, parsed)
        elif window.kind == "weekly_model":
            models.append(ModelQuotaWindow(model=window.label, **parsed.model_dump()))
    if not selected and not models:
        return unavailable
    return ClaudeAccountQuota(
        account=usage.account,
        status=usage.status,
        five_hour=selected.get("session"),
        seven_day=selected.get("weekly"),
        model_windows=models,
        observed_at=usage.observed_at,
    )


def _read_with_quota_axi() -> list[ClaudeAccountQuota]:
    command = shutil.which("quota-axi")
    try:
        folders = claude_account_folders()
    except (OSError, ValueError):
        return [ClaudeAccountQuota(account="default")]
    # Read all accounts together so batching cannot exceed the host reply deadline.
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(folders)) as pool:
        futures = [
            pool.submit(_read_quota, account, folder, command)
            for account, folder in folders.items()
        ]
        return [future.result() for future in futures]


def quota_reader() -> str:
    """Return ``native`` when ``quota_reader: native`` is set in the global config.

    Temporary switch: quota-axi stays the default until the native reader is
    verified on every account. The cutover PR removes the switch together
    with the quota-axi path.
    """
    from omnigent.config import load_global_config

    try:
        value = load_global_config().get("quota_reader")
    except (OSError, ValueError):
        return "quota-axi"
    return "native" if value == "native" else "quota-axi"


async def read_claude_quotas() -> list[ClaudeAccountQuota]:
    """Read every configured Claude account with the configured reader."""
    if quota_reader() == "native":
        try:
            readings = await read_usage_limits(("claude",))
        except (OSError, ValueError):
            return [ClaudeAccountQuota(account="default")]
        return [claude_quota_from_usage(reading) for reading in readings]
    return await asyncio.to_thread(_read_with_quota_axi)


class HostQuotaSnapshot(BaseModel):
    quotas: list[ClaudeAccountQuota] = Field(default_factory=list)
    usage: list[AccountUsage] = Field(default_factory=list)


async def read_host_quota_snapshot() -> HostQuotaSnapshot:
    """Read Claude quotas and native Codex usage concurrently for the tunnel."""
    quotas, usage = await asyncio.gather(
        read_claude_quotas(), read_usage_limits(("codex",)), return_exceptions=True
    )
    return HostQuotaSnapshot(
        quotas=quotas if isinstance(quotas, list) else [ClaudeAccountQuota(account="default")],
        usage=usage
        if isinstance(usage, list)
        else [AccountUsage(provider="codex", account="default")],
    )
