from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from secretary_bot import models
from secretary_bot.classifier import ClassifierSettings
from secretary_bot.daily_summary import DailySummary, summary_item_keyboard, summary_period
from secretary_bot.retention import MESSAGE_RETENTION, MessageCipher, MessageContext
from secretary_bot.storage import (
    ConnectionSnapshot,
    capture_message,
    load_owner_connection,
    upsert_connection,
)

NOW = datetime(2026, 9, 2, 9, 5, tzinfo=UTC)
SCHEDULED = datetime(2026, 9, 2, 9, 0, tzinfo=UTC)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> Any:
        self.sent.append(kwargs)
        return type("Sent", (), {"message_id": len(self.sent)})()


class FakeModel:
    def __init__(self) -> None:
        self.transcripts: list[str] = []

    async def summarize_dialogue(
        self, transcript: str, *, system_prompt: str, model: str
    ) -> str:
        self.transcripts.append(transcript)
        return json.dumps(
            {
                "topic": "Строк оплати",
                "agreements": ["Рахунок буде сьогодні"],
                "open_questions": [],
                "questions_asked": 1,
                "questions_closed": 1,
            },
            ensure_ascii=False,
        )


def test_summary_chat_button_uses_callback_without_known_username() -> None:
    button = summary_item_keyboard(7).inline_keyboard[1][0]

    assert button.url is None
    assert button.callback_data == "summary:open:7"


async def seed_message(
    session,
    cipher: MessageCipher,
    *,
    connection_id: int,
    contact_id: int,
    message_id: int,
    direction: str,
    text: str,
    occurred_at: datetime,
) -> None:
    encrypted = cipher.encrypt(
        text,
        context=MessageContext(connection_id, contact_id, message_id, direction),
    )
    await capture_message(
        session,
        connection_id=connection_id,
        contact_id=contact_id,
        tg_message_id=message_id,
        direction=direction,
        occurred_at=occurred_at,
        body_encrypted=encrypted,
        retention_until=occurred_at + MESSAGE_RETENTION,
    )


@pytest.mark.asyncio
async def test_daily_summary_sends_active_dialogue_once_and_persists_items(database) -> None:
    cipher = MessageCipher.from_encoded_key(MessageCipher.generate_encoded_key())
    async with database.session() as session, session.begin():
        connection = await upsert_connection(
            session,
            ConnectionSnapshot(
                business_connection_id="summary-connection",
                owner_user_id=42,
                owner_chat_id=42,
                rights={"can_reply": True},
            ),
        )
        row = await session.get(models.Connection, connection.id)
        assert row is not None
        row.message_retention_enabled = True
        row.summary_time = time(12, 0)
        row.summary_channel_id = -100123
        session.add(
            models.ContactActivity(
                connection_id=connection.id,
                contact_id=100,
                contact_name="Клієнт",
                contact_username="client_test",
            )
        )
        await seed_message(
            session,
            cipher,
            connection_id=connection.id,
            contact_id=100,
            message_id=10,
            direction="in",
            text="Коли буде рахунок?",
            occurred_at=SCHEDULED - timedelta(hours=1),
        )
        await seed_message(
            session,
            cipher,
            connection_id=connection.id,
            contact_id=100,
            message_id=11,
            direction="out",
            text="Рахунок буде сьогодні.",
            occurred_at=SCHEDULED - timedelta(minutes=50),
        )
        session.add(
            models.MorningQueue(
                connection_id=connection.id,
                contact_id=100,
                contact_name="Клієнт",
                occurred_at=SCHEDULED - timedelta(hours=1),
            )
        )
        # Still encrypted but outside the 24-hour summary period.
        await seed_message(
            session,
            cipher,
            connection_id=connection.id,
            contact_id=200,
            message_id=20,
            direction="in",
            text="Старий неактивний діалог",
            occurred_at=SCHEDULED - timedelta(hours=25),
        )

    bot = FakeBot()
    model = FakeModel()
    digest = DailySummary(
        database=database,
        bot=bot,
        cipher=cipher,
        model=model,
        classifier_defaults=ClassifierSettings(),
    )

    assert await digest.run_once(now=NOW) == 1
    assert await digest.run_once(now=NOW + timedelta(minutes=1)) == 0

    assert len(model.transcripts) == 1
    assert "Коли буде рахунок?" in model.transcripts[0]
    assert "Старий неактивний діалог" not in model.transcripts[0]
    assert len(bot.sent) == 2
    assert all(message["chat_id"] == -100123 for message in bot.sent)
    assert "💸 Відповісти вранці: 1" in bot.sent[0]["text"]
    header_button = bot.sent[0]["reply_markup"].inline_keyboard[0][0]
    assert header_button.callback_data == "summary:read:1"
    item_buttons = bot.sent[1]["reply_markup"].inline_keyboard
    assert item_buttons[0][0].callback_data == "summary:resolve:1"
    assert item_buttons[1][0].url == "https://t.me/client_test"
    assert item_buttons[2][0].callback_data == "summary:reply:1"
    assert "Строк оплати" in bot.sent[1]["text"]
    assert "Обіцяли відповісти вранці" in bot.sent[1]["text"]
    assert "немає" in bot.sent[1]["text"]

    async with database.session() as session:
        run = await session.scalar(select(models.SummaryRun))
        item = await session.scalar(select(models.SummaryItem))
        morning = await session.scalar(select(models.MorningQueue))
    assert run is not None and run.status == "delivered"
    assert run.telegram_message_id == 1
    assert item is not None and item.telegram_message_id == 2
    assert item.contact_username == "client_test"
    assert item.money_priority is True
    assert item.last_incoming_message_id == 10
    assert morning is not None and morning.is_delivered is True


@pytest.mark.asyncio
async def test_summary_period_obeys_local_time_and_connection_state(database) -> None:
    async with database.session() as session, session.begin():
        await upsert_connection(
            session,
            ConnectionSnapshot(
                business_connection_id="period",
                owner_user_id=42,
                rights={"can_reply": True},
            ),
        )
        row = await session.scalar(select(models.Connection))
        assert row is not None
        row.summary_time = time(12, 0)

    async with database.session() as session:
        connection = await load_owner_connection(session, 42)
    assert connection is not None
    assert summary_period(connection, now=NOW) == (
        SCHEDULED - timedelta(hours=24),
        SCHEDULED,
    )
    assert summary_period(connection, now=NOW + timedelta(hours=1)) is None

    async with database.session() as session, session.begin():
        row = await session.scalar(select(models.Connection))
        assert row is not None
        row.kill_switch = True
    async with database.session() as session:
        connection = await load_owner_connection(session, 42)
    assert connection is not None
    assert summary_period(connection, now=NOW) is None
