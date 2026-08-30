"""Add sender identity, delay bounds and read preference.

Revision ID: 20260830_0009
Revises: 20260828_0008
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260830_0009"
down_revision: str | None = "20260828_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "connections",
        sa.Column("sender_identity", sa.Text(), server_default=sa.text("'bot'"), nullable=False),
    )
    op.add_column(
        "connections",
        sa.Column(
            "delay_min_seconds", sa.SmallInteger(), server_default=sa.text("10"), nullable=False
        ),
    )
    op.add_column(
        "connections",
        sa.Column(
            "delay_max_seconds", sa.SmallInteger(), server_default=sa.text("60"), nullable=False
        ),
    )
    op.add_column(
        "connections",
        sa.Column(
            "bot_delay_seconds", sa.SmallInteger(), server_default=sa.text("5"), nullable=False
        ),
    )
    op.add_column(
        "connections",
        sa.Column("mark_read", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.create_check_constraint(
        op.f("ck_connections_sender_identity_values"),
        "connections",
        "sender_identity IN ('bot', 'owner')",
    )
    op.create_check_constraint(
        op.f("ck_connections_delay_min_seconds_range"),
        "connections",
        "delay_min_seconds BETWEEN 0 AND 3600",
    )
    op.create_check_constraint(
        op.f("ck_connections_delay_max_seconds_range"),
        "connections",
        "delay_max_seconds BETWEEN delay_min_seconds AND 3600",
    )
    op.create_check_constraint(
        op.f("ck_connections_bot_delay_seconds_range"),
        "connections",
        "bot_delay_seconds BETWEEN 1 AND 60",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_connections_bot_delay_seconds_range"), "connections", type_="check"
    )
    op.drop_constraint(
        op.f("ck_connections_delay_max_seconds_range"), "connections", type_="check"
    )
    op.drop_constraint(
        op.f("ck_connections_delay_min_seconds_range"), "connections", type_="check"
    )
    op.drop_constraint(op.f("ck_connections_sender_identity_values"), "connections", type_="check")
    op.drop_column("connections", "mark_read")
    op.drop_column("connections", "bot_delay_seconds")
    op.drop_column("connections", "delay_max_seconds")
    op.drop_column("connections", "delay_min_seconds")
    op.drop_column("connections", "sender_identity")
