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
    with pytest.raises(OmnigentError, match="requires the claude-native harness"):
        _resolve_create_account(requested="work", harness="codex-native", parent=None)


@pytest.mark.parametrize(("parent_account", "requested"), [("work", None), ("work", "work")])
def test_child_keeps_its_parents_account(parent_account: str, requested: str | None) -> None:
    """Creation resolves the inherited account before persistence."""
    assert (
        _resolve_create_account(
            requested=requested,
            harness="claude-native",
            parent=_parent(parent_account),
        )
        == parent_account
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


@pytest.mark.parametrize("harness", ["codex-native", "pi", "openai-agents"])
def test_non_claude_child_has_no_account(harness: str) -> None:
    assert (
        _resolve_create_account(requested=None, harness=harness, parent=_parent("private-work"))
        is None
    )


def test_sdk_child_cannot_inherit_a_claude_account() -> None:
    with pytest.raises(OmnigentError, match="requires the claude-native harness") as caught:
        _resolve_create_account(
            requested=None, harness="claude-sdk", parent=_parent("private-work")
        )
    assert "private-work" not in str(caught.value)


def test_agent_replacement_checks_all_account_sessions(db_uri: str) -> None:
    from omnigent.server.routes._session_create_validation import validate_agent_account_sessions
    from omnigent.spec.types import AgentSpec, ExecutorSpec
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

    store = SqlAlchemyConversationStore(db_uri)
    protected = store.create_conversation(
        agent_id="087b7cb7ac30abf4debfaa578d052ec6", account="private-work"
    )
    for _ in range(100):
        store.create_conversation(agent_id="087b7cb7ac30abf4debfaa578d052ec6")
    replacement = AgentSpec(
        spec_version=1, executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"})
    )
    with pytest.raises(OmnigentError, match="requires the claude-native harness") as caught:
        validate_agent_account_sessions(store, "087b7cb7ac30abf4debfaa578d052ec6", replacement)
    assert "private-work" not in str(caught.value)
    assert store.get_conversation(protected.id).account == "private-work"
