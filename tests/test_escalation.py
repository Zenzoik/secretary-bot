from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from aiogram.types import CallbackQuery

from secretary_bot import models
from secretary_bot.escalation import EscalationActions, parse_escalation_callback
from secretary_bot.sender import BusinessReplySender
from secretary_bot.storage import ConnectionSnapshot, ensure_master, upsert_connection

NOW = datetime(2026, 9, 4, 18, tzinfo=UTC)
CONTACT_ID = 100


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.answered: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.callback_error = False

    async def send_message(self, **kwargs: Any) -> Any:
        self.sent.append(kwargs)
        return type("Sent", (), {"message_id": 1000 + len(self.sent)})()

    async def read_business_message(self, **kwargs: Any) -> bool:
        return True

    async def answer_callback_query(self, callback_query_id: str, **kwargs: Any) -> bool:
        if self.callback_error:
            raise RuntimeError("callback expired")
        self.answered.append({"id": callback_query_id, **kwargs})
        return True

    async def edit_message_reply_markup(self, **kwargs: Any) -> bool:
        self.edited.append(kwargs)
        return True

    async def edit_message_text(self, **kwargs: Any) -> bool:
        self.edited.append(kwargs)
        return True

    async def get_chat(self, chat_id: int | str) -> Any:
        return type("Chat", (), {"id": chat_id, "username": "client_name"})()


def callback(action: str, request_id: int, *, user_id: int = CONTACT_ID) -> CallbackQuery:
    return CallbackQuery.model_validate(
        {
            "id": f"callback-{action}-{request_id}",
            "from": {"id": user_id, "is_bot": False, "first_name": "Client"},
            "chat_instance": "instance",
            "data": f"escalation:{action}:{request_id}",
            "message": {
                "message_id": 50,
                "date": NOW,
                "business_connection_id": "connection-1",
                "chat": {"id": user_id, "type": "private", "first_name": "Client"},
                "text": "Автовідповідь",
            },
        }
    )


async def seed_request(database, *, expires_at: datetime | None = None) -> int:
    async with database.session() as session, session.begin():
        await ensure_master(session, 42)
        connection = await upsert_connection(
            session,
            ConnectionSnapshot(
                "connection-1",
                owner_user_id=42,
                owner_chat_id=42,
                rights={"can_reply": True},
            ),
        )
        row = await session.get(models.Connection, connection.id)
        assert row is not None
        row.escalation_enabled = True
        row.escalation_price_amount = Decimal("250.00")
        row.escalation_currency = "UAH"
        session.add(
            models.ContactActivity(
                connection_id=connection.id,
                contact_id=CONTACT_ID,
                contact_name="Клієнт",
                contact_username="client_name",
                off_hours_request_count=1,
            )
        )
        request = models.ContactRequest(
            connection_id=connection.id,
            contact_id=CONTACT_ID,
            tg_message_id=7,
            category="general",
            occurred_at=NOW - timedelta(minutes=1),
            offer_expires_at=expires_at or NOW + timedelta(hours=1),
        )
        session.add(request)
        await session.flush()
        return request.id


@pytest.mark.asyncio
async def test_explicit_confirmation_counts_paid_request_once(database) -> None:
    request_id = await seed_request(database)
    bot = FakeBot()
    actions = EscalationActions(database, bot, BusinessReplySender(bot))

    assert await actions.handle_callback(callback("offer", request_id), now=NOW)
    async with database.session() as session:
        request = await session.get(models.ContactRequest, request_id)
        activity = await session.get(models.ContactActivity, (1, CONTACT_ID))
        assert request is not None and request.status == "offered"
        assert activity is not None and activity.paid_escalation_count == 0

    assert await actions.handle_callback(callback("confirm", request_id), now=NOW)
    assert await actions.handle_callback(callback("confirm", request_id), now=NOW)

    async with database.session() as session:
        request = await session.get(models.ContactRequest, request_id)
        activity = await session.get(models.ContactActivity, (1, CONTACT_ID))
    assert request is not None and request.status == "paid"
    assert request.price_amount == Decimal("250.00")
    assert activity is not None and activity.paid_escalation_count == 1
    owner_messages = [message for message in bot.sent if message.get("chat_id") == 42]
    assert len(owner_messages) == 1
    assert "250 UAH" in owner_messages[0]["text"]


@pytest.mark.asyncio
async def test_expired_offer_never_becomes_paid(database) -> None:
    request_id = await seed_request(database, expires_at=NOW - timedelta(seconds=1))
    bot = FakeBot()
    actions = EscalationActions(database, bot, BusinessReplySender(bot))

    assert await actions.handle_callback(callback("offer", request_id), now=NOW)

    async with database.session() as session:
        request = await session.get(models.ContactRequest, request_id)
        activity = await session.get(models.ContactActivity, (1, CONTACT_ID))
    assert request is not None and request.status == "normal"
    assert activity is not None and activity.paid_escalation_count == 0
    assert bot.sent == []


@pytest.mark.asyncio
async def test_owner_decline_keeps_paid_count_and_sends_configured_text(database) -> None:
    request_id = await seed_request(database)
    bot = FakeBot()
    actions = EscalationActions(database, bot, BusinessReplySender(bot))
    await actions.handle_callback(callback("offer", request_id), now=NOW)
    await actions.handle_callback(callback("confirm", request_id), now=NOW)
    bot.callback_error = True

    assert await actions.handle_callback(
        callback("decline", request_id, user_id=42), now=NOW
    )

    async with database.session() as session:
        request = await session.get(models.ContactRequest, request_id)
        activity = await session.get(models.ContactActivity, (1, CONTACT_ID))
    assert request is not None and request.status == "paid"
    assert request.owner_decision == "declined"
    assert activity is not None and activity.paid_escalation_count == 1
    assert "немає можливості відповісти терміново" in bot.sent[-1]["text"]
    assert bot.edited[-1]["reply_markup"] is None


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("escalation:offer:7", ("offer", 7)),
        ("escalation:confirm:8", ("confirm", 8)),
        ("escalation:decline:9", ("decline", 9)),
        ("escalation:confirm:0", None),
        ("other:confirm:1", None),
    ],
)
def test_callback_parser(data: str, expected: tuple[str, int] | None) -> None:
    assert parse_escalation_callback(data) == expected
