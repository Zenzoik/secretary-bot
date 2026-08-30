"""Keep the bot delay minimum within the configured maximum.

Revision ID: 20260830_0010
Revises: 20260830_0009
Create Date: 2026-08-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260830_0010"
down_revision: str | None = "20260830_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        op.f("ck_connections_bot_delay_within_max"),
        "connections",
        "bot_delay_seconds <= delay_max_seconds",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_connections_bot_delay_within_max"), "connections", type_="check"
    )
