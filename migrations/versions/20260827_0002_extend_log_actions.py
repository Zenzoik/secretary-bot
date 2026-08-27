"""Extend message_log.action with the remaining gate outcomes.

Revision ID: 20260827_0002
Revises: 20260827_0001
Create Date: 2026-08-27
"""

from collections.abc import Sequence

from alembic import op

from secretary_bot.actions import ACTION_SQL_LIST

revision: str = "20260827_0002"
down_revision: str | None = "20260827_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# op.f() marks the name as final: without it Alembic applies the metadata
# naming convention again and looks for ck_message_log_ck_message_log_…
CONSTRAINT = "ck_message_log_action_values"
PREVIOUS_ACTIONS = (
    "'replied', 'dry_run', 'skipped_schedule', 'skipped_excluded', "
    "'skipped_owner_replied', 'skipped_window_limit', 'error'"
)


def upgrade() -> None:
    op.drop_constraint(op.f(CONSTRAINT), "message_log", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "message_log", f"action IN ({ACTION_SQL_LIST})")


def downgrade() -> None:
    op.execute(f"DELETE FROM message_log WHERE action NOT IN ({PREVIOUS_ACTIONS})")
    op.drop_constraint(op.f(CONSTRAINT), "message_log", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "message_log", f"action IN ({PREVIOUS_ACTIONS})")
