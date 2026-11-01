"""Read-only Claude quota windows, reduced to owner-visible account data."""

from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

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

    ``stale`` means quota-axi served its last successful reading because a
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


def read_claude_quotas() -> list[ClaudeAccountQuota]:
    """Read every configured Claude account without refreshing credentials."""
    from omnigent.config import load_global_config
    from omnigent.onboarding.claude_accounts import (
        account_launch_environment,
        account_name_from_provider,
    )
    from omnigent.onboarding.provider_config import load_providers

    command = shutil.which("quota-axi")
    folders = {"default": os.environ.get("CLAUDE_CONFIG_DIR") or None}
    try:
        config = load_global_config()
        for name, entry in load_providers(config).items():
            account = account_name_from_provider(name)
            if (
                account is not None
                and entry.cli == "claude"
                and entry.kind == "subscription"
                and entry.config_dir
            ):
                folders[account] = account_launch_environment(
                    account, None, host_name="this host", config=config
                )["CLAUDE_CONFIG_DIR"]
    except (OSError, ValueError):
        return [ClaudeAccountQuota(account="default")]
    # Read all accounts together so batching cannot exceed the host reply deadline.
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(folders)) as pool:
        futures = [
            pool.submit(_read_quota, account, folder, command)
            for account, folder in folders.items()
        ]
        return [future.result() for future in futures]
