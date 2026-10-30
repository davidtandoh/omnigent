"""Server rules for a session's Claude account: create-time choice and host support."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.entities import Conversation
from omnigent.errors import OmnigentError
from omnigent.host.frames import CAP_CLAUDE_ACCOUNT
from omnigent.server.routes._host_launch import account_launch_refusal
from omnigent.server.routes._sessions.helpers import _resolve_create_account


def _parent(account: str | None) -> Conversation:
    return Conversation(
        id="conv_parent",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_parent",
        account=account,
    )


@pytest.mark.parametrize("requested", [None, "work"])
def test_top_level_session_records_only_the_explicit_account(requested: str | None) -> None:
    assert (
        _resolve_create_account(requested=requested, harness="claude-native", parent=None)
        == requested
    )


def test_account_is_rejected_off_claude_native() -> None:
    with pytest.raises(OmnigentError, match="only to Claude Code"):
        _resolve_create_account(requested="work", harness="codex-native", parent=None)


@pytest.mark.parametrize(("parent_account", "requested"), [("work", None), ("work", "work")])
def test_child_keeps_its_parents_account(parent_account: str, requested: str | None) -> None:
    """The store copies the parent's account, so the route records nothing extra."""
    assert (
        _resolve_create_account(
            requested=requested,
            harness="claude-native",
            parent=_parent(parent_account),
        )
        is None
    )


@pytest.mark.parametrize(("parent_account", "requested"), [("work", "client-a"), (None, "work")])
def test_child_cannot_switch_accounts(parent_account: str | None, requested: str) -> None:
    with pytest.raises(OmnigentError, match="parent's Claude account") as caught:
        _resolve_create_account(
            requested=requested,
            harness="claude-native",
            parent=_parent(parent_account),
        )
    if parent_account:
        assert parent_account not in str(caught.value)


def _conn(capabilities: list[str]) -> SimpleNamespace:
    return SimpleNamespace(hello=SimpleNamespace(name="mac-mini", capabilities=capabilities))


def test_account_launch_refusal_requires_host_support() -> None:
    assert account_launch_refusal(None, _conn([])) is None  # type: ignore[arg-type]
    assert account_launch_refusal("work", _conn([CAP_CLAUDE_ACCOUNT])) is None  # type: ignore[arg-type]
    refusal = account_launch_refusal("work", _conn([]))  # type: ignore[arg-type]
    assert refusal is not None
    assert "'work'" in refusal and "'mac-mini'" in refusal and "Upgrade Omnigent" in refusal
