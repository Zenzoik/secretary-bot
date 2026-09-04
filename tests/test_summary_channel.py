from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.types import Message

from secretary_bot import models
from secretary_bot.storage import ConnectionSnapshot, Database, upsert_connection
from secretary_bot.summary_channel import (
    SummaryChannelConnector,
    SummaryChannelError,
    parse_channel_reference,
)

NOW = datetime(2026, 9, 4, 16, 0, tzinfo=UTC)


class FakeBot:
    id = 999

    def __init__(self) -> None:
        self.prepared: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.owner_status = "creator"
        self.bot_status = "administrator"
        self.can_post = True

    async def save_prepared_keyboard_button(self, **kwargs: Any) -> Any:
        self.prepared.append(kwargs)
        return SimpleNamespace(id="prepared-channel-request")

    async def get_chat(self, chat_id: int | str) -> Any:
        resolved = -1004449496864 if chat_id == "@testsecretarychannel" else int(chat_id)
        return SimpleNamespace(id=resolved, type="channel", title="test secretary")

    async def get_chat_member(self, chat_id: int, user_id: int) -> Any:
        if user_id == self.id:
            return SimpleNamespace(status=self.bot_status, can_post_messages=self.can_post)
        return SimpleNamespace(status=self.owner_status)

    async def send_message(self, **kwargs: Any) -> Any:
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=1)


async def seed_connection(database: Database) -> models.Connection:
    async with database.session() as session, session.begin():
        return await upsert_connection(
            session,
            ConnectionSnapshot(
                business_connection_id="summary-channel",
                owner_user_id=42,
                owner_chat_id=42,
                rights={"can_reply": True},
            ),
        )


@pytest.mark.asyncio
async def test_native_request_connects_selected_channel(database: Database) -> None:
    connection = await seed_connection(database)
    bot = FakeBot()
    connector = SummaryChannelConnector(database=database, bot=bot)
    async with database.session() as session, session.begin():
        stored = await session.get(models.Connection, connection.id)
        assert stored is not None
        prepared = await connector.prepare_request(
            session, connection=stored, owner_user_id=42, now=NOW
        )

    assert prepared.prepared_id == "prepared-channel-request"
    button = bot.prepared[0]["button"]
    assert button.request_chat.chat_is_channel is True
    assert button.request_chat.bot_administrator_rights.can_post_messages is True

    message = Message.model_validate(
        {
            "message_id": 5,
            "date": int(NOW.timestamp()),
            "chat": {"id": 42, "type": "private", "first_name": "Owner"},
            "from": {"id": 42, "is_bot": False, "first_name": "Owner"},
            "chat_shared": {
                "request_id": prepared.request_id,
                "chat_id": -1004449496864,
                "title": "test secretary",
            },
        }
    )
    assert await connector.handle_message(message, now=NOW)

    async with database.session() as session:
        stored = await session.get(models.Connection, connection.id)
        request = await session.get(models.SummaryChannelRequest, prepared.request_id)
        assert stored is not None and request is not None
        assert stored.summary_channel_id == -1004449496864
        assert stored.summary_channel_title == "test secretary"
        assert request.status == "connected"
        assert request.channel_id == -1004449496864
    assert "підключено" in bot.sent[0]["text"]


@pytest.mark.asyncio
async def test_link_connection_requires_owner_and_bot_admin_rights(database: Database) -> None:
    connection = await seed_connection(database)
    bot = FakeBot()
    connector = SummaryChannelConnector(database=database, bot=bot)
    async with database.session() as session, session.begin():
        stored = await session.get(models.Connection, connection.id)
        assert stored is not None
        channel = await connector.connect_reference(
            session,
            connection=stored,
            owner_user_id=42,
            reference="https://t.me/c/4449496864/3",
        )
    assert channel.chat_id == -1004449496864

    bot.owner_status = "member"
    async with database.session() as session, session.begin():
        stored = await session.get(models.Connection, connection.id)
        assert stored is not None
        with pytest.raises(SummaryChannelError, match="адміністратор"):
            await connector.connect_reference(
                session,
                connection=stored,
                owner_user_id=42,
                reference="@testsecretarychannel",
            )


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("https://t.me/c/4449496864/3", -1004449496864),
        ("https://t.me/testsecretarychannel/7", "@testsecretarychannel"),
        ("@testsecretarychannel", "@testsecretarychannel"),
        ("-1004449496864", -1004449496864),
    ],
)
def test_parse_channel_reference(reference: str, expected: int | str) -> None:
    assert parse_channel_reference(reference) == expected


def test_invite_link_explains_that_a_post_link_is_required() -> None:
    with pytest.raises(SummaryChannelError, match="посилання на допис"):
        parse_channel_reference("https://t.me/+517TXEIZ3Dg4ZDJi")
