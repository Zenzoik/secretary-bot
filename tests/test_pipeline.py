from __future__ import annotations

import random
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from secretary_bot import models
from secretary_bot.actions import LogAction
from secretary_bot.delayed import DelayedReplyQueue, ReplyTask
from secretary_bot.hard_filter import HardFilterResult
from secretary_bot.notifications import Preview
from secretary_bot.pipeline import IncomingMessage, Pipeline
from secretary_bot.sender import BusinessReplySender
from secretary_bot.storage import (
    ConnectionSnapshot,
    Database,
    set_contact_template_override,
    upsert_connection,
)
from secretary_bot.templates import DEFAULT_TEMPLATES, TemplateCode
from tests.test_delayed import FakeSortedSet

# Monday 03:14 in Kyiv — inside the 22:00–08:00 quiet window.
NIGHT = datetime(2026, 8, 24, 0, 14, tzinfo=UTC)
DAY = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)


class FakeBot:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list[dict[str, Any]] = []
        self.read: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> Any:
        self.sent.append(kwargs)
        if self.error is not None:
            raise self.error
        return type("Sent", (), {"message_id": 999})()

    async def read_business_message(self, **kwargs: Any) -> bool:
        self.read.append(kwargs)
        return True


class FakeNotifier:
    def __init__(self) -> None:
        self.previews: list[tuple[int, Preview]] = []
        self.alerts: list[tuple[int, str]] = []

    async def preview(self, chat_id: int, preview: Preview) -> None:
        self.previews.append((chat_id, preview))

    async def alert(self, chat_id: int, text: str) -> None:
        self.alerts.append((chat_id, text))


class BrokenModel:
    async def classify(self, text: str, *, system_prompt: str, model: str) -> str:
        raise RuntimeError("llm is down")


@pytest_asyncio.fixture
async def world(database: Database):
    async with database.session() as session, session.begin():
        record = await upsert_connection(
            session,
            ConnectionSnapshot(
                business_connection_id="connection-1",
                owner_user_id=42,
                owner_chat_id=42,
                rights={"can_reply": True},
            ),
        )
        session.add(
            models.Schedule(
                connection_id=record.id,
                weekday_mask=0b1111111,
                time_from=time(22, 0),
                time_to=time(8, 0),
            )
        )

    bot = FakeBot()
    notifier = FakeNotifier()
    pipeline = Pipeline(
        database=database,
        queue=DelayedReplyQueue(client=FakeSortedSet()),
        sender=BusinessReplySender(bot=bot),
        notifier=notifier,
        model=BrokenModel(),  # keyword fallback keeps the tests offline
        rng=random.Random(1),
    )
    return pipeline, bot, notifier, database


def message(**changes: Any) -> IncomingMessage:
    base = IncomingMessage(
        business_connection_id="connection-1",
        chat_id=100,
        message_id=7,
        filter_result=HardFilterResult.ALLOWED,
        received_at=NIGHT,
        text="привет, ты тут?",
        contact_name="Вася",
    )
    return replace(base, **changes)


async def actions(database: Database) -> list[str]:
    async with database.session() as session:
        rows = await session.scalars(select(models.MessageLog).order_by(models.MessageLog.id))
        return [row.action for row in rows]


async def count(database: Database, model: Any) -> int:
    async with database.session() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def set_connection(database: Database, **values: Any) -> None:
    async with database.session() as session, session.begin():
        row = await session.scalar(select(models.Connection))
        for field, value in values.items():
            setattr(row, field, value)


async def scheduled(pipeline: Pipeline) -> list[ReplyTask]:
    return await pipeline.queue.pop_due(now=NIGHT + timedelta(minutes=10))


@pytest.mark.asyncio
async def test_night_message_is_scheduled_once_and_answered_once(world) -> None:
    pipeline, bot, notifier, database = world
    await set_connection(database, dry_run=False)

    for message_id in range(5):
        await pipeline.process_incoming(message(message_id=message_id))

    tasks = await scheduled(pipeline)
    assert len(tasks) == 1, "five messages in one window must produce one reply"

    action = await pipeline.deliver(tasks[0], now=NIGHT + timedelta(minutes=3))

    assert action is LogAction.REPLIED
    assert len(bot.sent) == 1
    assert bot.sent[0]["text"] == DEFAULT_TEMPLATES[TemplateCode.OFF_HOURS_DEFAULT]
    assert await actions(database) == ["skipped_window_limit"] * 4 + ["replied"]


@pytest.mark.asyncio
async def test_message_outside_the_window_is_logged_and_dropped(world) -> None:
    pipeline, bot, _, database = world

    await pipeline.process_incoming(message(received_at=DAY))

    assert await scheduled(pipeline) == []
    assert await actions(database) == ["skipped_schedule"]
    assert bot.sent == []


@pytest.mark.asyncio
async def test_excluded_contact_is_left_alone(world) -> None:
    pipeline, _, _, database = world
    async with database.session() as session, session.begin():
        session.add(models.Exclusion(connection_id=1, contact_id=100))

    await pipeline.process_incoming(message())

    assert await actions(database) == ["skipped_excluded"]


@pytest.mark.asyncio
async def test_kill_switch_stops_delivery_even_after_the_gate(world) -> None:
    pipeline, bot, _, database = world
    await pipeline.process_incoming(message())
    task = (await scheduled(pipeline))[0]

    await set_connection(database, kill_switch=True)
    action = await pipeline.deliver(task, now=NIGHT + timedelta(minutes=3))

    assert action is LogAction.SKIPPED_KILL_SWITCH
    assert bot.sent == []


@pytest.mark.asyncio
async def test_temporary_mute_stops_a_task_scheduled_before_the_command(world) -> None:
    pipeline, bot, _, database = world
    await pipeline.process_incoming(message())
    task = (await scheduled(pipeline))[0]

    await set_connection(database, muted_until=NIGHT + timedelta(hours=3))
    action = await pipeline.deliver(task, now=NIGHT + timedelta(minutes=3))

    assert action is LogAction.SKIPPED_KILL_SWITCH
    assert bot.sent == []


@pytest.mark.asyncio
async def test_contact_template_override_wins_over_classification(world) -> None:
    pipeline, _, _, database = world
    async with database.session() as session, session.begin():
        await set_contact_template_override(
            session,
            1,
            100,
            template_code=TemplateCode.MONEY_PRIORITY.value,
            template_text=DEFAULT_TEMPLATES[TemplateCode.MONEY_PRIORITY],
        )

    await pipeline.process_incoming(message(text="обычный вопрос"))
    task = (await scheduled(pipeline))[0]

    assert task.category == "general"
    assert task.template_code == "money_priority"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result",
    [HardFilterResult.BOT_SENDER, HardFilterResult.SERVICE_SENDER, HardFilterResult.NON_PRIVATE],
)
async def test_filtered_senders_never_reach_the_gate(world, result: HardFilterResult) -> None:
    pipeline, _, _, database = world

    await pipeline.process_incoming(message(filter_result=result))

    assert await actions(database) == []
    assert await count(database, models.ContactActivity) == 0


@pytest.mark.asyncio
async def test_unsupported_content_is_logged_but_not_answered(world) -> None:
    pipeline, _, _, database = world

    await pipeline.process_incoming(
        message(filter_result=HardFilterResult.UNSUPPORTED_CONTENT, text="")
    )

    assert await actions(database) == ["skipped_unsupported_content"]
    assert await scheduled(pipeline) == []


@pytest.mark.asyncio
async def test_owner_reply_before_the_delay_cancels_the_auto_reply(world) -> None:
    pipeline, bot, _, database = world
    await set_connection(database, dry_run=False)
    await pipeline.process_incoming(message())
    task = (await scheduled(pipeline))[0]

    await pipeline.process_incoming(
        message(
            filter_result=HardFilterResult.OWNER_MESSAGE,
            received_at=NIGHT + timedelta(minutes=1),
            message_id=8,
        )
    )
    action = await pipeline.deliver(task, now=NIGHT + timedelta(minutes=3))

    assert action is LogAction.SKIPPED_OWNER_REPLIED
    assert bot.sent == []


@pytest.mark.asyncio
async def test_an_older_owner_reply_does_not_cancel_a_new_message(world) -> None:
    pipeline, bot, _, database = world
    await set_connection(database, dry_run=False)
    await pipeline.process_incoming(
        message(
            filter_result=HardFilterResult.OWNER_MESSAGE, received_at=NIGHT - timedelta(hours=2)
        )
    )

    await pipeline.process_incoming(message())
    action = await pipeline.deliver((await scheduled(pipeline))[0], now=NIGHT)

    assert action is LogAction.REPLIED
    assert len(bot.sent) == 1


@pytest.mark.asyncio
async def test_dry_run_shows_the_owner_what_would_have_been_sent(world) -> None:
    pipeline, bot, notifier, database = world

    await pipeline.process_incoming(message(text="скинь реквизиты, слово буратино"))
    action = await pipeline.deliver((await scheduled(pipeline))[0], now=NIGHT)

    assert action is LogAction.DRY_RUN
    assert bot.sent == [], "the contact must receive nothing in dry run"
    chat_id, preview = notifier.previews[0]
    assert chat_id == 42
    assert preview.category == "money"
    assert preview.reply_text == DEFAULT_TEMPLATES[TemplateCode.MONEY_PRIORITY]
    assert "03:14" in preview.render(), "the owner sees his own timezone"
    assert "буратино" not in preview.render(), "message bodies never leave the process"


@pytest.mark.asyncio
async def test_money_message_lands_in_the_morning_queue(world) -> None:
    pipeline, _, _, database = world
    await set_connection(database, dry_run=False)

    await pipeline.process_incoming(message(text="когда будет оплата?"))
    await pipeline.deliver((await scheduled(pipeline))[0], now=NIGHT)

    async with database.session() as session:
        row = await session.scalar(select(models.MorningQueue))
    assert row is not None
    assert row.contact_name == "Вася"
    assert row.is_delivered is False


@pytest.mark.asyncio
async def test_ordinary_message_does_not_reach_the_morning_queue(world) -> None:
    pipeline, _, _, database = world
    await set_connection(database, dry_run=False)

    await pipeline.process_incoming(message())
    await pipeline.deliver((await scheduled(pipeline))[0], now=NIGHT)

    assert await count(database, models.MorningQueue) == 0


@pytest.mark.asyncio
async def test_broken_llm_falls_back_to_the_dictionary_without_crashing(world) -> None:
    pipeline, _, notifier, _ = world

    await pipeline.process_incoming(message(text="нужен инвойс на аванс"))
    task = (await scheduled(pipeline))[0]

    assert task.category == "money"
    assert task.confidence is None


@pytest.mark.asyncio
async def test_closed_business_chat_is_logged_as_an_error_without_retries(world) -> None:
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage

    pipeline, bot, notifier, database = world
    await set_connection(database, dry_run=False)
    bot.error = TelegramBadRequest(
        method=SendMessage(chat_id=100, text="…"), message="Bad Request: BUSINESS_CHAT_INACTIVE"
    )

    await pipeline.process_incoming(message())
    action = await pipeline.deliver((await scheduled(pipeline))[0], now=NIGHT)

    assert action is LogAction.ERROR
    assert len(bot.sent) == 1
    assert notifier.alerts == []
    async with database.session() as session:
        row = await session.scalar(
            select(models.MessageLog).where(models.MessageLog.action == "error")
        )
    assert row is not None and row.error_code == "BUSINESS_CHAT_INACTIVE"


@pytest.mark.asyncio
async def test_invalid_connection_alerts_the_owner(world) -> None:
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage

    pipeline, bot, notifier, database = world
    await set_connection(database, dry_run=False)
    bot.error = TelegramBadRequest(
        method=SendMessage(chat_id=100, text="…"), message="Forbidden: BUSINESS_CONNECTION_INVALID"
    )

    await pipeline.process_incoming(message())
    action = await pipeline.deliver((await scheduled(pipeline))[0], now=NIGHT)

    assert action is LogAction.ERROR
    assert notifier.alerts and notifier.alerts[0][0] == 42
    async with database.session() as session:
        connection = await session.scalar(select(models.Connection))
    assert connection is not None
    assert connection.is_active is False
    assert connection.kill_switch is True


@pytest.mark.asyncio
async def test_scheduled_task_retains_its_sender_identity(world) -> None:
    pipeline, bot, _, database = world
    await set_connection(database, dry_run=False, sender_identity="bot")
    await pipeline.process_incoming(message())
    task = (await scheduled(pipeline))[0]

    await set_connection(database, sender_identity="owner")
    await pipeline.deliver(task, now=NIGHT)

    assert task.sender_identity == "bot"
    assert bot.sent[0]["text"] == DEFAULT_TEMPLATES[TemplateCode.OFF_HOURS_DEFAULT]


@pytest.mark.asyncio
async def test_owner_identity_uses_configured_delay_and_no_prefix(world) -> None:
    pipeline, bot, _, database = world
    await set_connection(
        database,
        dry_run=False,
        sender_identity="owner",
        delay_min_seconds=17,
        delay_max_seconds=17,
    )

    await pipeline.process_incoming(message())

    assert await pipeline.queue.pop_due(now=NIGHT + timedelta(seconds=16)) == []
    task = (await pipeline.queue.pop_due(now=NIGHT + timedelta(seconds=17)))[0]
    await pipeline.deliver(task, now=NIGHT + timedelta(seconds=17))
    assert bot.sent[0]["text"] == DEFAULT_TEMPLATES[TemplateCode.OFF_HOURS_DEFAULT]


@pytest.mark.asyncio
async def test_bot_identity_uses_the_technical_delay(world) -> None:
    pipeline, _, _, database = world
    await set_connection(database, sender_identity="bot", bot_delay_seconds=5)

    await pipeline.process_incoming(message())

    assert await pipeline.queue.pop_due(now=NIGHT + timedelta(seconds=4)) == []
    task = (await pipeline.queue.pop_due(now=NIGHT + timedelta(seconds=5)))[0]
    assert task.sender_identity == "bot"


@pytest.mark.asyncio
async def test_successful_send_marks_read_only_when_enabled(world) -> None:
    pipeline, bot, _, database = world
    await set_connection(
        database,
        dry_run=False,
        mark_read=True,
        rights_json={"can_reply": True, "can_read_messages": True},
    )

    await pipeline.process_incoming(message())
    await pipeline.deliver((await scheduled(pipeline))[0], now=NIGHT)

    assert bot.read == [
        {
            "business_connection_id": "connection-1",
            "chat_id": 100,
            "message_id": 7,
        }
    ]


@pytest.mark.asyncio
async def test_successful_send_leaves_message_unread_by_default(world) -> None:
    pipeline, bot, _, database = world
    await set_connection(database, dry_run=False)

    await pipeline.process_incoming(message())
    await pipeline.deliver((await scheduled(pipeline))[0], now=NIGHT)

    assert bot.read == []


@pytest.mark.asyncio
async def test_unknown_connection_is_ignored(world) -> None:
    pipeline, _, _, database = world

    await pipeline.process_incoming(message(business_connection_id="other"))

    assert await actions(database) == []


@pytest.mark.asyncio
async def test_no_message_body_is_ever_written_to_the_database(world) -> None:
    pipeline, _, _, database = world
    secret = "скинь реквизиты, вот мой пароль"

    await pipeline.process_incoming(message(text=secret))
    await pipeline.deliver((await scheduled(pipeline))[0], now=NIGHT)

    async with database.session() as session:
        for table in models.Base.metadata.sorted_tables:
            rows = (await session.execute(select(table))).mappings().all()
            for row in rows:
                assert secret not in " ".join(str(value) for value in row.values())
