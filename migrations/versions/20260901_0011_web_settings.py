"""Add Stage 3 web settings and browser sessions.

Revision ID: 20260901_0011
Revises: 20260830_0010
Create Date: 2026-09-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_0011"
down_revision: str | None = "20260830_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "connections",
        sa.Column("summary_time", sa.Time(), server_default=sa.text("'09:00:00'"), nullable=False),
    )
    op.add_column("connections", sa.Column("summary_channel_id", sa.BigInteger(), nullable=True))
    op.add_column("contact_activity", sa.Column("contact_name", sa.Text(), nullable=True))

    op.create_table(
        "contact_windows",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("connection_id", sa.BigInteger(), nullable=False),
        sa.Column("contact_id", sa.BigInteger(), nullable=False),
        sa.Column("weekday_mask", sa.SmallInteger(), nullable=False),
        sa.Column("time_from", sa.Time(), nullable=False),
        sa.Column("time_to", sa.Time(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.CheckConstraint(
            "weekday_mask BETWEEN 1 AND 127", name=op.f("ck_contact_windows_weekday_mask_range")
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name=op.f("fk_contact_windows_connection_id_connections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contact_windows")),
        sa.UniqueConstraint(
            "connection_id",
            "contact_id",
            "weekday_mask",
            "time_from",
            "time_to",
            name=op.f("uq_contact_windows_connection_id_contact_id_weekday_mask_time_from_time_to"),
        ),
    )
    op.create_index(op.f("ix_contact_windows_connection_id"), "contact_windows", ["connection_id"])

    op.create_table(
        "classification_directions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("connection_id", sa.BigInteger(), nullable=False),
        sa.Column("code", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("keywords_json", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "code IN ('general', 'money')", name=op.f("ck_classification_directions_code_values")
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["connections.id"],
            name=op.f("fk_classification_directions_connection_id_connections"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_classification_directions")),
        sa.UniqueConstraint(
            "connection_id", "code", name=op.f("uq_classification_directions_connection_id_code")
        ),
    )
    op.create_index(
        op.f("ix_classification_directions_connection_id"),
        "classification_directions",
        ["connection_id"],
    )

    op.create_table(
        "web_sessions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "kind IN ('exchange', 'session')", name=op.f("ck_web_sessions_kind_values")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["access_users.user_id"],
            name=op.f("fk_web_sessions_user_id_access_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_web_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_web_sessions_token_hash")),
    )
    op.create_index(op.f("ix_web_sessions_expires_at"), "web_sessions", ["expires_at"])
    op.create_index(op.f("ix_web_sessions_user_id"), "web_sessions", ["user_id"])
    op.create_index("ix_web_sessions_user_expires", "web_sessions", ["user_id", "expires_at"])


def downgrade() -> None:
    op.drop_index("ix_web_sessions_user_expires", table_name="web_sessions")
    op.drop_index(op.f("ix_web_sessions_user_id"), table_name="web_sessions")
    op.drop_index(op.f("ix_web_sessions_expires_at"), table_name="web_sessions")
    op.drop_table("web_sessions")
    op.drop_index(
        op.f("ix_classification_directions_connection_id"), table_name="classification_directions"
    )
    op.drop_table("classification_directions")
    op.drop_index(op.f("ix_contact_windows_connection_id"), table_name="contact_windows")
    op.drop_table("contact_windows")
    op.drop_column("contact_activity", "contact_name")
    op.drop_column("connections", "summary_channel_id")
    op.drop_column("connections", "summary_time")
