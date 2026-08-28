from __future__ import annotations

import logging
from typing import Any, Protocol

from aiogram.types import CallbackQuery

logger = logging.getLogger(__name__)


class CallbackBot(Protocol):
    async def answer_callback_query(self, callback_query_id: str, **kwargs: Any) -> Any: ...

    async def edit_message_text(self, **kwargs: Any) -> Any: ...

    async def edit_message_reply_markup(self, **kwargs: Any) -> Any: ...


async def finalize_callback(
    bot: CallbackBot,
    query: CallbackQuery,
    *,
    note: str,
    toast: str,
) -> None:
    """Acknowledge an action, remove its buttons and leave a durable result."""
    await bot.answer_callback_query(query.id, text=toast)
    message = query.message
    if message is None:
        return
    try:
        text = getattr(message, "text", None)
        if text:
            await bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=message.message_id,
                text=f"{text.rstrip()}\n\n{note}",
                reply_markup=None,
            )
        else:
            await bot.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=message.message_id,
                reply_markup=None,
            )
    except Exception as exc:
        # The action is already committed and acknowledged. An old/deleted
        # Telegram message must not make the business action look failed.
        logger.warning("could not clear callback keyboard: %s", type(exc).__name__)
