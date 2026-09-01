from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_CONFIDENCE_MIN = Decimal("0.70")
DEFAULT_TIMEOUT_SECONDS = 8.0
MAX_OUTPUT_TOKENS = 256

DEFAULT_SYSTEM_PROMPT = """\
Ты классифицируешь одно входящее сообщение из личной переписки.

Категории:
- money — деньги: оплата, счёт, инвойс, реквизиты, аванс, долг, сроки платежа.
- general — всё остальное.

Отвечай строгим JSON: category, confidence (0.0–1.0), reason — короткое
пояснение для лога на русском, без цитат из сообщения.
Сомневаешься — ставь general и низкую уверенность."""

CLASSIFICATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": ["money", "general"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
    "required": ["category", "confidence", "reason"],
    "additionalProperties": False,
}

# §6.3: the fallback dictionary. Stems, so declensions and plurals match too.
MONEY_KEYWORDS: tuple[str, ...] = (
    "счет",
    "счёт",
    "рахунок",
    "оплат",
    "платеж",
    "платіж",
    "инвойс",
    "invoice",
    "payment",
    "реквизит",
    "реквізит",
    "аванс",
    "предоплат",
    "передоплат",
    "долг",
    "борг",
    "гривн",
    "гривен",
    "євро",
    "евро",
    "доллар",
    "долар",
)
_MONEY_PATTERN = re.compile(rf"\b(?:{'|'.join(MONEY_KEYWORDS)})", re.IGNORECASE)


class Category(StrEnum):
    MONEY = "money"
    GENERAL = "general"


class ClassificationSource(StrEnum):
    LLM = "llm"
    KEYWORDS = "keywords"


@dataclass(frozen=True, slots=True)
class Classification:
    category: Category
    source: ClassificationSource
    reason: str
    # None for keyword matches: a dictionary hit is not a probability.
    confidence: Decimal | None = None

    @property
    def is_money(self) -> bool:
        return self.category is Category.MONEY


@dataclass(frozen=True, slots=True)
class ClassifierSettings:
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    model: str = DEFAULT_MODEL
    confidence_min: Decimal = DEFAULT_CONFIDENCE_MIN
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    money_keywords: tuple[str, ...] = MONEY_KEYWORDS
    money_enabled: bool = True


class LanguageModel(Protocol):
    """Returns the raw JSON body produced for one message."""

    async def classify(self, text: str, *, system_prompt: str, model: str) -> str: ...


async def classify(
    text: str,
    *,
    model: LanguageModel | None = None,
    settings: ClassifierSettings | None = None,
) -> Classification:
    """Classify one message per §6.3.

    Only the message itself ever reaches the model — no history, no names
    (NFR-3). An unreachable model falls back to the keyword dictionary; a
    reachable model that answers with garbage yields ``general``, because a
    wrong money promise costs more than a missed one.
    """
    settings = settings or ClassifierSettings()
    if model is None:
        return classify_by_keywords(
            text,
            reason="llm disabled",
            money_keywords=settings.money_keywords,
            money_enabled=settings.money_enabled,
        )

    try:
        raw = await asyncio.wait_for(
            model.classify(text, system_prompt=settings.system_prompt, model=settings.model),
            timeout=settings.timeout_seconds,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # timeout, transport failure, bad credentials — all mean silence
        logger.warning("classifier fell back to keywords: %s", type(exc).__name__)
        return classify_by_keywords(
            text,
            reason=f"llm unavailable: {type(exc).__name__}",
            money_keywords=settings.money_keywords,
            money_enabled=settings.money_enabled,
        )

    return _interpret(
        raw,
        confidence_min=settings.confidence_min,
        money_enabled=settings.money_enabled,
    )


def classify_by_keywords(
    text: str,
    *,
    reason: str,
    money_keywords: tuple[str, ...] = MONEY_KEYWORDS,
    money_enabled: bool = True,
) -> Classification:
    pattern = (
        _MONEY_PATTERN
        if money_keywords == MONEY_KEYWORDS
        else re.compile(rf"\b(?:{'|'.join(map(re.escape, money_keywords))})", re.IGNORECASE)
        if money_keywords
        else None
    )
    if money_enabled and pattern is not None and pattern.search(text):
        return Classification(
            category=Category.MONEY,
            source=ClassificationSource.KEYWORDS,
            reason=f"{reason}; money keyword matched",
        )
    return Classification(
        category=Category.GENERAL,
        source=ClassificationSource.KEYWORDS,
        reason=f"{reason}; no money keyword",
    )


def _interpret(raw: str, *, confidence_min: Decimal, money_enabled: bool = True) -> Classification:
    payload = _load(raw)
    if payload is None:
        return Classification(
            category=Category.GENERAL,
            source=ClassificationSource.LLM,
            reason="unparsable llm response",
        )

    confidence = _confidence(payload.get("confidence"))
    reason = str(payload.get("reason", ""))[:200]
    if payload.get("category") != Category.MONEY.value or not money_enabled:
        return Classification(Category.GENERAL, ClassificationSource.LLM, reason, confidence)
    if confidence is None:
        return Classification(
            Category.GENERAL, ClassificationSource.LLM, f"{reason}; confidence missing"
        )
    if confidence < confidence_min:
        return Classification(
            Category.GENERAL,
            ClassificationSource.LLM,
            f"{reason}; below confidence threshold",
            confidence,
        )
    return Classification(Category.MONEY, ClassificationSource.LLM, reason, confidence)


def _load(raw: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _confidence(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        confidence = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    if not Decimal(0) <= confidence <= Decimal(1):
        return None
    return confidence
