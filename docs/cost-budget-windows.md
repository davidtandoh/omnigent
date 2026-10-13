# Cost budget windows

`Policy builder → shared daily cost records → each policy's UTC window`

Each policy selects its own dates from the shared records before evaluating spend.
Weeks start on Monday. Months, quarters, and years use calendar boundaries.
Records after the current UTC day do not count.

The builder loads records from the earliest configured window. This can be a
week that starts in the previous month. Daily and period policies can coexist.
The model downgrade and harness-specific messages retain their existing behavior.

`Approved checkpoint → policy engine → conversation store → approval table`

The engine writes approvals only through the existing state-update interface.
The persistence identity determines which other sessions can reuse an approval.

| Policy | Approval identity | Storage |
| --- | --- | --- |
| Daily, including period `day` | Workspace, user, UTC day | `user_daily_cost.ask_approved_usd` |
| Week, month, quarter, year | Workspace, user, period, window start | `user_period_approvals` |

Non-day state updates carry the period and window start that produced the ASK.
Accepting an older request cannot approve a new window. A lower checkpoint
cannot replace a higher non-day approval. Approvals remain shared across
harnesses, as do the owner's cost records.

Alembic revision `mm2a3b4c5d6e` adds the non-day approval table. Existing daily
approvals remain unchanged and are never interpreted as non-day approvals.
Account generation checks guard writes. Account deletion removes approvals.

The builder seeds costs and approvals at engine construction. An approval
refreshes the approving engine's snapshot; other sessions see it when rebuilt.

Focused verification:

```sh
uv run --no-sync pytest tests/runtime/policies/test_cost_windows.py \
  tests/stores/test_period_approvals.py tests/db/test_migration_user_period_approvals.py
```

These tests use isolated temporary databases and cover shared windows,
cross-session approval isolation, UTC boundaries, and migration preservation.
