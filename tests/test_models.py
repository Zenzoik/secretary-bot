from sqlalchemy import CheckConstraint, ForeignKeyConstraint, create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from secretary_bot.models import Base

EXPECTED_TABLES = {
    "access_invites",
    "access_users",
    "connections",
    "contact_activity",
    "exclusions",
    "message_log",
    "morning_queue",
    "overrides",
    "prompts",
    "schedules",
    "shadow_feedback",
    "templates",
}


def test_metadata_contains_complete_schema() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_schema_can_be_created_in_dependency_order() -> None:
    engine = create_engine("sqlite:///:memory:")

    Base.metadata.create_all(engine)

    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_tenant_tables_cascade_from_connection() -> None:
    direct_tenant_tables = EXPECTED_TABLES - {
        "access_invites",
        "access_users",
        "connections",
        "shadow_feedback",
    }

    for table_name in direct_tenant_tables:
        table = Base.metadata.tables[table_name]
        foreign_keys = [
            constraint
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
            and constraint.referred_table.name == "connections"
        ]
        assert len(foreign_keys) == 1, table_name
        assert foreign_keys[0].ondelete == "CASCADE", table_name


def test_message_log_has_no_plaintext_body_column() -> None:
    columns = Base.metadata.tables["message_log"].columns

    assert "body" not in columns
    assert "message_text" not in columns
    assert columns["body_encrypted"].nullable is True
    assert columns["retention_until"].nullable is True


def test_critical_domain_checks_are_present() -> None:
    expected = {
        "access_users": {
            "ck_access_users_onboarding_state_values",
            "ck_access_users_role_values",
            "ck_access_users_status_values",
        },
        "connections": {
            "ck_connections_bot_delay_within_max",
            "ck_connections_bot_delay_seconds_range",
            "ck_connections_control_state_values",
            "ck_connections_delay_max_seconds_range",
            "ck_connections_delay_min_seconds_range",
            "ck_connections_sender_identity_values",
        },
        "schedules": {"ck_schedules_weekday_mask_range"},
        "overrides": {"ck_overrides_mode_values"},
        "prompts": {"ck_prompts_confidence_min_range"},
        "message_log": {
            "ck_message_log_action_values",
            "ck_message_log_category_values",
            "ck_message_log_confidence_range",
            "ck_message_log_direction_values",
        },
        "shadow_feedback": {"ck_shadow_feedback_verdict_values"},
    }

    for table_name, names in expected.items():
        constraints = {
            constraint.name
            for constraint in Base.metadata.tables[table_name].constraints
            if isinstance(constraint, CheckConstraint)
        }
        assert names <= constraints


def test_all_postgresql_ddl_compiles() -> None:
    dialect = postgresql.dialect()

    for table in Base.metadata.sorted_tables:
        assert str(CreateTable(table).compile(dialect=dialect))
        for index in table.indexes:
            assert str(CreateIndex(index).compile(dialect=dialect))
