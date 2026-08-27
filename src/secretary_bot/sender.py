from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
FLOOD_WAIT_MARGIN_SECONDS = 5
BACKOFF_BASE_SECONDS = 2.0

CHAT_INACTIVE = "BUSINESS_CHAT_INACTIVE"
CONNECTION_INVALID = "BUSINESS_CONNECTION_INVALID"


class SendOutcome(StrEnum):
    SENT = "sent"
    CHAT_INACTIVE = "chat_inactive"
    CONNECTION_INVALID = "connection_invalid"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SendResult:
    outcome: SendOutcome
    message_id: int | None = None
    error_code: str | None = None
    attempts: int = 1

    @property
    def is_sent(self) -> bool:
        return self.outcome is SendOutcome.SENT


class MessageSender(Protocol):
    async def send_message(self, **kwargs: Any) -> Any: ...


@dataclass(slots=True)
class BusinessReplySender:
    """§6.5: send once, and treat each Telegram failure on its own terms."""

    bot: MessageSender
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    max_attempts: int = MAX_ATTEMPTS

    async def send(self, *, business_connection_id: str, chat_id: int, text: str) -> SendResult:
        for attempt in range(1, self.max_attempts + 1):
            try:
                sent = await self.bot.send_message(
                    business_connection_id=business_connection_id,
                    chat_id=chat_id,
                    text=text,
                )
            except TelegramRetryAfter as exc:
                # Flood control: wait out the window Telegram named, then retry.
                if attempt == self.max_attempts:
                    return SendResult(SendOutcome.FAILED, error_code="FLOOD_WAIT", attempts=attempt)
                await self.sleep(exc.retry_after + FLOOD_WAIT_MARGIN_SECONDS)
            except TelegramAPIError as exc:
                error_code = _error_code(exc)
                if error_code == CHAT_INACTIVE:
                    # The 24-hour window closed. Retrying cannot reopen it.
                    return SendResult(
                        SendOutcome.CHAT_INACTIVE, error_code=error_code, attempts=attempt
                    )
                if error_code == CONNECTION_INVALID:
                    return SendResult(
                        SendOutcome.CONNECTION_INVALID, error_code=error_code, attempts=attempt
                    )
                if attempt == self.max_attempts:
                    return SendResult(SendOutcome.FAILED, error_code=error_code, attempts=attempt)
                await self.sleep(BACKOFF_BASE_SECONDS**attempt)
            else:
                return SendResult(
                    SendOutcome.SENT,
                    message_id=getattr(sent, "message_id", None),
                    attempts=attempt,
                )
        raise AssertionError("unreachable: every attempt either returns or retries")


def _error_code(error: TelegramAPIError) -> str:
    message = str(error).upper()
    for known in (CHAT_INACTIVE, CONNECTION_INVALID):
        if known in message:
            return known
    return type(error).__name__
