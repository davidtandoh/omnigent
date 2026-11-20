"""Join upstream v0.23.0 and the fork's migration history.

Both parent revisions remain unchanged. This revision reconciles only Alembic
version rows and does not change application schema or data.
"""

from __future__ import annotations

revision: str = "f023a5b6c7d8"
down_revision: tuple[str, ...] = ("ffeec66eadb5", "f021a4b5c6d7")
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Join the independently applied parent revisions."""


def downgrade() -> None:
    """Restore both parent markers without changing either schema."""
