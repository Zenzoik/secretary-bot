"""Add paid escalation settings, counters, and request journal.

Revision ID: 20260904_0021
Revises: 20260904_0020
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260904_0021"
down_revision: str | None = "20260904_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "connections",
        sa.Column(
            "escalation_enabled",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "connections",
        sa.Column(
            "escalation_price_amount",
            sa.Numeric(12, 2),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "connections",
        sa.Column(
            "escalation_currency",
            sa.Text(),
            server_default=sa.text("'UAH'"),
            nullable=False,
        ),
    )
    op.add_column(
        "connections",
        sa.Column(
            "escalation_offer_text",
            sa.Text(),
            server_default=sa.text(
                "'Якщо відповідь потрібна терміново, можна створити платне звернення.'"
            ),
            nullable=False,
        ),
    )
    op.add_column(
        "connections",
        sa.Column(
            "escalation_confirm_text",
            sa.Text(),
            server_default=sa.text(
                "'Платне звернення підтверджено. Зв’язок не гарантовано, але звернення "
                "буде враховано в рахунку наприкінці місяця.'"
            ),
            nullable=False,
        ),
    )
    op.add_column(
        "connections",
        sa.Column(
            "escalation_decline_text",
            sa.Text(),
            server_default=sa.text(
                "'На жаль, зараз немає можливості відповісти терміново.'"
            ),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        op.f("ck_connections_escalation_price_amount_nonnegative"),
        "connections",
        "escalation_price_amount >= 0",
    )
    for column in ("off_hours_request_count", "paid_escalation_count"):
        op.add_column(
            "contact_activity",
            sa.Column(column, sa.Integer(), server_default=sa.text("0"), nullable=False),
        )
    op.create_table(
        "contact_requests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("connection_id", sa.BigInteger(), nullable=False),
        sa.Column("contact_id", sa.BigInteger(), nullable=False),
        sa.Column("tg_message_id", sa.BigInteger(), nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("window_key", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'normal'"), nullable=False),
        sa.Column("price_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.Text(), nullable=True),
        sa.Column("offer_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owner_decision", sa.Text(), nullable=True),
        sa.Column("owner_decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bot_reply_message_id", sa.BigInteger(), nullable=True),
        sa.Column("owner_notification_message_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "owner_decision IS NULL OR owner_decision IN ('pending', 'declined')",
            name=op.f("ck_contact_requests_owner_decision_values"),
        ),
        sa.CheckConstraint(
            "status IN ('normal', 'offered', 'paid')",
            name=op.f("ck_contact_requests_status_values"),
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name=op.f("fk_contact_requests_connection_id_connections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contact_requests")),
        sa.UniqueConstraint(
            "connection_id",
            "tg_message_id",
            name=op.f("uq_contact_requests_connection_id_tg_message_id"),
        ),
    )
    op.create_index(
        op.f("ix_contact_requests_connection_id"),
        "contact_requests",
        ["connection_id"],
    )
    op.create_index(
        op.f("ix_contact_requests_occurred_at"),
        "contact_requests",
        ["occurred_at"],
    )
    op.create_index(
        op.f("ix_contact_requests_offer_expires_at"),
        "contact_requests",
        ["offer_expires_at"],
    )
    op.create_index(
        "ix_contact_requests_connection_contact_occurred",
        "contact_requests",
        ["connection_id", "contact_id", sa.text("occurred_at DESC")],
    )
    for column in ("normal_request_count", "paid_request_count"):
        op.add_column(
            "summary_items",
            sa.Column(column, sa.Integer(), server_default=sa.text("0"), nullable=False),
        )


def downgrade() -> None:
    op.drop_column("summary_items", "paid_request_count")
    op.drop_column("summary_items", "normal_request_count")
    op.drop_index(
        "ix_contact_requests_connection_contact_occurred",
        table_name="contact_requests",
    )
    op.drop_index(op.f("ix_contact_requests_offer_expires_at"), table_name="contact_requests")
    op.drop_index(op.f("ix_contact_requests_occurred_at"), table_name="contact_requests")
    op.drop_index(op.f("ix_contact_requests_connection_id"), table_name="contact_requests")
    op.drop_table("contact_requests")
    op.drop_column("contact_activity", "paid_escalation_count")
    op.drop_column("contact_activity", "off_hours_request_count")
    op.drop_constraint(
        op.f("ck_connections_escalation_price_amount_nonnegative"),
        "connections",
        type_="check",
    )
    for column in (
        "escalation_decline_text",
        "escalation_confirm_text",
        "escalation_offer_text",
        "escalation_currency",
        "escalation_price_amount",
        "escalation_enabled",
    ):
        op.drop_column("connections", column)
