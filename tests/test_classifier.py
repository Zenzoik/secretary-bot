from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest

from secretary_bot.classifier import (
    Category,
    ClassificationSource,
    ClassifierSettings,
    classify,
    classify_by_keywords,
)

FAST = ClassifierSettings(timeout_seconds=0.05)


class FakeModel:
    def __init__(self, *, answer: str = "", error: Exception | None = None, delay: float = 0.0):
        self.answer = answer
        self.error = error
        self.delay = delay
        self.calls: list[dict[str, str]] = []

    async def classify(self, text: str, *, system_prompt: str, model: str) -> str:
        self.calls.append({"text": text, "system_prompt": system_prompt, "model": model})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.answer


def answer(category: str, confidence: float, reason: str = "по тексту") -> str:
    return json.dumps({"category": category, "confidence": confidence, "reason": reason})


@pytest.mark.asyncio
async def test_money_above_threshold_is_money() -> None:
    result = await classify("когда оплата?", model=FakeModel(answer=answer("money", 0.91)))

    assert result.category is Category.MONEY
    assert result.source is ClassificationSource.LLM
    assert result.confidence == Decimal("0.91")
    assert result.is_money


@pytest.mark.asyncio
async def test_confidence_below_threshold_falls_back_to_general() -> None:
    result = await classify("когда оплата?", model=FakeModel(answer=answer("money", 0.55)))

    assert result.category is Category.GENERAL
    assert result.confidence == Decimal("0.55")
    assert "threshold" in result.reason


@pytest.mark.asyncio
async def test_threshold_is_configurable_per_connection() -> None:
    model = FakeModel(answer=answer("money", 0.55))

    result = await classify(
        "когда оплата?",
        model=model,
        settings=ClassifierSettings(confidence_min=Decimal("0.50")),
    )

    assert result.category is Category.MONEY


@pytest.mark.asyncio
async def test_only_the_message_text_reaches_the_model() -> None:
    model = FakeModel(answer=answer("general", 0.99))
    settings = ClassifierSettings(system_prompt="prompt", model="claude-sonnet-4-6")

    await classify("привет", model=model, settings=settings)

    assert model.calls == [
        {"text": "привет", "system_prompt": "prompt", "model": "claude-sonnet-4-6"}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["", "not json", "[]", '{"category": "money"', "null"])
async def test_unparsable_answers_become_general(raw: str) -> None:
    result = await classify("когда оплата?", model=FakeModel(answer=raw))

    assert result.category is Category.GENERAL
    assert result.source is ClassificationSource.LLM
    assert result.confidence is None


@pytest.mark.asyncio
async def test_money_without_confidence_is_not_trusted() -> None:
    raw = json.dumps({"category": "money", "reason": "нет уверенности"})

    result = await classify("когда оплата?", model=FakeModel(answer=raw))

    assert result.category is Category.GENERAL
    assert result.confidence is None


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, "high", 1.5, -0.1, None, [0.9]])
async def test_confidence_that_is_not_a_probability_is_dropped(value: object) -> None:
    raw = json.dumps({"category": "general", "confidence": value, "reason": "x"})

    result = await classify("привет", model=FakeModel(answer=raw))

    assert result.confidence is None


@pytest.mark.asyncio
async def test_unknown_category_is_treated_as_general() -> None:
    raw = json.dumps({"category": "urgent", "confidence": 0.99, "reason": "x"})

    result = await classify("когда оплата?", model=FakeModel(answer=raw))

    assert result.category is Category.GENERAL


@pytest.mark.asyncio
async def test_llm_error_falls_back_to_the_keyword_dictionary() -> None:
    model = FakeModel(error=RuntimeError("api down"))

    money = await classify("скинь реквизиты для оплаты", model=model, settings=FAST)
    general = await classify("привет, как дела", model=model, settings=FAST)

    assert money.category is Category.MONEY
    assert money.source is ClassificationSource.KEYWORDS
    assert money.confidence is None
    assert general.category is Category.GENERAL


@pytest.mark.asyncio
async def test_slow_llm_times_out_into_the_keyword_dictionary() -> None:
    model = FakeModel(answer=answer("general", 0.99), delay=1.0)

    result = await classify("нужен инвойс", model=model, settings=FAST)

    assert result.category is Category.MONEY
    assert result.source is ClassificationSource.KEYWORDS


@pytest.mark.asyncio
async def test_without_a_model_the_dictionary_decides() -> None:
    result = await classify("аванс переведу завтра", model=None)

    assert result.source is ClassificationSource.KEYWORDS
    assert result.category is Category.MONEY


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed() -> None:
    model = FakeModel(error=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await classify("привет", model=model)


@pytest.mark.parametrize(
    "text",
    [
        "Когда оплата пройдёт?",
        "скинь счёт",
        "потрібен рахунок",
        "жду платіж",
        "нужны реквизиты",
        "это аванс за январь",
        "долг закрою в пятницу",
        "переведу 500 гривен",
        "Invoice attached",
        "payment failed",
    ],
)
def test_dictionary_recalls_money_wording(text: str) -> None:
    assert classify_by_keywords(text, reason="test").category is Category.MONEY


@pytest.mark.parametrize(
    "text",
    ["Привет, как дела?", "недолго осталось ждать", "встретимся завтра в 10", "спасибо!"],
)
def test_dictionary_leaves_ordinary_messages_alone(text: str) -> None:
    assert classify_by_keywords(text, reason="test").category is Category.GENERAL
