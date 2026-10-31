"""Read-only Claude quota windows, reduced to owner-visible account data."""

from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import subprocess
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

QUOTA_TIMEOUT_SECONDS = 3.0


class QuotaWindow(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    percent_used: float = Field(ge=0, le=100, allow_inf_nan=False)
    resets_at: datetime


class ClaudeAccountQuota(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    account: str
    status: Literal["available", "unavailable"] = "unavailable"
    five_hour: QuotaWindow | None = None
    seven_day: QuotaWindow | None = None


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
            [command, "--provider", "claude", "--json", "--no-credential-refresh"],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=QUOTA_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode != 0:
            return unavailable
        data = json.loads(result.stdout)
        providers = data.get("providers") if isinstance(data, dict) else None
        if not isinstance(providers, list) or len(providers) != 1:
            return unavailable
        provider = providers[0]
        if not isinstance(provider, dict) or provider.get("provider") != "claude":
            return unavailable
        state = provider.get("state")
        if not isinstance(state, dict) or state.get("status") != "fresh":
            return unavailable
        windows = provider.get("windows")
        if not isinstance(windows, list):
            return unavailable
        selected: dict[str, QuotaWindow] = {}
        for window in windows:
            if not isinstance(window, dict) or window.get("id") not in ("five_hour", "seven_day"):
                continue
            key = window["id"]
            if key in selected:
                return unavailable
            reset = window.get("resetsAt")
            used = window.get("percentUsed")
            if (
                not isinstance(reset, str)
                or isinstance(used, bool)
                or not isinstance(used, (int, float))
            ):
                return unavailable
            parsed = datetime.fromisoformat(reset.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                return unavailable
            selected[key] = QuotaWindow(percent_used=float(used), resets_at=parsed)
        if set(selected) != {"five_hour", "seven_day"}:
            return unavailable
        return ClaudeAccountQuota(
            account=account,
            status="available",
            five_hour=selected["five_hour"],
            seven_day=selected["seven_day"],
        )
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
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = [
            pool.submit(_read_quota, account, folder, command)
            for account, folder in folders.items()
        ]
        return [future.result() for future in futures]
