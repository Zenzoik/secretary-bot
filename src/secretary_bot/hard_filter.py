from __future__ import annotations

from enum import StrEnum

from aiogram.types import Message

TELEGRAM_SERVICE_USER_ID = 777000


class HardFilterResult(StrEnum):
    ALLOWED = "allowed"
    BOT_ECHO = "bot_echo"
    BOT_SENDER = "bot_sender"
    SERVICE_SENDER = "service_sender"
    OWNER_MESSAGE = "owner_message"
    NON_PRIVATE = "non_private"
    UNSUPPORTED_SENDER = "unsupported_sender"
    UNSUPPORTED_CONTENT = "unsupported_content"


def apply_hard_filter(message: Message, *, owner_user_id: int) -> HardFilterResult:
    if message.sender_business_bot is not None:
        return HardFilterResult.BOT_ECHO

    sender = message.from_user
    if sender is None:
        return HardFilterResult.UNSUPPORTED_SENDER
    if sender.is_bot:
        return HardFilterResult.BOT_SENDER
    if sender.id == TELEGRAM_SERVICE_USER_ID:
        return HardFilterResult.SERVICE_SENDER
    if sender.id == owner_user_id:
        return HardFilterResult.OWNER_MESSAGE
    if message.chat.type != "private":
        return HardFilterResult.NON_PRIVATE
    if message.text is None:
        return HardFilterResult.UNSUPPORTED_CONTENT
    return HardFilterResult.ALLOWED
