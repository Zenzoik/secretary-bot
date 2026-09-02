"""Add persistent FSM state for replies from summary cards.

Revision ID: 20260902_0014
Revises: 20260902_0013
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260902_0014"
down_revision: str | None = "20260902_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "summary_reply_states",
        sa.Column("connection_id", sa.BigInteger(), nullable=False),
        sa.Column("summary_item_id", sa.BigInteger(), nullable=False),
        sa.Column("prompt_message_id", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name=op.f("fk_summary_reply_states_connection_id_connections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["summary_item_id"],
            ["summary_items.id"],
            name=op.f("fk_summary_reply_states_summary_item_id_summary_items"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("connection_id", name=op.f("pk_summary_reply_states")),
        sa.UniqueConstraint(
            "summary_item_id", name=op.f("uq_summary_reply_states_summary_item_id")
        ),
    )
    op.create_index(
        op.f("ix_summary_reply_states_expires_at"),
        "summary_reply_states",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_summary_reply_states_expires_at"), table_name="summary_reply_states"
    )
    op.drop_table("summary_reply_states")
