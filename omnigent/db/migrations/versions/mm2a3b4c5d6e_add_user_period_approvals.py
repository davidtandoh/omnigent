"""Add approval checkpoints keyed by user, period, and UTC window."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "mm2a3b4c5d6e"
down_revision: str | None = "mm1a2b3c4d5e"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the independent non-day approval table."""
    op.create_table(
        "user_period_approvals",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("period", sa.String(8), nullable=False),
        sa.Column("window_start", sa.String(10), nullable=False),
        sa.Column("ask_approved_usd", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "user_id", "period", "window_start"),
    )


def downgrade() -> None:
    """Drop non-day approvals without changing daily records."""
    op.drop_table("user_period_approvals")
