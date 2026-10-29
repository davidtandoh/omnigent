"""Verify an explicitly selected Claude account in the native child's environment."""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Mapping
from pathlib import Path

_logger = logging.getLogger(__name__)


def ensure_claude_account_signed_in(
    command: str,
    requested_config_dir: str,
    *,
    env: Mapping[str, str],
    cwd: Path,
) -> None:
    """Refuse a changed selector or signed-out verdict without exposing status output.

    Ambient readiness probes cache success and inherit the runner environment.
    Account enforcement needs a fresh verdict under the child's exact environment.
    """
    if env.get("CLAUDE_CONFIG_DIR") != requested_config_dir:
        raise RuntimeError(
            "native-claude: the child would receive a different CLAUDE_CONFIG_DIR "
            "than the requested account. Check the harness launch command."
        )
    logged_in: bool | None = None
    try:
        result = subprocess.run(
            [command, "auth", "status"],
            env=env,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        status = json.loads(result.stdout)
        if isinstance(status, dict):
            if status.get("loggedIn") is False:
                logged_in = False
            elif status.get("loggedIn") is True and result.returncode == 0:
                logged_in = True
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError):
        pass
    if logged_in is False:
        raise RuntimeError(
            "native-claude: the selected CLAUDE_CONFIG_DIR account is not signed in. "
            "Run claude auth login with that exact CLAUDE_CONFIG_DIR before launching."
        )
    if logged_in is None:
        _logger.warning(
            "native-claude: could not verify account %r with claude auth status; "
            "continuing launch without a verified login.",
            requested_config_dir,
        )
