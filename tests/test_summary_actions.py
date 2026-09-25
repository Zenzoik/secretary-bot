from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from secretary_bot import models
from secretary_bot.actions import LogAction
from secretary_bot.sender import BusinessReplySender
from secretary_bot.storage import ConnectionSnapshot, ensure_master, upsert_connection
from secretary_bot.summary_actions import (
    SummaryActions,
    parse_direct_reply_callback,
    parse_summary_callback,
)
from secretary_bot.texts import BOT_IDENTITY_SUFFIX, BUTTON_SEND_BOT

NOW = datetime(2026, 9, 2, 12, tzinfo=UTC)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.read: list[dict[str, Any]] = []
        self.answered: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.chat_username: str | None = "first_contact"

    async def send_message(self, **kwargs: Any) -> Any:
        self.sent.append(kwargs)
        return type("Sent", (), {"message_id": 100 + len(self.sent)})()

    async def read_business_message(self, **kwargs: Any) -> bool:
        self.read.append(kwargs)
        return True

    async def answer_callback_query(self, callback_query_id: str, **kwargs: Any) -> bool:
        self.answered.append({"id": callback_query_id, **kwargs})
        return True

    async def edit_message_text(self, **kwargs: Any) -> bool:
        self.edited.append(kwargs)
        return True

    async def edit_message_reply_markup(self, **kwargs: Any) -> bool:
        self.edited.append(kwargs)
        return True

    async def get_chat(self, chat_id: int | str) -> Any:
        return type("Chat", (), {"id": chat_id, "username": self.chat_username})()


def callback(data: str, *, owner_id: int = 42, message_id: int = 50) -> CallbackQuery:
    return CallbackQuery.model_validate(
        {
            "id": f"callback-{data}",
            "from": {"id": owner_id, "is_bot": False, "first_name": "Owner"},
            "chat_instance": "channel-instance",
            "data": data,
            "message": {
                "message_id": message_id,
                "date": NOW,
                "chat": {"id": -100123, "type": "channel", "title": "Summary"},
                "text": "Summary card",
            },
        }
    )


def reply_message(text: str, *, reply_to_message_id: int) -> Message:
    return Message.model_validate(
        {
            "message_id": 70,
            "date": NOW,
            "chat": {"id": 42, "type": "private", "first_name": "Owner"},
            "from": {"id": 42, "is_bot": False, "first_name": "Owner"},
            "text": text,
            "reply_to_message": {
                "message_id": reply_to_message_id,
                "date": NOW,
                "chat": {"id": 42, "type": "private", "first_name": "Owner"},
                "from": {"id": 900, "is_bot": True, "first_name": "Bot"},
                "text": "Reply prompt",
            },
        }
    )


async def seed_summary(database) -> tuple[int, int, int]:
    async with database.session() as session, session.begin():
        await ensure_master(session, 42)
        connection = await upsert_connection(
            session,
            ConnectionSnapshot(
                "connection-1",
                owner_user_id=42,
                owner_chat_id=42,
                rights={"can_reply": True, "can_read_messages": True},
            ),
        )
        run = models.SummaryRun(
            connection_id=connection.id,
            period_start=NOW - timedelta(days=1),
            period_end=NOW,
            status="delivered",
            destination_chat_id=-100123,
        )
        session.add(run)
        await session.flush()
        first = models.SummaryItem(
            run_id=run.id,
            contact_id=100,
            contact_name="First",
            topic="Перше питання",
            agreements_json=[],
            open_questions_json=["Питання"],
            questions_asked=1,
            questions_closed=0,
            last_incoming_message_id=10,
        )
        second = models.SummaryItem(
            run_id=run.id,
            contact_id=200,
            contact_name="Second",
            topic="Друге питання",
            agreements_json=[],
            open_questions_json=[],
            questions_asked=1,
            questions_closed=1,
            last_incoming_message_id=20,
        )
        session.add_all([first, second])
        session.add_all(
            [
                models.ContactActivity(
                    connection_id=connection.id,
                    contact_id=100,
                    contact_name="First",
                    contact_username="first_contact",
                    last_incoming_at=NOW - timedelta(minutes=1),
                ),
                models.ContactActivity(
                    connection_id=connection.id,
                    contact_id=200,
                    contact_name="Second",
                    last_incoming_at=NOW - timedelta(minutes=2),
                ),
            ]
        )
        await session.flush()
        return run.id, first.id, second.id


def actions(database, bot: FakeBot) -> SummaryActions:
    return SummaryActions(
        database=database,
        bot=bot,
        sender=BusinessReplySender(bot=bot),
    )


@pytest.mark.asyncio
async def test_resolve_is_idempotent_and_removes_inline_keyboard(database) -> None:
    _, item_id, _ = await seed_summary(database)
    bot = FakeBot()
    handler = actions(database, bot)

    assert await handler.handle_callback(callback(f"summary:resolve:{item_id}"), now=NOW)
    assert await handler.handle_callback(callback(f"summary:resolve:{item_id}"), now=NOW)

    async with database.session() as session:
        item = await session.get(models.SummaryItem, item_id)
    assert item is not None and item.resolved_at == NOW
    assert bot.edited[0]["reply_markup"] is None
    assert "Вирішено" in bot.edited[0]["text"]
    assert bot.answered[-1]["text"] == "Уже вирішено"


@pytest.mark.asyncio
async def test_reply_fsm_only_accepts_a_reply_to_its_prompt_and_sends_as_bot(database) -> None:
    _, item_id, _ = await seed_summary(database)
    bot = FakeBot()
    handler = actions(database, bot)

    assert await handler.handle_callback(callback(f"summary:reply:{item_id}"), now=NOW)
    prompt_id = 101
    assert not await handler.handle_message(
        reply_message("Не той reply", reply_to_message_id=999), now=NOW
    )
    assert await handler.handle_message(
        reply_message("Відповім сьогодні", reply_to_message_id=prompt_id), now=NOW
    )

    business_send = next(message for message in bot.sent if "business_connection_id" in message)
    assert business_send["chat_id"] == 100
    assert business_send["business_connection_id"] == "connection-1"
    assert business_send["text"] == f"Відповім сьогодні\n\n{BOT_IDENTITY_SUFFIX}"
    async with database.session() as session:
        state = await session.get(models.SummaryReplyState, 1)
        replied = await session.scalar(
            select(func.count())
            .select_from(models.MessageLog)
            .where(models.MessageLog.action == LogAction.REPLIED.value)
        )
    assert state is None
    assert replied == 1


@pytest.mark.asyncio
async def test_persistent_button_selects_contact_and_sends_as_bot(database) -> None:
    await seed_summary(database)
    bot = FakeBot()
    handler = actions(database, bot)

    assert await handler.handle_message(
        reply_message(BUTTON_SEND_BOT, reply_to_message_id=0), now=NOW
    )
    chooser = bot.sent[-1]["reply_markup"]
    assert chooser.inline_keyboard[0][0].callback_data == "direct:select:100"
    assert "@first_contact" in chooser.inline_keyboard[0][0].text

    assert await handler.handle_callback(callback("direct:select:100"), now=NOW)
    prompt_id = 102
    assert await handler.handle_message(
        reply_message("Надішлю документи сьогодні", reply_to_message_id=prompt_id),
        now=NOW,
    )

    business_send = next(message for message in bot.sent if "business_connection_id" in message)
    assert business_send["chat_id"] == 100
    assert business_send["text"] == (
        f"Надішлю документи сьогодні\n\n{BOT_IDENTITY_SUFFIX}"
    )
    async with database.session() as session:
        state = await session.get(models.DirectReplyState, 1)
    assert state is None


@pytest.mark.asyncio
async def test_direct_reply_reports_closed_telegram_window(database) -> None:
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendMessage

    class InactiveChatBot(FakeBot):
        async def send_message(self, **kwargs: Any) -> Any:
            if "business_connection_id" in kwargs:
                raise TelegramBadRequest(
                    method=SendMessage(chat_id=kwargs["chat_id"], text=kwargs["text"]),
                    message="Bad Request: BUSINESS_CHAT_INACTIVE",
                )
            return await super().send_message(**kwargs)

    await seed_summary(database)
    bot = InactiveChatBot()
    handler = actions(database, bot)
    await handler.handle_message(
        reply_message(BUTTON_SEND_BOT, reply_to_message_id=0), now=NOW
    )
    await handler.handle_callback(callback("direct:select:100"), now=NOW)

    assert await handler.handle_message(
        reply_message("Тест", reply_to_message_id=102), now=NOW
    )

    assert "24-годинне вікно Telegram" in bot.sent[-1]["text"]


@pytest.mark.asyncio
async def test_read_all_marks_only_dialogues_from_this_summary(database) -> None:
    run_id, _, _ = await seed_summary(database)
    bot = FakeBot()
    handler = actions(database, bot)

    assert await handler.handle_callback(callback(f"summary:read:{run_id}"), now=NOW)

    assert bot.read == [
        {"business_connection_id": "connection-1", "chat_id": 100, "message_id": 10},
        {"business_connection_id": "connection-1", "chat_id": 200, "message_id": 20},
    ]
    assert "Прочитано діалогів: 2" in bot.edited[-1]["text"]


@pytest.mark.asyncio
async def test_open_chat_replaces_fallback_with_official_username_link(database) -> None:
    _, item_id, _ = await seed_summary(database)
    bot = FakeBot()
    handler = actions(database, bot)

    assert await handler.handle_callback(callback(f"summary:open:{item_id}"), now=NOW)

    button = bot.edited[-1]["reply_markup"].inline_keyboard[1][0]
    assert button.url == "https://t.me/first_contact"
    assert "Натисніть" in bot.answered[-1]["text"]
    async with database.session() as session:
        item = await session.get(models.SummaryItem, item_id)
    assert item is not None and item.contact_username == "first_contact"


@pytest.mark.asyncio
async def test_open_chat_explains_when_contact_has_no_public_username(database) -> None:
    _, item_id, _ = await seed_summary(database)
    bot = FakeBot()
    bot.chat_username = None
    handler = actions(database, bot)

    assert await handler.handle_callback(callback(f"summary:open:{item_id}"), now=NOW)

    assert bot.edited == []
    assert bot.answered[-1]["show_alert"] is True
    assert "немає публічного username" in bot.answered[-1]["text"]


@pytest.mark.asyncio
async def test_summary_action_from_another_user_is_rejected(database) -> None:
    run_id, item_id, _ = await seed_summary(database)
    bot = FakeBot()
    handler = actions(database, bot)

    assert not await handler.handle_callback(
        callback(f"summary:resolve:{item_id}", owner_id=99), now=NOW
    )
    assert not await handler.handle_callback(
        callback(f"summary:read:{run_id}", owner_id=99), now=NOW
    )
    assert bot.sent == bot.read == bot.answered == bot.edited == []


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("summary:resolve:7", ("resolve", 7)),
        ("summary:reply:8", ("reply", 8)),
        ("summary:read:9", ("read", 9)),
        ("summary:open:10", ("open", 10)),
        ("summary:unknown:1", None),
        ("summary:read:0", None),
    ],
)
def test_summary_callback_parser(data: str, expected: tuple[str, int] | None) -> None:
    assert parse_summary_callback(data) == expected


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("direct:select:100", ("select", 100)),
        ("direct:cancel:0", ("cancel", 0)),
        ("direct:select:0", None),
        ("direct:cancel:1", None),
        ("summary:reply:1", None),
    ],
)
def test_direct_reply_callback_parser(
    data: str, expected: tuple[str, int] | None
) -> None:
    assert parse_direct_reply_callback(data) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_direct_reply_text_is_retained_only_for_a_reviewed_contact(
    database, configured: bool
) -> None:
    from secretary_bot.retention import MessageCipher

    await seed_summary(database)
    async with database.session() as session, session.begin():
        connection = await session.scalar(select(models.Connection))
        assert connection is not None
        connection.message_retention_enabled = True
        contact = await session.get(models.ContactActivity, (connection.id, 100))
        assert contact is not None
        contact.configured_at = NOW if configured else None
    bot = FakeBot()
    handler = SummaryActions(
        database=database,
        bot=bot,
        sender=BusinessReplySender(bot=bot),
        cipher=MessageCipher.from_encoded_key(MessageCipher.generate_encoded_key()),
    )

    await handler.handle_message(reply_message(BUTTON_SEND_BOT, reply_to_message_id=0), now=NOW)
    await handler.handle_callback(callback("direct:select:100"), now=NOW)
    assert await handler.handle_message(
        reply_message("Надішлю документи", reply_to_message_id=102), now=NOW
    )

    assert any("business_connection_id" in message for message in bot.sent)
    async with database.session() as session:
        captured = await session.scalar(
            select(func.count())
            .select_from(models.MessageLog)
            .where(models.MessageLog.action == LogAction.CAPTURED.value)
        )
    assert captured == (1 if configured else 0)
