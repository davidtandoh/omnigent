from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnigent.policies.builtins.cost import user_daily_cost_budget, user_period_cost_budget
from omnigent.policies.cost_windows import cost_window
from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies.builder import build_policy_engine
from omnigent.spec.types import AgentSpec, FunctionPolicySpec, FunctionRef, Phase, PolicyAction
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


@pytest.fixture
def clock(monkeypatch):
    current = [datetime(2026, 10, 13, 12, tzinfo=UTC)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0].astimezone(tz)

    monkeypatch.setattr("omnigent.policies.cost_windows.datetime", Clock)
    monkeypatch.setattr("omnigent.db.utils.now_epoch", lambda: int(current[0].timestamp()))
    return current


def _policy(period, threshold=None, cap=1000.0):
    arguments = {"max_cost_usd": cap}
    if threshold is not None:
        arguments["ask_thresholds_usd"] = [threshold]
    if period == "daily":
        path = "omnigent.policies.builtins.cost.user_daily_cost_budget"
    else:
        path = "omnigent.policies.builtins.cost.user_period_cost_budget"
        arguments["period"] = period
    return FunctionPolicySpec(
        name=period, on=None, function=FunctionRef(path=path, arguments=arguments)
    )


def _build(store, db_uri, policies, owner="alice"):
    conv = store.create_conversation()
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user(owner)
    perms.grant(owner, conv.id, 4)
    return build_policy_engine(
        spec=AgentSpec(spec_version=1, name="budget"),
        conversation_id=conv.id,
        conversation_store=store,
        default_policies=policies,
    )


@pytest.mark.asyncio
async def test_daily_cap_selects_today_from_month_seed(conversation_store, db_uri, clock):
    conversation_store.add_daily_cost("alice", "2026-10-01", 1.0)
    conversation_store.add_daily_cost("alice", "2026-10-13", 20.0)
    engine = _build(conversation_store, db_uri, [_policy("month"), _policy("daily", cap=10.0)])
    result = await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="run"))
    assert result.action == PolicyAction.DENY
    assert "$20.00" in result.reason


@pytest.mark.parametrize("period", ["daily", "day", "week", "month", "quarter", "year"])
def test_each_policy_selects_its_window_from_shared_seed(period, clock):
    rows = [
        {"day_utc": "2025-12-31", "cost_usd": 1000.0, "ask_approved_usd": 999.0},
        {"day_utc": "2026-01-01", "cost_usd": 100.0},
        {"day_utc": "2026-09-30", "cost_usd": 10.0},
        {"day_utc": "2026-10-01", "cost_usd": 4.0, "ask_approved_usd": 999.0},
        {"day_utc": "2026-10-12", "cost_usd": 2.0},
        {"day_utc": "2026-10-13", "cost_usd": 1.0},
        {"day_utc": "2026-10-14", "cost_usd": 1000.0, "ask_approved_usd": 999.0},
    ]
    expected = {"daily": 1, "day": 1, "week": 3, "month": 7, "quarter": 7, "year": 117}[period]
    event = {"type": "request", "context": {"user_daily_cost": rows}}
    factory = user_daily_cost_budget if period == "daily" else user_period_cost_budget
    kwargs = {} if period == "daily" else {"period": period}
    assert factory(**kwargs, max_cost_usd=expected)(event)["result"] == "DENY"
    assert factory(**kwargs, max_cost_usd=expected + 1)(event)["result"] == "ALLOW"
    assert (
        factory(**kwargs, max_cost_usd=10000, ask_thresholds_usd=[expected])(event)["result"]
        == "ASK"
    )


@pytest.mark.parametrize(
    "timestamp,period,start,today",
    [
        ("2026-11-01T00:30:00+02:00", "month", "2026-10-01", "2026-10-31"),
        ("2026-11-01T00:00:00+00:00", "month", "2026-11-01", "2026-11-01"),
        ("2026-10-12T00:00:00+00:00", "week", "2026-10-12", "2026-10-12"),
        ("2027-01-01T00:00:00+00:00", "quarter", "2027-01-01", "2027-01-01"),
        ("2027-01-01T00:00:00+00:00", "year", "2027-01-01", "2027-01-01"),
        ("2026-10-13T23:30:00-02:00", "day", "2026-10-14", "2026-10-14"),
    ],
)
def test_utc_boundaries(clock, timestamp, period, start, today):
    clock[0] = datetime.fromisoformat(timestamp)
    assert cost_window(period) == (start, today)


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [("month", "daily"), ("daily", "month")])
async def test_approvals_are_independent_across_sessions(conversation_store, db_uri, clock, order):
    conversation_store.add_daily_cost("alice", "2026-10-01", 50.0)
    conversation_store.add_daily_cost("alice", "2026-10-13", 10.0)
    thresholds = {"month": 50.0, "daily": 5.0}
    ctx = EvaluationContext(phase=Phase.REQUEST, content="run")
    for period in order:
        policies = [_policy(period, thresholds[period])]
        if period == "daily":
            policies.insert(0, _policy("month"))
        engine = _build(conversation_store, db_uri, policies)
        ask = await engine.evaluate(ctx)
        assert ask.action == PolicyAction.ASK
        engine.apply_state_updates(ask.state_updates, approved=True)
        assert (await engine.evaluate(ctx)).action == PolicyAction.ALLOW
        another = _build(conversation_store, db_uri, policies)
        assert (await another.evaluate(ctx)).action == PolicyAction.ALLOW
    assert conversation_store.get_daily_cost_state("alice", "2026-10-01")["ask_approved_usd"] == 0
    assert conversation_store.get_daily_cost_state("alice", "2026-10-13")["ask_approved_usd"] == 5
    assert conversation_store.list_period_ask_approvals("alice", "2026-01-01") == [
        {"period": "month", "window_start": "2026-10-01", "ask_approved_usd": 50.0}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("other_period", ["week", "quarter", "year"])
async def test_month_approval_does_not_approve_other_periods(
    conversation_store, db_uri, clock, other_period
):
    conversation_store.add_daily_cost("alice", "2026-10-13", 60.0)
    ctx = EvaluationContext(phase=Phase.REQUEST, content="run")
    monthly = _build(conversation_store, db_uri, [_policy("month", 50.0)])
    ask = await monthly.evaluate(ctx)
    monthly.apply_state_updates(ask.state_updates, approved=True)
    other = _build(conversation_store, db_uri, [_policy(other_period, 5.0)])
    assert (await other.evaluate(ctx)).action == PolicyAction.ASK


@pytest.mark.asyncio
async def test_simultaneous_windows_keep_separate_approvals(conversation_store, db_uri, clock):
    conversation_store.add_daily_cost("alice", "2026-10-13", 60.0)
    periods = ["daily", "day", "week", "month", "quarter", "year"]
    policies = [_policy(period, 5.0) for period in periods]
    engine = _build(conversation_store, db_uri, policies)
    ctx = EvaluationContext(phase=Phase.TOOL_CALL, content={"name": "shell"}, tool_name="shell")
    ask = await engine.evaluate(ctx)
    assert ask.action == PolicyAction.ASK
    assert len(ask.state_updates) == 6
    engine.apply_state_updates(ask.state_updates, approved=True)
    assert (await engine.evaluate(ctx)).action == PolicyAction.ALLOW
    rebuilt = _build(conversation_store, db_uri, policies)
    assert (await rebuilt.evaluate(ctx)).action == PolicyAction.ALLOW
    approvals = conversation_store.list_period_ask_approvals("alice", "2026-01-01")
    assert {r["period"] for r in approvals} == {"week", "month", "quarter", "year"}


@pytest.mark.asyncio
async def test_delayed_approval_stays_in_original_window(conversation_store, db_uri, clock):
    conversation_store.add_daily_cost("alice", "2026-10-13", 60.0)
    policies = [_policy("month", 50.0)]
    engine = _build(conversation_store, db_uri, policies)
    ctx = EvaluationContext(phase=Phase.REQUEST, content="run")
    ask = await engine.evaluate(ctx)
    assert ask.action == PolicyAction.ASK
    clock[0] = datetime(2026, 11, 1, tzinfo=UTC)
    engine.apply_state_updates(ask.state_updates, approved=True)
    conversation_store.add_daily_cost("alice", "2026-11-01", 60.0)
    next_month = _build(conversation_store, db_uri, policies)
    assert (await next_month.evaluate(ctx)).action == PolicyAction.ASK
    assert conversation_store.list_period_ask_approvals("alice", "2026-11-01") == []


@pytest.mark.asyncio
async def test_new_month_does_not_truncate_overlapping_week(conversation_store, db_uri, clock):
    clock[0] = datetime(2026, 11, 1, tzinfo=UTC)
    conversation_store.add_daily_cost("alice", "2026-10-26", 60.0)
    conversation_store.add_daily_cost("alice", "2026-11-01", 1.0)
    engine = _build(
        conversation_store, db_uri, [_policy("month", cap=10), _policy("week", cap=50)]
    )
    result = await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="run"))
    assert result.action == PolicyAction.DENY
    assert "weekly" in result.reason
    assert "$61.00" in result.reason
