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


def _account_label(account: str | None) -> str:
    """Name *account* and this machine for a refusal message.

    :returns: e.g. ``"Claude account 'work' on host 'mac-mini'"``.
    """
    import socket

    from omnigent.config import load_global_config

    try:
        host = load_global_config().get("host")
        host_name = host.get("name") if isinstance(host, dict) else None
    except Exception:  # noqa: BLE001 - the label must never hide the refusal
        host_name = None
    subject = f"Claude account {account!r}" if account else "The selected Claude account"
    return f"{subject} on host {str(host_name or socket.gethostname())!r}"


def _account_name_for_config_dir(config_dir: str) -> str | None:
    """Return the account name whose sign-in folder is *config_dir*, if known."""
    from omnigent.config import load_global_config
    from omnigent.onboarding.claude_accounts import account_name_for_config_dir

    try:
        return account_name_for_config_dir(load_global_config(), config_dir)
    except Exception:  # noqa: BLE001 - the label must never hide the refusal
        return None


def ensure_spec_account_selected(
    spec_provider: str | None,
    requested_config_dir: str | None,
    *,
    config: dict[str, object] | None = None,
    session_account: str | None = None,
) -> None:
    """Require the runner to use the session and agent spec Claude account.

    An agent whose ``executor.auth`` names an extra Claude account must run on
    that account, never silently on the default login or another account.

    :param spec_provider: The spec's ``executor.auth`` provider name, e.g.
        ``"claude-work"``; ``None`` when it names none.
    :param requested_config_dir: The launch's ``CLAUDE_CONFIG_DIR``, or ``None``
        for the default login.
    :param config: Parsed user config; ``None`` reads the global config.
    :param session_account: The session's recorded account name, or ``None`` for default.
    :raises RuntimeError: If the session or spec account does not match the runner.
    """
    from omnigent.config import load_global_config
    from omnigent.onboarding.claude_accounts import (
        account_launch_environment,
        account_name_for_config_dir,
        account_name_from_provider,
    )
    from omnigent.onboarding.provider_config import load_providers

    spec_account = account_name_from_provider(spec_provider) if spec_provider else None
    if session_account is None and spec_account is None and not requested_config_dir:
        return
    config = load_global_config() if config is None else config
    if spec_account is not None:
        entry = load_providers(config).get(spec_provider)
        if entry is None or (
            entry.config_dir is not None and entry.config_dir != requested_config_dir
        ):
            raise RuntimeError(
                f"config_mismatch: this agent runs on Claude account {spec_account!r}. "
                f"Check that {_account_label(spec_account)} is set up, then launch "
                "a new runner for this account."
            )
    if session_account is not None:
        import socket

        expected = account_launch_environment(
            session_account, None, host_name=socket.gethostname(), config=config
        )["CLAUDE_CONFIG_DIR"]
        if expected == (requested_config_dir or None):
            return
    elif (
        not requested_config_dir
        or account_name_for_config_dir(config, requested_config_dir) is None
    ):
        return
    subject = _account_label(session_account) if session_account else "the default Claude login"
    raise RuntimeError(
        f"config_mismatch: this session requires {subject}, but this runner uses "
        "a different Claude account. Launch a new runner for this session."
    )


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
        from omnigent.cli_invocation import cli_invocation

        label = _account_label(_account_name_for_config_dir(requested_config_dir))
        raise RuntimeError(
            f"native-claude: {label} is signed out. "
            f"Run `{cli_invocation()} setup` on that machine and sign in to the account "
            "again, or start a new session on another account."
        )
    if logged_in is None:
        _logger.warning(
            "native-claude: could not verify account %r with claude auth status; "
            "continuing launch without a verified login.",
            requested_config_dir,
        )
