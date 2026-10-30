"""Verify an explicitly selected Claude account in the native child's environment."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

from omnigent.native.launch_environment import native_launch_environment

_logger = logging.getLogger(__name__)


def selected_claude_config_dir(
    launch_env: Mapping[str, str] | None = None,
) -> Path | None:
    """Return the ``CLAUDE_CONFIG_DIR`` this process's Claude child uses, if any.

    A runner's explicit launch value reaches the child but not this process's
    env, so it wins; an empty value selects Claude's default ``~/.claude``.

    :param launch_env: Client launch values, or ``None`` for the installed runner map.
    :returns: e.g. ``Path("/accounts/work")``, or ``None`` for the default.
    """
    launch_env = native_launch_environment() if launch_env is None else launch_env
    configured = launch_env.get("CLAUDE_CONFIG_DIR", os.environ.get("CLAUDE_CONFIG_DIR"))
    return Path(configured).expanduser() if configured else None


def claude_auth_status(command: str, *, env: Mapping[str, str], cwd: Path) -> bool | None:
    """Return ``claude auth status``'s structural verdict, never its output.

    :returns: ``True`` for a signed-in account, ``False`` for a definitive
        signed-out verdict, and ``None`` when the probe is inconclusive.
    """
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
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError):
        return None
    if isinstance(status, dict):
        if status.get("loggedIn") is False:
            return False
        if status.get("loggedIn") is True and result.returncode == 0:
            return True
    return None


def ensure_claude_account_signed_in(
    command: str | None,
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
    if command is None:
        _logger.warning(
            "native-claude: could not verify account %r because the harness "
            "env wrapper could not be resolved; continuing launch.",
            requested_config_dir,
        )
        return
    logged_in = claude_auth_status(command, env=env, cwd=cwd)
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
