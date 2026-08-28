"""Persist the owner's reply-keyboard control state.

Revision ID: 20260828_0007
Revises: 20260828_0006
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260828_0007"
down_revision: str | None = "20260828_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "connections",
        sa.Column(
            "control_state",
            sa.Text(),
            server_default=sa.text("'main'"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_connections_control_state_values"),
        "connections",
        "control_state IN ('main', 'mute_hours', 'live_confirm')",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_connections_control_state_values"), "connections", type_="check")
    op.drop_column("connections", "control_state")
