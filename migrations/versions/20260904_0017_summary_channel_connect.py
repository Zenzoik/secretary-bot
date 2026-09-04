"""Add self-service summary channel connection state.

Revision ID: 20260904_0017
Revises: 20260904_0016
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260904_0017"
down_revision: str | None = "20260904_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("connections", sa.Column("summary_channel_title", sa.Text(), nullable=True))
    op.create_table(
        "summary_channel_requests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("connection_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name=op.f("fk_summary_channel_requests_connection_id_connections"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'connected', 'error')",
            name=op.f("ck_summary_channel_requests_status_values"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_summary_channel_requests")),
    )
    op.create_index(
        op.f("ix_summary_channel_requests_connection_id"),
        "summary_channel_requests",
        ["connection_id"],
    )
    op.create_index(
        "ix_summary_channel_requests_owner_pending",
        "summary_channel_requests",
        ["owner_user_id", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_summary_channel_requests_owner_pending",
        table_name="summary_channel_requests",
    )
    op.drop_index(
        op.f("ix_summary_channel_requests_connection_id"),
        table_name="summary_channel_requests",
    )
    op.drop_table("summary_channel_requests")
    op.drop_column("connections", "summary_channel_title")
