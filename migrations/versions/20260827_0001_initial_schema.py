"""Create the initial multi-tenant schema.

Revision ID: 20260827_0001
Revises:
Create Date: 2026-08-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260827_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "connections",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("business_connection_id", sa.Text(), nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_username", sa.Text(), nullable=True),
        sa.Column(
            "rights_json",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("dry_run", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("kill_switch", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("timezone", sa.Text(), server_default=sa.text("'Europe/Kyiv'"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connections")),
        sa.UniqueConstraint(
            "business_connection_id", name=op.f("uq_connections_business_connection_id")
        ),
    )

    op.create_table(
        "schedules",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("connection_id", sa.BigInteger(), nullable=False),
        sa.Column("weekday_mask", sa.SmallInteger(), nullable=False),
        sa.Column("time_from", sa.Time(), nullable=False),
        sa.Column("time_to", sa.Time(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.CheckConstraint(
            "weekday_mask BETWEEN 1 AND 127", name=op.f("ck_schedules_weekday_mask_range")
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name=op.f("fk_schedules_connection_id_connections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_schedules")),
    )
    op.create_index(op.f("ix_schedules_connection_id"), "schedules", ["connection_id"])

    op.create_table(
        "exclusions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("connection_id", sa.BigInteger(), nullable=False),
        sa.Column("contact_id", sa.BigInteger(), nullable=False),
        sa.Column("contact_name", sa.Text(), nullable=True),
        sa.Column("until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name=op.f("fk_exclusions_connection_id_connections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_exclusions")),
        sa.UniqueConstraint(
            "connection_id", "contact_id", name=op.f("uq_exclusions_connection_id_contact_id")
        ),
    )

    op.create_table(
        "templates",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("connection_id", sa.BigInteger(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name=op.f("fk_templates_connection_id_connections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_templates")),
        sa.UniqueConstraint("connection_id", "code", name=op.f("uq_templates_connection_id_code")),
    )

    op.create_table(
        "overrides",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("connection_id", sa.BigInteger(), nullable=False),
        sa.Column("contact_id", sa.BigInteger(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("template_id", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "mode IN ('always_silent', 'always_reply', 'force_template')",
            name=op.f("ck_overrides_mode_values"),
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name=op.f("fk_overrides_connection_id_connections"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["template_id"],
            ["templates.id"],
            name=op.f("fk_overrides_template_id_templates"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_overrides")),
        sa.UniqueConstraint(
            "connection_id", "contact_id", name=op.f("uq_overrides_connection_id_contact_id")
        ),
    )

    op.create_table(
        "prompts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("connection_id", sa.BigInteger(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column(
            "model", sa.Text(), server_default=sa.text("'claude-sonnet-4-6'"), nullable=False
        ),
        sa.Column(
            "confidence_min",
            sa.Numeric(precision=3, scale=2),
            server_default=sa.text("0.70"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "confidence_min BETWEEN 0.00 AND 1.00",
            name=op.f("ck_prompts_confidence_min_range"),
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name=op.f("fk_prompts_connection_id_connections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_prompts")),
        sa.UniqueConstraint("connection_id", "code", name=op.f("uq_prompts_connection_id_code")),
    )

    op.create_table(
        "contact_activity",
        sa.Column("connection_id", sa.BigInteger(), nullable=False),
        sa.Column("contact_id", sa.BigInteger(), nullable=False),
        sa.Column("last_incoming_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owner_last_reply_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_auto_reply_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("quiet_window_key", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name=op.f("fk_contact_activity_connection_id_connections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("connection_id", "contact_id", name=op.f("pk_contact_activity")),
    )

    op.create_table(
        "message_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("connection_id", sa.BigInteger(), nullable=False),
        sa.Column("contact_id", sa.BigInteger(), nullable=False),
        sa.Column("tg_message_id", sa.BigInteger(), nullable=True),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=3, scale=2), nullable=True),
        sa.Column("template_code", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("body_encrypted", sa.LargeBinary(), nullable=True),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by_user", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.CheckConstraint(
            "action IN ('replied', 'dry_run', 'skipped_schedule', 'skipped_excluded', "
            "'skipped_owner_replied', 'skipped_window_limit', 'error')",
            name=op.f("ck_message_log_action_values"),
        ),
        sa.CheckConstraint(
            "category IS NULL OR category IN ('money', 'general')",
            name=op.f("ck_message_log_category_values"),
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 0.00 AND 1.00",
            name=op.f("ck_message_log_confidence_range"),
        ),
        sa.CheckConstraint(
            "direction IN ('in', 'out')", name=op.f("ck_message_log_direction_values")
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name=op.f("fk_message_log_connection_id_connections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_message_log")),
    )
    op.create_index(
        "ix_message_log_connection_contact_occurred",
        "message_log",
        ["connection_id", "contact_id", sa.literal_column("occurred_at DESC")],
    )
    op.create_index(
        "ix_message_log_connection_occurred",
        "message_log",
        ["connection_id", sa.literal_column("occurred_at DESC")],
    )

    op.create_table(
        "morning_queue",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("connection_id", sa.BigInteger(), nullable=False),
        sa.Column("contact_id", sa.BigInteger(), nullable=False),
        sa.Column("contact_name", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("is_delivered", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_done", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name=op.f("fk_morning_queue_connection_id_connections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_morning_queue")),
    )
    op.create_index(op.f("ix_morning_queue_connection_id"), "morning_queue", ["connection_id"])

    op.create_table(
        "shadow_feedback",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("log_id", sa.BigInteger(), nullable=False),
        sa.Column("verdict", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "verdict IN ('ok', 'wrong', 'exclude')",
            name=op.f("ck_shadow_feedback_verdict_values"),
        ),
        sa.ForeignKeyConstraint(
            ["log_id"],
            ["message_log.id"],
            name=op.f("fk_shadow_feedback_log_id_message_log"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shadow_feedback")),
    )
    op.create_index(op.f("ix_shadow_feedback_log_id"), "shadow_feedback", ["log_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_shadow_feedback_log_id"), table_name="shadow_feedback")
    op.drop_table("shadow_feedback")
    op.drop_index(op.f("ix_morning_queue_connection_id"), table_name="morning_queue")
    op.drop_table("morning_queue")
    op.drop_index("ix_message_log_connection_occurred", table_name="message_log")
    op.drop_index("ix_message_log_connection_contact_occurred", table_name="message_log")
    op.drop_table("message_log")
    op.drop_table("contact_activity")
    op.drop_table("prompts")
    op.drop_table("overrides")
    op.drop_table("templates")
    op.drop_table("exclusions")
    op.drop_index(op.f("ix_schedules_connection_id"), table_name="schedules")
    op.drop_table("schedules")
    op.drop_table("connections")
