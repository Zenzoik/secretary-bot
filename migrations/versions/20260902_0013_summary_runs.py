"""Add durable daily summary runs and items.

Revision ID: 20260902_0013
Revises: 20260902_0012
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260902_0013"
down_revision: str | None = "20260902_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "summary_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("connection_id", sa.BigInteger(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("destination_chat_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'error')",
            name=op.f("ck_summary_runs_status_values"),
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name=op.f("fk_summary_runs_connection_id_connections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_summary_runs")),
        sa.UniqueConstraint(
            "connection_id",
            "period_start",
            "period_end",
            name=op.f("uq_summary_runs_connection_id_period_start_period_end"),
        ),
    )
    op.create_index(
        op.f("ix_summary_runs_connection_id"), "summary_runs", ["connection_id"]
    )
    op.create_index(
        "ix_summary_runs_connection_period", "summary_runs", ["connection_id", "period_end"]
    )
    op.create_table(
        "summary_items",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("contact_id", sa.BigInteger(), nullable=False),
        sa.Column("contact_name", sa.Text(), nullable=True),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("agreements_json", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column(
            "open_questions_json", sa.JSON(), server_default=sa.text("'[]'"), nullable=False
        ),
        sa.Column("questions_asked", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("questions_closed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_incoming_message_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["summary_runs.id"],
            name=op.f("fk_summary_items_run_id_summary_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_summary_items")),
        sa.UniqueConstraint(
            "run_id", "contact_id", name=op.f("uq_summary_items_run_id_contact_id")
        ),
    )
    op.create_index(op.f("ix_summary_items_run_id"), "summary_items", ["run_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_summary_items_run_id"), table_name="summary_items")
    op.drop_table("summary_items")
    op.drop_index("ix_summary_runs_connection_period", table_name="summary_runs")
    op.drop_index(op.f("ix_summary_runs_connection_id"), table_name="summary_runs")
    op.drop_table("summary_runs")
