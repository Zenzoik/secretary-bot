"""Add gated users and one-time access invites.

Revision ID: 20260828_0008
Revises: 20260828_0007
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260828_0008"
down_revision: str | None = "20260828_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "access_users",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), server_default=sa.text("'user'"), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column(
            "onboarding_state",
            sa.Text(),
            server_default=sa.text("'awaiting_connection'"),
            nullable=False,
        ),
        sa.Column("invited_by", sa.BigInteger(), nullable=True),
        sa.Column("approved_by", sa.BigInteger(), nullable=True),
        sa.Column("revoked_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "status_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "onboarding_state IN ('awaiting_connection', 'timezone', 'schedule', 'scope', 'ready')",
            name=op.f("ck_access_users_onboarding_state_values"),
        ),
        sa.CheckConstraint("role IN ('master', 'user')", name=op.f("ck_access_users_role_values")),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'revoked')",
            name=op.f("ck_access_users_status_values"),
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_access_users")),
    )
    op.create_index("ix_access_users_status_role", "access_users", ["status", "role"], unique=False)
    op.create_table(
        "access_invites",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("token_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["access_users.user_id"],
            name=op.f("fk_access_invites_created_by_access_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_access_invites")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_access_invites_token_hash")),
    )
    op.create_index(
        op.f("ix_access_invites_created_by"), "access_invites", ["created_by"], unique=False
    )
    op.create_index(
        op.f("ix_access_invites_expires_at"), "access_invites", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_access_invites_expires_at"), table_name="access_invites")
    op.drop_index(op.f("ix_access_invites_created_by"), table_name="access_invites")
    op.drop_table("access_invites")
    op.drop_index("ix_access_users_status_role", table_name="access_users")
    op.drop_table("access_users")
