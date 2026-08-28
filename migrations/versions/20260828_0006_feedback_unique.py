"""Make dry-run feedback one row per decision.

Revision ID: 20260828_0006
Revises: 20260828_0005
Create Date: 2026-08-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260828_0006"
down_revision: str | None = "20260828_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM shadow_feedback AS older
        USING shadow_feedback AS newer
        WHERE older.log_id = newer.log_id AND older.id < newer.id
        """
    )
    op.create_unique_constraint(op.f("uq_shadow_feedback_log_id"), "shadow_feedback", ["log_id"])


def downgrade() -> None:
    op.drop_constraint(op.f("uq_shadow_feedback_log_id"), "shadow_feedback", type_="unique")
