"""Make the per-window auto-reply limit configurable and disabled by default.

Revision ID: 20260904_0020
Revises: 20260904_0019
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260904_0020"
down_revision: str | None = "20260904_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "connections",
        sa.Column("max_auto_replies_per_window", sa.SmallInteger(), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_connections_max_auto_replies_per_window_range"),
        "connections",
        "max_auto_replies_per_window IS NULL OR "
        "max_auto_replies_per_window BETWEEN 1 AND 100",
    )
    op.add_column(
        "contact_activity",
        sa.Column(
            "quiet_window_reply_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.execute(
        "UPDATE contact_activity SET quiet_window_reply_count = 1 "
        "WHERE quiet_window_key IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("contact_activity", "quiet_window_reply_count")
    op.drop_constraint(
        op.f("ck_connections_max_auto_replies_per_window_range"),
        "connections",
        type_="check",
    )
    op.drop_column("connections", "max_auto_replies_per_window")
