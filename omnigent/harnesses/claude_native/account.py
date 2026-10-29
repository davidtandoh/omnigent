"""Verify an explicitly selected Claude account in the native child's environment."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path


def ensure_claude_account_signed_in(
    command: str,
    requested_config_dir: str,
    *,
    env: Mapping[str, str],
    cwd: Path,
) -> None:
    """Refuse a changed selector or unusable login without exposing status output.

    Ambient readiness probes cache success and inherit the runner environment.
    Account enforcement needs a fresh verdict under the child's exact environment.
    """
    if env.get("CLAUDE_CONFIG_DIR") != requested_config_dir:
        raise RuntimeError(
            "native-claude: the child would receive a different CLAUDE_CONFIG_DIR "
            "than the requested account. Check the harness launch command."
        )
    failure = (
        "native-claude: could not verify the selected account with claude auth status; "
        "check the Claude command and retry."
    )
    try:
        result = subprocess.run(
            [command, "auth", "status"],
            env=env,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise RuntimeError(failure) from None
    try:
        status = json.loads(result.stdout)
    except ValueError:
        raise RuntimeError(failure) from None
    if not isinstance(status, dict) or not isinstance(status.get("loggedIn"), bool):
        raise RuntimeError(failure)
    if status["loggedIn"] is False:
        raise RuntimeError(
            "native-claude: the selected CLAUDE_CONFIG_DIR account is not signed in. "
            "Run claude auth login with that exact CLAUDE_CONFIG_DIR before launching."
        )
    if result.returncode != 0:
        raise RuntimeError(failure)
