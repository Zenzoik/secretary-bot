from __future__ import annotations

from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage

from secretary_bot.classifier import Category
from secretary_bot.sender import BusinessReplySender, SendOutcome
from secretary_bot.templates import DEFAULT_TEMPLATES, TemplateCode, render, template_for

METHOD = SendMessage(chat_id=100, text="…")


class Sent:
    message_id = 555


class FakeBot:
    """Raises the queued failures, then succeeds."""

    def __init__(self, *failures: Exception) -> None:
        self.failures = list(failures)
        self.calls: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> Sent:
        self.calls.append(kwargs)
        if self.failures:
            raise self.failures.pop(0)
        return Sent()


def sender(bot: FakeBot) -> tuple[BusinessReplySender, list[float]]:
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    return BusinessReplySender(bot=bot, sleep=sleep), slept


async def send(bot: FakeBot) -> tuple[Any, list[float]]:
    reply_sender, slept = sender(bot)
    result = await reply_sender.send(
        business_connection_id="connection-1", chat_id=100, text="Сейчас нерабочее время"
    )
    return result, slept


def bad_request(description: str) -> TelegramBadRequest:
    return TelegramBadRequest(method=METHOD, message=description)


@pytest.mark.asyncio
async def test_a_reply_goes_out_as_the_owner() -> None:
    bot = FakeBot()

    result, _ = await send(bot)

    assert result.outcome is SendOutcome.SENT
    assert result.message_id == 555
    assert bot.calls == [
        {
            "business_connection_id": "connection-1",
            "chat_id": 100,
            "text": "Сейчас нерабочее время",
        }
    ]


@pytest.mark.asyncio
async def test_closed_business_chat_is_never_retried() -> None:
    bot = FakeBot(bad_request("Bad Request: BUSINESS_CHAT_INACTIVE"))

    result, slept = await send(bot)

    assert result.outcome is SendOutcome.CHAT_INACTIVE
    assert result.error_code == "BUSINESS_CHAT_INACTIVE"
    assert len(bot.calls) == 1
    assert slept == []


@pytest.mark.asyncio
async def test_invalid_connection_is_reported_for_an_alert() -> None:
    bot = FakeBot(bad_request("Forbidden: BUSINESS_CONNECTION_INVALID"))

    result, _ = await send(bot)

    assert result.outcome is SendOutcome.CONNECTION_INVALID
    assert len(bot.calls) == 1


@pytest.mark.asyncio
async def test_flood_wait_retries_after_the_named_delay() -> None:
    bot = FakeBot(TelegramRetryAfter(method=METHOD, message="Too Many Requests", retry_after=17))

    result, slept = await send(bot)

    assert result.outcome is SendOutcome.SENT
    assert result.attempts == 2
    assert slept == [22]


@pytest.mark.asyncio
async def test_flood_wait_gives_up_after_three_attempts() -> None:
    flood = [
        TelegramRetryAfter(method=METHOD, message="Too Many Requests", retry_after=1)
        for _ in range(3)
    ]
    bot = FakeBot(*flood)

    result, slept = await send(bot)

    assert result.outcome is SendOutcome.FAILED
    assert result.error_code == "FLOOD_WAIT"
    assert result.attempts == 3
    assert len(bot.calls) == 3
    assert slept == [6, 6]


@pytest.mark.asyncio
async def test_transient_failures_are_retried_with_backoff() -> None:
    bot = FakeBot(TelegramNetworkError(method=METHOD, message="timeout"))

    result, slept = await send(bot)

    assert result.outcome is SendOutcome.SENT
    assert slept == [2.0]


@pytest.mark.asyncio
async def test_persistent_failure_is_reported_not_raised() -> None:
    bot = FakeBot(*[TelegramNetworkError(method=METHOD, message="timeout") for _ in range(3)])

    result, slept = await send(bot)

    assert result.outcome is SendOutcome.FAILED
    assert result.error_code == "TelegramNetworkError"
    assert slept == [2.0, 4.0]


def test_money_and_general_map_to_their_templates() -> None:
    assert template_for(Category.MONEY) is TemplateCode.MONEY_PRIORITY
    assert template_for(Category.GENERAL) is TemplateCode.OFF_HOURS_DEFAULT


def test_owner_wording_wins_over_the_shipped_text() -> None:
    overrides = {"off_hours_default": "Отвечу утром", "money_priority": "   "}

    assert render(TemplateCode.OFF_HOURS_DEFAULT, overrides=overrides) == "Отвечу утром"
    assert (
        render(TemplateCode.MONEY_PRIORITY, overrides=overrides)
        == (DEFAULT_TEMPLATES[TemplateCode.MONEY_PRIORITY])
    )
    assert (
        render(TemplateCode.OFF_HOURS_DEFAULT)
        == (DEFAULT_TEMPLATES[TemplateCode.OFF_HOURS_DEFAULT])
    )
