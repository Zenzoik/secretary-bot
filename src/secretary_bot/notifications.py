from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

FEEDBACK_PREFIX = "feedback"
VERDICT_BUTTONS = (("ok", "✅ Норм"), ("wrong", "❌ Не надо было"), ("exclude", "🚫 Исключить"))


@dataclass(frozen=True, slots=True)
class Preview:
    """What the owner sees instead of an answer while dry run is on.

    The contact's own words are absent on purpose: the decision waits out its
    delay in Redis, and message bodies are not stored anywhere (NFR-2).
    """

    log_id: int
    contact_id: int
    contact_name: str | None
    occurred_at: datetime
    category: str
    reply_text: str
    confidence: str | None = None

    def render(self) -> str:
        who = self.contact_name or f"id {self.contact_id}"
        category = self.category
        if self.confidence is not None:
            category = f"{category} ({self.confidence})"
        return (
            f"🌙 {self.occurred_at:%H:%M} · {who}\n"
            f"Категория: {category}\n"
            f"Я бы ответил: «{self.reply_text}»"
        )


class OwnerNotifier(Protocol):
    async def preview(self, chat_id: int, preview: Preview) -> None: ...

    async def alert(self, chat_id: int, text: str) -> None: ...


class MessageBot(Protocol):
    async def send_message(self, **kwargs: Any) -> Any: ...


@dataclass(slots=True)
class TelegramOwnerNotifier:
    """Messages to the owner's private chat with the bot."""

    bot: MessageBot

    async def preview(self, chat_id: int, preview: Preview) -> None:
        await self.bot.send_message(
            chat_id=chat_id,
            text=preview.render(),
            reply_markup=feedback_keyboard(preview.log_id),
        )

    async def alert(self, chat_id: int, text: str) -> None:
        await self.bot.send_message(chat_id=chat_id, text=text)


def feedback_keyboard(log_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=label, callback_data=f"{FEEDBACK_PREFIX}:{log_id}:{code}")
                for code, label in VERDICT_BUTTONS
            ]
        ]
    )


def parse_feedback(callback_data: str) -> tuple[int, str] | None:
    """``feedback:<log_id>:<verdict>`` — anything else is not ours."""
    parts = callback_data.split(":")
    if len(parts) != 3 or parts[0] != FEEDBACK_PREFIX:
        return None
    log_id, verdict = parts[1], parts[2]
    if not log_id.isdigit() or verdict not in {code for code, _ in VERDICT_BUTTONS}:
        return None
    return int(log_id), verdict
