"""Add opt-in encrypted message retention.

Revision ID: 20260902_0012
Revises: 20260901_0011
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from secretary_bot.actions import ACTION_SQL_LIST

revision: str = "20260902_0012"
down_revision: str | None = "20260901_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_message_log_action_values"
PREVIOUS_ACTIONS = (
    "'replied', 'dry_run', 'skipped_unsupported_content', 'skipped_inactive', "
    "'skipped_kill_switch', 'skipped_excluded', 'skipped_schedule', "
    "'skipped_window_limit', 'skipped_owner_replied', 'error'"
)


def upgrade() -> None:
    op.add_column(
        "connections",
        sa.Column(
            "message_retention_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.drop_constraint(op.f(CONSTRAINT), "message_log", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "message_log", f"action IN ({ACTION_SQL_LIST})")
    op.create_index(
        op.f("ix_message_log_retention_until"), "message_log", ["retention_until"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_message_log_retention_until"), table_name="message_log")
    op.execute("DELETE FROM message_log WHERE action = 'captured'")
    op.drop_constraint(op.f(CONSTRAINT), "message_log", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "message_log", f"action IN ({PREVIOUS_ACTIONS})")
    op.drop_column("connections", "message_retention_enabled")
