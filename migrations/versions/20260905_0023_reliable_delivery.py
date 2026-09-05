"""Durable delivery metadata, scheduling outbox and narrowly scoped PDF tokens."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260905_0023"
down_revision: str | None = "20260905_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "delivery_receipts",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column(
            "connection_id",
            sa.BigInteger(),
            sa.ForeignKey("connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("error_code", sa.Text()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "reply_jobs",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column(
            "connection_id",
            sa.BigInteger(),
            sa.ForeignKey("connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "notification_jobs",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column(
            "connection_id",
            sa.BigInteger(),
            sa.ForeignKey("connections.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.Text()),
    )
    for table in ("delivery_receipts", "reply_jobs", "notification_jobs"):
        op.create_index(f"ix_{table}_connection_id", table, ["connection_id"])
    for table in ("reply_jobs", "notification_jobs"):
        for column in ("due_at", "completed_at"):
            op.create_index(f"ix_{table}_{column}", table, [column])
    op.create_table(
        "pdf_tokens",
        sa.Column("token_hash", sa.LargeBinary(32), primary_key=True),
        sa.Column(
            "user_id",
            sa.BigInteger(),
            sa.ForeignKey("access_users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("month", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
    )
    op.drop_constraint(
        "uq_contact_requests_connection_id_tg_message_id", "contact_requests", type_="unique"
    )
    op.create_unique_constraint(
        "uq_contact_requests_connection_id_contact_id_tg_message_id",
        "contact_requests",
        ["connection_id", "contact_id", "tg_message_id"],
    )


def downgrade() -> None:
    # Cross-chat message IDs may now coexist; rolling back requires an explicit data decision.
    raise RuntimeError(
        "Forward-only reliability migration; restore a pre-migration backup to roll back"
    )
