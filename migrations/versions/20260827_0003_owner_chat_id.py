"""Store the owner's private chat with the bot.

Revision ID: 20260827_0003
Revises: 20260827_0002
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260827_0003"
down_revision: str | None = "20260827_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("connections", sa.Column("owner_chat_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("connections", "owner_chat_id")
