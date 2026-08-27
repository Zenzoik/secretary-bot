from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from secretary_bot import models
from secretary_bot.actions import LogAction
from secretary_bot.classifier import ClassifierSettings
from secretary_bot.gate import ConnectionPolicy, ContactState, Exclusion, QuietWindow


@dataclass(frozen=True, slots=True)
class ConnectionSnapshot:
    """What a ``business_connection`` update tells us about a connection."""

    business_connection_id: str
    owner_user_id: int
    owner_chat_id: int | None = None
    owner_username: str | None = None
    rights: dict[str, Any] | None = None
    is_enabled: bool = True


@dataclass(frozen=True, slots=True)
class ConnectionRecord:
    id: int
    business_connection_id: str
    owner_user_id: int
    owner_chat_id: int | None
    dry_run: bool
    policy: ConnectionPolicy


@dataclass(slots=True)
class Database:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]

    @classmethod
    def from_url(cls, url: str, **engine_options: Any) -> Database:
        engine = create_async_engine(url, **engine_options)
        return cls(
            engine=engine, session_factory=async_sessionmaker(engine, expire_on_commit=False)
        )

    def session(self) -> AsyncSession:
        return self.session_factory()

    async def aclose(self) -> None:
        await self.engine.dispose()


async def upsert_connection(
    session: AsyncSession, snapshot: ConnectionSnapshot
) -> ConnectionRecord:
    """Create or refresh the connection row; ``is_enabled=false`` marks it dead."""
    row = await _connection_row(session, snapshot.business_connection_id)
    if row is None:
        row = models.Connection(business_connection_id=snapshot.business_connection_id)
        session.add(row)

    row.owner_user_id = snapshot.owner_user_id
    row.owner_username = snapshot.owner_username
    row.rights_json = snapshot.rights or {}
    row.is_active = snapshot.is_enabled
    if snapshot.owner_chat_id is not None:
        row.owner_chat_id = snapshot.owner_chat_id
    await session.flush()
    return await _record(session, row)


async def load_connection(
    session: AsyncSession, business_connection_id: str
) -> ConnectionRecord | None:
    row = await _connection_row(session, business_connection_id)
    return None if row is None else await _record(session, row)


async def load_contact_state(
    session: AsyncSession, connection_id: int, contact_id: int
) -> ContactState:
    exclusion_row = await session.scalar(
        select(models.Exclusion).where(
            models.Exclusion.connection_id == connection_id,
            models.Exclusion.contact_id == contact_id,
        )
    )
    activity = await session.get(models.ContactActivity, (connection_id, contact_id))
    return ContactState(
        exclusion=None if exclusion_row is None else Exclusion(until=exclusion_row.until),
        last_auto_reply_window_key=None if activity is None else activity.quiet_window_key,
    )


async def load_templates(session: AsyncSession, connection_id: int) -> dict[str, str]:
    rows = await session.scalars(
        select(models.Template).where(
            models.Template.connection_id == connection_id,
            models.Template.is_active.is_(True),
        )
    )
    return {row.code: row.text for row in rows}


async def load_classifier_settings(
    session: AsyncSession, connection_id: int, *, defaults: ClassifierSettings | None = None
) -> ClassifierSettings:
    """Owner-editable prompt from the database, falling back to the shipped one."""
    defaults = defaults or ClassifierSettings()
    row = await session.scalar(
        select(models.Prompt).where(
            models.Prompt.connection_id == connection_id, models.Prompt.code == "classifier"
        )
    )
    if row is None:
        return defaults
    return ClassifierSettings(
        system_prompt=row.system_prompt,
        model=row.model,
        confidence_min=Decimal(row.confidence_min),
        timeout_seconds=defaults.timeout_seconds,
    )


async def log_decision(
    session: AsyncSession,
    *,
    connection_id: int,
    contact_id: int,
    action: LogAction,
    tg_message_id: int | None = None,
    direction: str = "in",
    category: str | None = None,
    confidence: Decimal | None = None,
    template_code: str | None = None,
    error_code: str | None = None,
    occurred_at: datetime | None = None,
) -> int:
    """Write one decision. Message bodies never reach this table (NFR-2)."""
    row = models.MessageLog(
        connection_id=connection_id,
        contact_id=contact_id,
        tg_message_id=tg_message_id,
        direction=direction,
        action=action.value,
        category=category,
        confidence=confidence,
        template_code=template_code,
        error_code=error_code,
    )
    if occurred_at is not None:
        row.occurred_at = occurred_at
    session.add(row)
    await session.flush()
    return row.id


async def record_incoming(
    session: AsyncSession, connection_id: int, contact_id: int, *, at: datetime
) -> None:
    await _touch_activity(session, connection_id, contact_id, last_incoming_at=at)


async def record_owner_reply(
    session: AsyncSession, connection_id: int, contact_id: int, *, at: datetime
) -> None:
    await _touch_activity(session, connection_id, contact_id, owner_last_reply_at=at)


async def claim_window(
    session: AsyncSession, connection_id: int, contact_id: int, *, window_key: str | None
) -> None:
    """FR-7: take the window as soon as a reply is scheduled.

    Claiming at delivery time would let five messages sent a minute apart each
    schedule their own reply. A claim that is later cancelled costs nothing —
    one missed auto-reply is invisible, five are not.
    """
    await _touch_activity(session, connection_id, contact_id, quiet_window_key=window_key)


async def record_auto_reply(
    session: AsyncSession,
    connection_id: int,
    contact_id: int,
    *,
    at: datetime,
    window_key: str | None,
) -> None:
    await _touch_activity(
        session, connection_id, contact_id, last_auto_reply_at=at, quiet_window_key=window_key
    )


async def owner_replied_since(
    session: AsyncSession, connection_id: int, contact_id: int, *, moment: datetime
) -> bool:
    """FR-9: did the owner answer this chat himself while we were waiting?"""
    activity = await session.get(models.ContactActivity, (connection_id, contact_id))
    if activity is None or activity.owner_last_reply_at is None:
        return False
    return activity.owner_last_reply_at >= moment


async def enqueue_morning(
    session: AsyncSession,
    *,
    connection_id: int,
    contact_id: int,
    occurred_at: datetime,
    contact_name: str | None = None,
    summary: str | None = None,
) -> int:
    row = models.MorningQueue(
        connection_id=connection_id,
        contact_id=contact_id,
        contact_name=contact_name,
        occurred_at=occurred_at,
        summary=summary,
    )
    session.add(row)
    await session.flush()
    return row.id


async def list_connections(session: AsyncSession) -> list[ConnectionRecord]:
    rows = await session.scalars(select(models.Connection))
    return [await _record(session, row) for row in rows]


async def pending_morning(session: AsyncSession, connection_id: int) -> list[models.MorningQueue]:
    rows = await session.scalars(
        select(models.MorningQueue)
        .where(
            models.MorningQueue.connection_id == connection_id,
            models.MorningQueue.is_delivered.is_(False),
        )
        .order_by(models.MorningQueue.occurred_at)
    )
    return list(rows)


async def mark_morning_delivered(session: AsyncSession, ids: Sequence[int]) -> None:
    if not ids:
        return
    await session.execute(
        update(models.MorningQueue).where(models.MorningQueue.id.in_(ids)).values(is_delivered=True)
    )


async def record_feedback(session: AsyncSession, *, log_id: int, verdict: str) -> int:
    row = models.ShadowFeedback(log_id=log_id, verdict=verdict)
    session.add(row)
    await session.flush()
    return row.id


async def _connection_row(
    session: AsyncSession, business_connection_id: str
) -> models.Connection | None:
    return await session.scalar(
        select(models.Connection).where(
            models.Connection.business_connection_id == business_connection_id
        )
    )


async def _record(session: AsyncSession, row: models.Connection) -> ConnectionRecord:
    schedules = await session.scalars(
        select(models.Schedule).where(models.Schedule.connection_id == row.id)
    )
    windows = tuple(
        QuietWindow(
            schedule_id=schedule.id,
            weekday_mask=schedule.weekday_mask,
            time_from=schedule.time_from,
            time_to=schedule.time_to,
            is_active=schedule.is_active,
        )
        for schedule in schedules
    )
    return ConnectionRecord(
        id=row.id,
        business_connection_id=row.business_connection_id,
        owner_user_id=row.owner_user_id,
        owner_chat_id=row.owner_chat_id,
        dry_run=row.dry_run,
        policy=ConnectionPolicy(
            timezone=row.timezone,
            windows=windows,
            is_active=row.is_active,
            kill_switch=row.kill_switch,
        ),
    )


async def _touch_activity(
    session: AsyncSession, connection_id: int, contact_id: int, **values: object
) -> None:
    activity = await session.get(models.ContactActivity, (connection_id, contact_id))
    if activity is None:
        activity = models.ContactActivity(connection_id=connection_id, contact_id=contact_id)
        session.add(activity)
    for field, value in values.items():
        setattr(activity, field, value)
    await session.flush()
