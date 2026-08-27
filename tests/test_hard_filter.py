from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from aiogram.types import Message

from secretary_bot.hard_filter import HardFilterResult, apply_hard_filter

BASE_MESSAGE: dict[str, Any] = {
    "message_id": 10,
    "date": 1_700_000_001,
    "business_connection_id": "connection-1",
    "chat": {"id": 100, "type": "private", "first_name": "Contact"},
    "from": {"id": 100, "is_bot": False, "first_name": "Contact"},
    "text": "hello",
}


def message_with(**changes: Any) -> Message:
    payload = deepcopy(BASE_MESSAGE)
    for key, value in changes.items():
        if key == "from_user":
            payload["from"] = value
        elif value is None:
            payload.pop(key, None)
        else:
            payload[key] = value
    return Message.model_validate(payload)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            message_with(sender_business_bot={"id": 900, "is_bot": True, "first_name": "Bot"}),
            HardFilterResult.BOT_ECHO,
        ),
        (
            message_with(from_user={"id": 900, "is_bot": True, "first_name": "Bot"}),
            HardFilterResult.BOT_SENDER,
        ),
        (
            message_with(
                from_user={
                    "id": 777000,
                    "is_bot": False,
                    "first_name": "Telegram",
                }
            ),
            HardFilterResult.SERVICE_SENDER,
        ),
        (
            message_with(from_user={"id": 42, "is_bot": False, "first_name": "Owner"}),
            HardFilterResult.OWNER_MESSAGE,
        ),
        (
            message_with(chat={"id": -100, "type": "group", "title": "Group"}),
            HardFilterResult.NON_PRIVATE,
        ),
        (message_with(from_user=None), HardFilterResult.UNSUPPORTED_SENDER),
        (message_with(text=None), HardFilterResult.UNSUPPORTED_CONTENT),
        (message_with(), HardFilterResult.ALLOWED),
    ],
)
def test_hard_filter_covers_every_outcome(message: Message, expected: HardFilterResult) -> None:
    assert apply_hard_filter(message, owner_user_id=42) is expected


def test_bot_filter_takes_priority_over_owner_identity() -> None:
    message = message_with(from_user={"id": 42, "is_bot": True, "first_name": "Owner Bot"})

    assert apply_hard_filter(message, owner_user_id=42) is HardFilterResult.BOT_SENDER
