from __future__ import annotations

from datetime import UTC, datetime, time
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    SmallInteger,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON, TypeDecorator

from secretary_bot.actions import ACTION_SQL_LIST

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
JSON_DOCUMENT = JSON().with_variant(JSONB, "postgresql")
# SQLite only autoincrements INTEGER primary keys; Postgres keeps BIGSERIAL.
SURROGATE_KEY = BigInteger().with_variant(Integer, "sqlite")


class UtcDateTime(TypeDecorator[datetime]):
    """TIMESTAMPTZ that always reads back as aware UTC, including on SQLite."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetimes are ambiguous; pass an aware value")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Connection(Base):
    __tablename__ = "connections"

    id: Mapped[int] = mapped_column(SURROGATE_KEY, primary_key=True, autoincrement=True)
    business_connection_id: Mapped[str] = mapped_column(Text, unique=True)
    owner_user_id: Mapped[int] = mapped_column(BigInteger)
    owner_username: Mapped[str | None] = mapped_column(Text)
    owner_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    rights_json: Mapped[dict[str, Any]] = mapped_column(
        JSON_DOCUMENT, server_default=sql_text("'{}'"), default=dict
    )
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("true"))
    dry_run: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("true"))
    kill_switch: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("false"))
    timezone: Mapped[str] = mapped_column(Text, server_default=sql_text("'Europe/Kyiv'"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), server_default=func.now(), onupdate=func.now()
    )


class Schedule(Base):
    __tablename__ = "schedules"
    __table_args__ = (CheckConstraint("weekday_mask BETWEEN 1 AND 127", name="weekday_mask_range"),)

    id: Mapped[int] = mapped_column(SURROGATE_KEY, primary_key=True, autoincrement=True)
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), index=True
    )
    weekday_mask: Mapped[int] = mapped_column(SmallInteger)
    time_from: Mapped[time] = mapped_column(Time)
    time_to: Mapped[time] = mapped_column(Time)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("true"))


class Exclusion(Base):
    __tablename__ = "exclusions"
    __table_args__ = (UniqueConstraint("connection_id", "contact_id"),)

    id: Mapped[int] = mapped_column(SURROGATE_KEY, primary_key=True, autoincrement=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"))
    contact_id: Mapped[int] = mapped_column(BigInteger)
    contact_name: Mapped[str | None] = mapped_column(Text)
    until: Mapped[datetime | None] = mapped_column(UtcDateTime())
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), server_default=func.now())


class Template(Base):
    __tablename__ = "templates"
    __table_args__ = (UniqueConstraint("connection_id", "code"),)

    id: Mapped[int] = mapped_column(SURROGATE_KEY, primary_key=True, autoincrement=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("true"))
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), server_default=func.now(), onupdate=func.now()
    )


class Override(Base):
    __tablename__ = "overrides"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('always_silent', 'always_reply', 'force_template')",
            name="mode_values",
        ),
        UniqueConstraint("connection_id", "contact_id"),
    )

    id: Mapped[int] = mapped_column(SURROGATE_KEY, primary_key=True, autoincrement=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"))
    contact_id: Mapped[int] = mapped_column(BigInteger)
    mode: Mapped[str] = mapped_column(Text)
    template_id: Mapped[int | None] = mapped_column(ForeignKey("templates.id", ondelete="SET NULL"))


class Prompt(Base):
    __tablename__ = "prompts"
    __table_args__ = (
        CheckConstraint("confidence_min BETWEEN 0.00 AND 1.00", name="confidence_min_range"),
        UniqueConstraint("connection_id", "code"),
    )

    id: Mapped[int] = mapped_column(SURROGATE_KEY, primary_key=True, autoincrement=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(Text)
    system_prompt: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text, server_default=sql_text("'claude-sonnet-4-6'"))
    confidence_min: Mapped[Decimal] = mapped_column(Numeric(3, 2), server_default=sql_text("0.70"))
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime(), server_default=func.now(), onupdate=func.now()
    )


class ContactActivity(Base):
    __tablename__ = "contact_activity"

    connection_id: Mapped[int] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), primary_key=True
    )
    contact_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    last_incoming_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    owner_last_reply_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    last_auto_reply_at: Mapped[datetime | None] = mapped_column(UtcDateTime())
    quiet_window_key: Mapped[str | None] = mapped_column(Text)


class MessageLog(Base):
    __tablename__ = "message_log"
    __table_args__ = (
        CheckConstraint("direction IN ('in', 'out')", name="direction_values"),
        CheckConstraint(f"action IN ({ACTION_SQL_LIST})", name="action_values"),
        CheckConstraint(
            "category IS NULL OR category IN ('money', 'general')", name="category_values"
        ),
        CheckConstraint(
            "confidence IS NULL OR confidence BETWEEN 0.00 AND 1.00",
            name="confidence_range",
        ),
        Index(
            "ix_message_log_connection_occurred",
            "connection_id",
            sql_text("occurred_at DESC"),
        ),
        Index(
            "ix_message_log_connection_contact_occurred",
            "connection_id",
            "contact_id",
            sql_text("occurred_at DESC"),
        ),
    )

    id: Mapped[int] = mapped_column(SURROGATE_KEY, primary_key=True, autoincrement=True)
    connection_id: Mapped[int] = mapped_column(ForeignKey("connections.id", ondelete="CASCADE"))
    contact_id: Mapped[int] = mapped_column(BigInteger)
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    direction: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime(), server_default=func.now())
    action: Mapped[str] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    template_code: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(Text)
    body_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    retention_until: Mapped[datetime | None] = mapped_column(UtcDateTime())
    deleted_by_user: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("false"))


class MorningQueue(Base):
    __tablename__ = "morning_queue"

    id: Mapped[int] = mapped_column(SURROGATE_KEY, primary_key=True, autoincrement=True)
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), index=True
    )
    contact_id: Mapped[int] = mapped_column(BigInteger)
    contact_name: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime())
    summary: Mapped[str | None] = mapped_column(Text)
    is_delivered: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("false"))
    is_done: Mapped[bool] = mapped_column(Boolean, server_default=sql_text("false"))


class ShadowFeedback(Base):
    __tablename__ = "shadow_feedback"
    __table_args__ = (
        CheckConstraint("verdict IN ('ok', 'wrong', 'exclude')", name="verdict_values"),
    )

    id: Mapped[int] = mapped_column(SURROGATE_KEY, primary_key=True, autoincrement=True)
    log_id: Mapped[int] = mapped_column(
        ForeignKey("message_log.id", ondelete="CASCADE"), index=True
    )
    verdict: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), server_default=func.now())
