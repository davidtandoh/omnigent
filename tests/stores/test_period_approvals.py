from __future__ import annotations

import pytest

from omnigent.db.account_authority import account_authority_scope
from omnigent.db.db_models import workspace_scope
from omnigent.errors import OmnigentError
from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.mark.parametrize("initial,higher,lower", [(50, 75, 25), (0.5, 1.25, 0.25)])
def test_approval_checkpoint_only_increases(db_uri, initial, higher, lower):
    store = SqlAlchemyConversationStore(db_uri)
    reader = SqlAlchemyConversationStore(db_uri)
    for amount, expected in (
        (initial, initial),
        (higher, higher),
        (lower, higher),
        (higher, higher),
    ):
        store.set_period_ask_approved("alice", "month", "2026-10-01", amount)
        assert reader.list_period_ask_approvals("alice", "2026-10-01") == [
            {"period": "month", "window_start": "2026-10-01", "ask_approved_usd": expected}
        ]


def test_approvals_isolate_workspace_user_period_and_window(db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    for workspace in (0, 42):
        with workspace_scope(workspace):
            store.set_period_ask_approved("alice", "month", "2026-10-01", 50 + workspace)
    store.set_period_ask_approved("alice", "month", "2026-10-01", 25)
    store.set_period_ask_approved("alice", "quarter", "2026-10-01", 100)
    store.set_period_ask_approved("alice", "month", "2026-11-01", 75)
    store.set_period_ask_approved("bob", "month", "2026-10-01", 10)
    assert store.list_period_ask_approvals("alice", "2026-10-01") == [
        {"period": "month", "window_start": "2026-10-01", "ask_approved_usd": 50},
        {"period": "month", "window_start": "2026-11-01", "ask_approved_usd": 75},
        {"period": "quarter", "window_start": "2026-10-01", "ask_approved_usd": 100},
    ]
    with workspace_scope(42):
        assert store.list_period_ask_approvals("alice", "2026-10-01") == [
            {"period": "month", "window_start": "2026-10-01", "ask_approved_usd": 92}
        ]
    assert store.get_daily_cost_state("alice", "2026-10-13") == {
        "cost_usd": 0.0,
        "ask_approved_usd": 0.0,
    }


@pytest.mark.parametrize(
    "period,start,value",
    [
        ("day", "2026-10-01", 50),
        ("month", "2026-10-13", 50),
        ("week", "2026-10-13", 50),
        ("quarter", "2026-11-01", 50),
        ("month", "invalid", 50),
        ("month", "2026-10-01", float("nan")),
        ("month", "2026-10-01", float("inf")),
        ("month", "2026-10-01", 0),
        ("month", "2026-10-01", -1),
        ("month", "2026-10-01", True),
    ],
)
def test_invalid_approval_does_not_write(db_uri, period, start, value):
    store = SqlAlchemyConversationStore(db_uri)
    with pytest.raises(ValueError):
        store.set_period_ask_approved("alice", period, start, value)
    assert store.list_period_ask_approvals("alice", "2026-01-01") == []


def test_account_recreation_drops_approvals_and_rejects_stale_writes(db_uri):
    accounts = SqlAlchemyAccountStore(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    old = accounts.create_user_with_password("alice", "test-hash")
    with account_authority_scope("alice", old.account_generation):
        store.set_period_ask_approved("alice", "month", "2026-10-01", 50)
    assert accounts.delete_user("alice") is True
    accounts.create_user_with_password("alice", "new-test-hash")
    assert store.list_period_ask_approvals("alice", "2026-01-01") == []
    with account_authority_scope("alice", old.account_generation):
        with pytest.raises(OmnigentError, match="revoked"):
            store.set_period_ask_approved("alice", "month", "2026-10-01", 50)
