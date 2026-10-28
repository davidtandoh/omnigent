"""Join upstream v0.20.0 and the fork's period-approval history.

Both parent revisions remain unchanged. This revision reconciles only Alembic
version rows and does not change application schema or data.
"""

from __future__ import annotations

revision: str = "e020a3b4c5d6"
down_revision: tuple[str, ...] = ("b2e4f6a8c013", "mm2a3b4c5d6e")
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Join the independently applied parent revisions."""


def downgrade() -> None:
    """Restore both parent markers without changing either schema."""
