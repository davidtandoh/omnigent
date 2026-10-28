from __future__ import annotations

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.utils import _build_alembic_config, get_or_create_engine


def test_period_approval_migration_preserves_daily_state(tmp_path):
    uri = f"sqlite:///{tmp_path / 'period-approvals.db'}"
    engine = get_or_create_engine(uri)
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "mm1a2b3c4d5e")
        connection.execute(
            sa.text(
                "INSERT INTO user_daily_cost "
                "(workspace_id, user_id, day_utc, cost_usd, ask_approved_usd, updated_at) "
                "VALUES (0, 'alice', '2026-10-13', 20, 5, 1)"
            )
        )
        command.upgrade(config, "head")
        assert connection.execute(
            sa.text("SELECT cost_usd, ask_approved_usd FROM user_daily_cost")
        ).one() == (20, 5)
        assert (
            connection.execute(sa.text("SELECT COUNT(*) FROM user_period_approvals")).scalar_one()
            == 0
        )
        connection.execute(
            sa.text(
                "INSERT INTO user_period_approvals "
                "(workspace_id, user_id, period, window_start, ask_approved_usd, updated_at) "
                "VALUES (0, 'alice', 'month', '2026-10-01', 50, 1), "
                "(0, 'alice', 'quarter', '2026-10-01', 100, 1)"
            )
        )
        assert (
            connection.execute(sa.text("SELECT COUNT(*) FROM user_period_approvals")).scalar_one()
            == 2
        )
        command.downgrade(config, "mm1a2b3c4d5e")
        assert connection.execute(
            sa.text("SELECT cost_usd, ask_approved_usd FROM user_daily_cost")
        ).one() == (20, 5)
        command.upgrade(config, "head")
        assert (
            connection.execute(sa.text("SELECT COUNT(*) FROM user_period_approvals")).scalar_one()
            == 0
        )


@pytest.mark.parametrize("starting_revision", ["mm2a3b4c5d6e", "b2e4f6a8c013"])
def test_release_merge_upgrades_each_history_without_losing_approvals(tmp_path, starting_revision):
    uri = f"sqlite:///{tmp_path / 'release-merge.db'}"
    engine = sa.create_engine(uri)
    config = _build_alembic_config(uri)
    try:
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, starting_revision)
            connection.execute(
                sa.text(
                    "INSERT INTO user_daily_cost "
                    "(workspace_id, user_id, day_utc, cost_usd, ask_approved_usd, updated_at) "
                    "VALUES (0, 'alice', '2026-10-13', 20, 5, 1)"
                )
            )
            if starting_revision == "mm2a3b4c5d6e":
                connection.execute(
                    sa.text(
                        "INSERT INTO user_period_approvals "
                        "(workspace_id, user_id, period, window_start, "
                        "ask_approved_usd, updated_at) "
                        "VALUES (0, 'alice', 'month', '2026-10-01', 50, 1)"
                    )
                )
            command.upgrade(config, "head")
            assert connection.execute(
                sa.text("SELECT cost_usd, ask_approved_usd FROM user_daily_cost")
            ).one() == (20, 5)
            assert "pull_requests" in {
                column["name"]
                for column in sa.inspect(connection).get_columns("omnigent_conversation_metadata")
            }
            approvals = (
                connection.execute(sa.text("SELECT ask_approved_usd FROM user_period_approvals"))
                .scalars()
                .all()
            )
            assert approvals == ([50] if starting_revision == "mm2a3b4c5d6e" else [])
            assert len(connection.execute(sa.text("SELECT * FROM alembic_version")).all()) == 1
    finally:
        engine.dispose()
