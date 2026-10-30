"""Extra Claude accounts: naming, folders, SSH refusal, and provider parsing."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.errors import OmnigentError
from omnigent.onboarding.ambient import DetectedProvider
from omnigent.onboarding.claude_accounts import (
    account_config_dir,
    account_name_error,
    find_account,
    ssh_login_refusal,
)
from omnigent.onboarding.detected import effective_config_with_detected
from omnigent.onboarding.provider_config import (
    ANTHROPIC_FAMILY,
    first_available_provider,
    load_providers,
)

_WORK = {"kind": "subscription", "cli": "claude", "config_dir": "/accounts/work"}


@pytest.mark.parametrize("name", ["work", "client-a", "a1", "x" * 32])
def test_valid_account_names(name: str) -> None:
    assert account_name_error(name, {}) is None


@pytest.mark.parametrize(
    "name",
    ["", "Work", "../x", "a/b", "a.b", "a b", "-a", "a-", "a--b", "x" * 33, "subscription"],
)
def test_unsafe_or_reserved_account_names_are_rejected(name: str) -> None:
    assert account_name_error(name, {}) is not None


def test_duplicate_account_name_is_rejected() -> None:
    assert "already exists" in str(account_name_error("work", {"claude-work": _WORK}))


def test_account_folder_is_absolute_under_the_config_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert account_config_dir(Path("home"), "work") == str(tmp_path / "home/claude-accounts/work")


@pytest.mark.parametrize("ssh_var", ["SSH_CONNECTION", "SSH_TTY", "SSH_CLIENT"])
def test_ssh_login_is_refused_only_on_macos(ssh_var: str) -> None:
    refusal = ssh_login_refusal(platform="darwin", env={ssh_var: "1"})
    assert refusal is not None and "Screen Sharing" in refusal
    assert ssh_login_refusal(platform="linux", env={ssh_var: "1"}) is None
    assert ssh_login_refusal(platform="darwin", env={ssh_var: ""}) is None


def test_find_account_resolves_short_names_exactly() -> None:
    config = {"providers": {"claude-work": _WORK, "anthropic": {"kind": "key"}}}
    assert (
        find_account({"providers": {"claude-work": _WORK}}, "work").config_dir == "/accounts/work"
    )
    assert find_account({"providers": {"claude-work": _WORK}}, "claude-work") is None
    other = {**_WORK, "config_dir": "/accounts/claude-work"}
    assert (
        find_account(
            {"providers": {"claude-work": _WORK, "claude-claude-work": other}}, "claude-work"
        ).config_dir
        == "/accounts/claude-work"
    )
    assert find_account({"providers": {"claude-work": _WORK}}, "personal") is None
    with pytest.raises(OmnigentError):
        find_account(config, "work")  # a malformed sibling is still a config error


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({**_WORK, "config_dir": "accounts/work"}, "absolute path"),
        ({**_WORK, "config_dir": "~/accounts/work"}, "absolute path"),
        ({**_WORK, "cli": "codex"}, "only supported for cli: claude"),
        (
            {**_WORK, "default": True},
            "cannot be a default.*`omnigent claude --account work`",
        ),
    ],
)
def test_hand_edited_account_entries_fail_with_a_fix(
    entry: dict[str, object], message: str
) -> None:
    with pytest.raises(OmnigentError, match=message):
        load_providers({"providers": {"claude-work": entry}})


def test_config_dir_is_kept_byte_for_byte() -> None:
    entry = {**_WORK, "config_dir": "/accounts/café/"}
    assert load_providers({"providers": {"x": entry}})["x"].config_dir == "/accounts/café/"


def test_extra_account_is_never_the_fallback_provider() -> None:
    assert (
        first_available_provider({"providers": {"claude-work": _WORK}}, ANTHROPIC_FAMILY) is None
    )


def test_extra_account_does_not_hide_the_detected_default_login() -> None:
    detected = [
        DetectedProvider(
            name="claude", kind="subscription", family=ANTHROPIC_FAMILY, source="claude CLI login"
        )
    ]
    merged = effective_config_with_detected({"providers": {"claude-work": _WORK}}, detected)
    providers = load_providers(merged)
    assert providers["claude"].config_dir is None
    assert ANTHROPIC_FAMILY in providers["claude"].default_families


def test_default_login_is_not_an_extra_account() -> None:
    assert (
        find_account(
            {
                "providers": {
                    "claude-subscription": {
                        "kind": "subscription",
                        "cli": "claude",
                        "default": True,
                    }
                }
            },
            "subscription",
        )
        is None
    )
