"""Store contact usernames for reliable summary links.

Revision ID: 20260904_0018
Revises: 20260904_0017
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260904_0018"
down_revision: str | None = "20260904_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("contact_activity", sa.Column("contact_username", sa.Text(), nullable=True))
    op.add_column("summary_items", sa.Column("contact_username", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("summary_items", "contact_username")
    op.drop_column("contact_activity", "contact_username")
