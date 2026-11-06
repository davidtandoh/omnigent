"""Join upstream v0.21.0 and the fork's migration history.

Both parent revisions remain unchanged. This revision reconciles only Alembic
version rows and does not change application schema or data.
"""

from __future__ import annotations

revision: str = "f021a4b5c6d7"
down_revision: tuple[str, ...] = ("d0a1f879a01d", "e020a3b4c5d6")
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Join the independently applied parent revisions."""


def downgrade() -> None:
    """Restore both parent markers without changing either schema."""
