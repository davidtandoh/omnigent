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


def account_name_format_error(account_name: str) -> str | None:
    """Return why *account_name* is not a well-formed account name, or ``None``.

    Checks only the shape, so the server can validate a session's account
    without reading any machine's config.

    :param account_name: The short name, e.g. ``"work"``.
    """
    if not account_name:
        return "Enter a short name, e.g. work."
    if len(account_name) > _ACCOUNT_NAME_MAX_LENGTH:
        return f"Use at most {_ACCOUNT_NAME_MAX_LENGTH} characters."
    if _ACCOUNT_NAME_PATTERN.fullmatch(account_name) is None:
        return "Use lowercase letters, digits, and single hyphens, e.g. work or client-a."
    if account_name in _RESERVED_ACCOUNT_NAMES:
        return f"{account_name!r} is reserved. Choose another name."
    return None


def account_name_from_provider(provider_name: str) -> str | None:
    """Return the account name a ``claude-<name>`` provider key spells, or ``None``.

    The inverse of :func:`account_provider_name`. ``claude-subscription``
    (the default login) and malformed names return ``None``.

    :param provider_name: A ``providers:`` key, e.g. ``"claude-work"``.
    """
    prefix = account_provider_name("")
    if not provider_name.startswith(prefix):
        return None
    account_name = provider_name.removeprefix(prefix)
    return account_name if account_name_format_error(account_name) is None else None


def account_name_error(account_name: str, providers: Mapping[str, object]) -> str | None:
    """Return why *account_name* cannot name a new account, or ``None`` when it can.

    :param account_name: The short name the user typed, e.g. ``"work"``.
    :param providers: The raw ``providers:`` mapping, used to reject duplicates.
    """
    if (format_error := account_name_format_error(account_name)) is not None:
        return format_error
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
    from omnigent.harnesses.claude_native.account import claude_auth_status

    return claude_auth_status(command, env=_account_env(config_dir), cwd=Path(config_dir)) is False


def claude_command() -> str | None:
    """Return the ``claude`` executable on ``PATH``, or ``None`` when missing."""
    return shutil.which("claude")


def find_account(config: dict[str, object], selector: str) -> ProviderEntry | None:
    """Return the Claude subscription named *selector*, or ``None``.

    :param selector: The short account name it was created from (``"work"``).
    """
    entry = load_providers(config).get(account_provider_name(selector))
    if (
        entry is not None
        and entry.kind == SUBSCRIPTION_KIND
        and entry.cli == "claude"
        and entry.config_dir is not None
    ):
        return entry
    return None


def account_name_for_config_dir(config: dict[str, object], config_dir: str) -> str | None:
    """Return the account name whose sign-in folder is *config_dir*, or ``None``.

    :param config: Parsed user config.
    :param config_dir: A ``CLAUDE_CONFIG_DIR`` value, e.g.
        ``"/home/u/.omnigent/claude-accounts/work"``.
    """
    for name, entry in load_providers(config).items():
        if (
            entry.kind == SUBSCRIPTION_KIND
            and entry.cli == "claude"
            and entry.config_dir == config_dir
        ):
            return account_name_from_provider(name)
    return None


class AccountUnavailableError(Exception):
    """A session's recorded Claude account cannot be used on this machine.

    The message is user-facing: it names the account, the machine, and the fix.
    """


def account_launch_environment(
    account: str,
    native_env: Mapping[str, str] | None,
    *,
    host_name: str,
    config: dict[str, object] | None = None,
) -> dict[str, str]:
    """Return *native_env* with *account*'s ``CLAUDE_CONFIG_DIR`` added.

    Resolves the name against this machine's own user config, so the folder
    path never comes from the server. Never falls back to the default login.

    :param account: The session's account name, e.g. ``"work"``.
    :param native_env: Explicit launch values already requested, e.g. from
        ``--env``; not mutated.
    :param host_name: This machine's host name, for the refusal message.
    :param config: Parsed user config; ``None`` reads the global config.
    :returns: A new launch environment selecting the account.
    :raises AccountUnavailableError: If the account is not set up here, the
        config cannot be read, or ``--env`` selects a different folder.
    """
    import yaml

    from omnigent.cli_invocation import cli_invocation
    from omnigent.errors import OmnigentError

    try:
        if config is None:
            from omnigent.config import load_global_config

            config = load_global_config()
        entry = find_account(config, account)
    except (OSError, ValueError, yaml.YAMLError, OmnigentError) as exc:
        raise AccountUnavailableError(
            f"Claude account {account!r} cannot be checked on host {host_name!r}: "
            f"its Omnigent config could not be read ({exc})."
        ) from None
    if entry is None or entry.config_dir is None:
        raise AccountUnavailableError(
            f"Claude account {account!r} is not set up on host {host_name!r}. "
            f"Run `{cli_invocation()} setup` on that machine to add it, "
            "or start a new session on another account."
        )
    requested = dict(native_env or {})
    if requested.get("CLAUDE_CONFIG_DIR", entry.config_dir) != entry.config_dir:
        raise AccountUnavailableError(
            f"Claude account {account!r} and --env CLAUDE_CONFIG_DIR select different "
            "sign-in folders. Pass only one of them."
        )
    return {**requested, "CLAUDE_CONFIG_DIR": entry.config_dir}
