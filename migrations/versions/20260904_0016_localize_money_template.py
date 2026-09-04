"""Localize the legacy money reply template to Ukrainian.

Revision ID: 20260904_0016
Revises: 20260902_0015
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260904_0016"
down_revision: str | None = "20260902_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_TEXT = "Сейчас нерабочее время. Вопрос по оплате увидел, отвечу первым делом утром."
UKRAINIAN_TEXT = (
    "Зараз неробочий час. Питання щодо оплати побачив, відповім насамперед уранці."
)


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE templates SET text = :new_text "
            "WHERE code = 'money_priority' AND text = :old_text"
        ).bindparams(new_text=UKRAINIAN_TEXT, old_text=LEGACY_TEXT)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE templates SET text = :old_text "
            "WHERE code = 'money_priority' AND text = :new_text"
        ).bindparams(old_text=LEGACY_TEXT, new_text=UKRAINIAN_TEXT)
    )
