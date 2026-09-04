"""Add persistent FSM state for direct bot replies.

Revision ID: 20260904_0019
Revises: 20260904_0018
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260904_0019"
down_revision: str | None = "20260904_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "direct_reply_states",
        sa.Column("connection_id", sa.BigInteger(), nullable=False),
        sa.Column("contact_id", sa.BigInteger(), nullable=False),
        sa.Column("prompt_message_id", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name=op.f("fk_direct_reply_states_connection_id_connections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("connection_id", name=op.f("pk_direct_reply_states")),
    )
    op.create_index(
        op.f("ix_direct_reply_states_expires_at"),
        "direct_reply_states",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_direct_reply_states_expires_at"), table_name="direct_reply_states"
    )
    op.drop_table("direct_reply_states")
