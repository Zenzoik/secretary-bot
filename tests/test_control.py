from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest
from aiogram.types import Message

from secretary_bot import models
from secretary_bot.actions import LogAction
from secretary_bot.control import ControlPlane
from secretary_bot.gate import ContactState, GateDecision, evaluate_gate
from secretary_bot.storage import (
    ConnectionSnapshot,
    Database,
    load_owner_connection,
    log_decision,
    upsert_connection,
)

NOW = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)


def owner_message(text: str, *, owner_id: int = 42, chat_id: int = 42) -> Message:
    return Message.model_validate(
        {
            "message_id": 1,
            "date": NOW,
            "chat": {"id": chat_id, "type": "private", "first_name": "Owner"},
            "from": {"id": owner_id, "is_bot": False, "first_name": "Owner"},
            "text": text,
        }
    )


async def store_owner(database: Database) -> int:
    async with database.session() as session, session.begin():
        connection = await upsert_connection(
            session,
            ConnectionSnapshot(
                business_connection_id="connection-1",
                owner_user_id=42,
                owner_chat_id=42,
            ),
        )
        session.add(
            models.Schedule(
                connection_id=connection.id,
                weekday_mask=127,
                time_from=time(0, 0),
                time_to=time(23, 59),
            )
        )
        return connection.id


@pytest.mark.asyncio
async def test_off_status_and_on_change_the_persistent_gate(database: Database) -> None:
    await store_owner(database)
    bot = FakeBot()
    control = ControlPlane(database, bot)

    assert await control.handle_message(owner_message("/off"), now=NOW)
    assert await control.handle_message(owner_message("/status"), now=NOW)
    async with database.session() as session:
        connection = await load_owner_connection(session, 42)
        assert connection is not None
        assert connection.policy.kill_switch is True
    assert "выключен" in bot.sent[-1]["text"]

    assert await control.handle_message(owner_message("/on"), now=NOW)
    async with database.session() as session:
        connection = await load_owner_connection(session, 42)
        assert connection is not None
        assert connection.policy.kill_switch is False
        assert connection.policy.muted_until is None


@pytest.mark.asyncio
async def test_mute_blocks_for_requested_hours_then_expires(database: Database) -> None:
    await store_owner(database)
    bot = FakeBot()
    control = ControlPlane(database, bot)

    assert await control.handle_message(owner_message("/mute 3"), now=NOW)
    async with database.session() as session:
        connection = await load_owner_connection(session, 42)
        assert connection is not None
        assert connection.policy.muted_until == NOW + timedelta(hours=3)
        assert evaluate_gate(connection.policy, ContactState(), now=NOW).decision is (
            GateDecision.SKIPPED_KILL_SWITCH
        )
        assert (
            evaluate_gate(connection.policy, ContactState(), now=NOW + timedelta(hours=3)).decision
            is GateDecision.ALLOWED
        )


@pytest.mark.asyncio
async def test_invalid_mute_does_not_change_state(database: Database) -> None:
    await store_owner(database)
    bot = FakeBot()
    control = ControlPlane(database, bot)

    assert await control.handle_message(owner_message("/mute forever"), now=NOW)
    async with database.session() as session:
        connection = await load_owner_connection(session, 42)
        assert connection is not None and connection.policy.muted_until is None
    assert "Использование" in bot.sent[-1]["text"]


@pytest.mark.asyncio
async def test_today_lists_only_the_owners_local_day(database: Database) -> None:
    connection_id = await store_owner(database)
    async with database.session() as session, session.begin():
        await log_decision(
            session,
            connection_id=connection_id,
            contact_id=100,
            action=LogAction.DRY_RUN,
            category="general",
            occurred_at=NOW,
        )
        await log_decision(
            session,
            connection_id=connection_id,
            contact_id=100,
            action=LogAction.SKIPPED_SCHEDULE,
            occurred_at=NOW - timedelta(days=1),
        )

    bot = FakeBot()
    assert await ControlPlane(database, bot).handle_message(owner_message("/today"), now=NOW)

    assert "dry_run/general: 1" in bot.sent[-1]["text"]
    assert "skipped_schedule" not in bot.sent[-1]["text"]


@pytest.mark.asyncio
async def test_unknown_or_unauthorized_commands_are_ignored(database: Database) -> None:
    await store_owner(database)
    bot = FakeBot()
    control = ControlPlane(database, bot)

    assert not await control.handle_message(owner_message("/unknown"), now=NOW)
    assert not await control.handle_message(owner_message("/off", owner_id=7, chat_id=7), now=NOW)
    assert bot.sent == []
