"""Allow owner-defined classification directions and reply preferences."""

import sqlalchemy as sa
from alembic import op

revision = "20260917_0025"
down_revision = "20260905_0024"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint(
        op.f("ck_classification_directions_code_values"), "classification_directions", type_="check"
    )
    op.drop_constraint(op.f("ck_message_log_category_values"), "message_log", type_="check")
    op.add_column(
        "classification_directions",
        sa.Column("reply_template", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "classification_directions",
        sa.Column("priority", sa.Text(), nullable=False, server_default="normal"),
    )
    op.create_check_constraint(
        "code_values", "classification_directions", "length(code) BETWEEN 1 AND 40"
    )
    op.create_check_constraint(
        "category_values", "message_log", "category IS NULL OR length(category) BETWEEN 1 AND 40"
    )
    op.create_check_constraint(
        "priority_values", "classification_directions", "priority IN ('normal', 'high')"
    )
    op.execute("UPDATE classification_directions SET priority = 'high' WHERE code = 'money'")


def downgrade():
    # Refuse a lossy rollback while custom categories are in use.
    op.drop_constraint(
        op.f("ck_classification_directions_code_values"), "classification_directions", type_="check"
    )
    op.drop_constraint(op.f("ck_message_log_category_values"), "message_log", type_="check")
    op.drop_constraint(
        op.f("ck_classification_directions_priority_values"),
        "classification_directions",
        type_="check",
    )
    op.create_check_constraint(
        "code_values", "classification_directions", "code IN ('general', 'money')"
    )
    op.create_check_constraint(
        "category_values", "message_log", "category IS NULL OR category IN ('money', 'general')"
    )
    op.drop_column("classification_directions", "priority")
    op.drop_column("classification_directions", "reply_template")
