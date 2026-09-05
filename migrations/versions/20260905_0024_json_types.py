"""Align legacy JSON columns with the JSONB model used in production."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260905_0024"
down_revision: str | None = "20260905_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
COLUMNS = (
    ("classification_directions", "keywords_json"),
    ("summary_items", "agreements_json"),
    ("summary_items", "open_questions_json"),
)


def upgrade() -> None:
    for table, column in COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=sa.JSON(),
            type_=postgresql.JSONB(),
            postgresql_using=f"{column}::jsonb",
        )


def downgrade() -> None:
    for table, column in COLUMNS:
        op.alter_column(
            table,
            column,
            existing_type=postgresql.JSONB(),
            type_=sa.JSON(),
            postgresql_using=f"{column}::json",
        )
