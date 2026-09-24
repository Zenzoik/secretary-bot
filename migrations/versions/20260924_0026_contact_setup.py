"""Keep the bot silent towards contacts the owner has not reviewed yet.

Revision ID: 20260924_0026
Revises: 20260917_0025
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from secretary_bot.actions import ACTION_SQL_LIST

revision: str = "20260924_0026"
down_revision: str | None = "20260917_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_message_log_action_values"
PREVIOUS_ACTIONS = (
    "'replied', 'dry_run', 'skipped_unsupported_content', 'skipped_inactive', "
    "'skipped_kill_switch', 'skipped_excluded', 'skipped_schedule', "
    "'skipped_window_limit', 'skipped_owner_replied', 'error', 'captured'"
)


def upgrade() -> None:
    op.add_column("contact_activity", sa.Column("configured_at", sa.DateTime(timezone=True)))
    op.add_column("contact_activity", sa.Column("setup_alert_sent_at", sa.DateTime(timezone=True)))
    # Contacts that already exist keep being answered exactly as before.
    op.execute("UPDATE contact_activity SET configured_at = now()")
    op.drop_constraint(op.f(CONSTRAINT), "message_log", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "message_log", f"action IN ({ACTION_SQL_LIST})")


def downgrade() -> None:
    op.execute("DELETE FROM message_log WHERE action = 'skipped_unconfigured'")
    op.drop_constraint(op.f(CONSTRAINT), "message_log", type_="check")
    op.create_check_constraint(op.f(CONSTRAINT), "message_log", f"action IN ({PREVIOUS_ACTIONS})")
    op.drop_column("contact_activity", "setup_alert_sent_at")
    op.drop_column("contact_activity", "configured_at")
