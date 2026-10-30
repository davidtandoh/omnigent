"""Extra Claude subscription accounts, each signed in under its own config folder.

Claude Code keys a login on the exact ``CLAUDE_CONFIG_DIR`` string, so each
extra account is a ``kind: subscription, cli: claude`` provider whose
``config_dir`` names a folder under the active Omnigent config home. The
default login (no ``config_dir``) is the CLI's own ``~/.claude``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from omnigent.onboarding.provider_config import SUBSCRIPTION_KIND, ProviderEntry, load_providers

ACCOUNTS_DIRNAME = "claude-accounts"
_ACCOUNT_NAME_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_ACCOUNT_NAME_MAX_LENGTH = 32
# ``claude-subscription`` is the default login's provider name.
_RESERVED_ACCOUNT_NAMES = frozenset({"subscription", "default"})
_SSH_ENV_VARS = ("SSH_CONNECTION", "SSH_TTY", "SSH_CLIENT")
_LOGIN_ARGS = ("auth", "login", "--claudeai")
_LOGOUT_ARGS = ("auth", "logout")


def account_provider_name(account_name: str) -> str:
    """Return the ``providers:`` key for *account_name*, e.g. ``"claude-work"``."""
    return f"claude-{account_name}"


def account_name_error(account_name: str, providers: Mapping[str, object]) -> str | None:
    """Return why *account_name* cannot name a new account, or ``None`` when it can.

    :param account_name: The short name the user typed, e.g. ``"work"``.
    :param providers: The raw ``providers:`` mapping, used to reject duplicates.
    """
    if not account_name:
        return "Enter a short name, e.g. work."
    if len(account_name) > _ACCOUNT_NAME_MAX_LENGTH:
        return f"Use at most {_ACCOUNT_NAME_MAX_LENGTH} characters."
    if _ACCOUNT_NAME_PATTERN.fullmatch(account_name) is None:
        return "Use lowercase letters, digits, and single hyphens, e.g. work or client-a."
    if account_name in _RESERVED_ACCOUNT_NAMES:
        return f"{account_name!r} is reserved. Choose another name."
    if account_provider_name(account_name) in providers:
        return f"A provider named {account_provider_name(account_name)!r} already exists."
    return None


def account_config_dir(config_home: Path, account_name: str) -> str:
    """Return the absolute ``CLAUDE_CONFIG_DIR`` for *account_name*.

    The string is stored and passed to Claude unchanged, so it is made
    absolute here and never contains ``~`` or resolved symlinks.

    :param config_home: The active Omnigent config home, i.e. the folder
        holding ``config.yaml``.
    """
    return os.path.join(os.path.abspath(config_home), ACCOUNTS_DIRNAME, account_name)


def ssh_login_refusal(
    *, platform: str | None = None, env: Mapping[str, str] | None = None
) -> str | None:
    """Return why a Claude login cannot run here, or ``None`` when it can.

    On macOS Claude stores the login in the user's login keychain, which an
    SSH session cannot unlock; the login would appear to finish but not stick.

    :param platform: ``sys.platform`` value; ``None`` reads the current one.
    :param env: Environment to inspect; ``None`` reads ``os.environ``.
    """
    platform = sys.platform if platform is None else platform
    env = os.environ if env is None else env
    if platform != "darwin" or not any(env.get(name) for name in _SSH_ENV_VARS):
        return None
    return (
        "This is an SSH session. Claude stores each sign-in in the macOS keychain, "
        "which an SSH session cannot open. Run this step on the Mac's own screen "
        "or through Screen Sharing."
    )


def _account_env(config_dir: str) -> dict[str, str]:
    return {**os.environ, "CLAUDE_CONFIG_DIR": config_dir}


def account_signed_in(command: str, config_dir: str) -> bool:
    """Return whether ``claude auth status`` reports *config_dir* signed in.

    An inconclusive probe counts as not signed in. Status output is never shown.
    """
    from omnigent.harnesses.claude_native.account import claude_auth_status

    return claude_auth_status(command, env=_account_env(config_dir), cwd=Path(config_dir)) is True


def sign_in_account(command: str, config_dir: str) -> bool:
    """Run ``claude auth login`` for *config_dir* unless it is already signed in.

    :returns: Whether ``claude auth status`` confirms the account afterward.
    """
    return account_signed_in(command, config_dir) or log_in_account(command, config_dir)


def log_in_account(command: str, config_dir: str) -> bool:
    """Run ``claude auth login`` for *config_dir*, even when it reports signed in.

    A stored but expired sign-in can still report signed in, so a requested
    re-login always runs the login.

    :returns: Whether ``claude auth status`` confirms the account afterward.
    """
    from omnigent.onboarding.harness_install import run_interactive_login

    if not run_interactive_login([command, *_LOGIN_ARGS], env=_account_env(config_dir)):
        return False
    return account_signed_in(command, config_dir)


def sign_out_account(command: str, config_dir: str) -> bool:
    """Run ``claude auth logout`` for *config_dir* only.

    :returns: Whether the account no longer reports signed in.
    """
    try:
        subprocess.run(
            [command, *_LOGOUT_ARGS],
            env=_account_env(config_dir),
            cwd=config_dir,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return not account_signed_in(command, config_dir)


def claude_command() -> str | None:
    """Return the ``claude`` executable on ``PATH``, or ``None`` when missing."""
    return shutil.which("claude")


def find_account(config: dict[str, object], selector: str) -> ProviderEntry | None:
    """Return the Claude subscription named *selector*, or ``None``.

    :param selector: A provider name (``"claude-work"``) or the short account
        name it was created from (``"work"``).
    """
    providers = load_providers(config)
    for name in (selector, account_provider_name(selector)):
        entry = providers.get(name)
        if entry is not None and entry.kind == SUBSCRIPTION_KIND and entry.cli == "claude":
            return entry
    return None
