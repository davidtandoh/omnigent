"""Fork-only Alembic revisions and a version-row reader that ignores them.

The fork carries its own migration branch (user period approvals) and one
merge revision per upstream sync. Pinning an upstream branch downgrades only
that branch, so these revisions stay in ``alembic_version`` beside it.
"""

from __future__ import annotations

import sqlalchemy as sa

FORK_ONLY_REVISIONS = frozenset(
    {
        "mm2a3b4c5d6e",  # add user_period_approvals
        "e020a3b4c5d6",  # merge upstream and period approvals
        "f021a4b5c6d7",  # merge upstream v0.21.0
        "f023a5b6c7d8",  # merge upstream v0.23.0
    }
)


def upstream_version_rows(connection: sa.Connection) -> list[str]:
    """Return ``alembic_version`` rows, leaving out fork-only revisions."""
    rows = connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalars()
    return [row for row in rows if row not in FORK_ONLY_REVISIONS]
