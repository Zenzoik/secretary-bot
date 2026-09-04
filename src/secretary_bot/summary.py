from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

SUMMARY_OUTPUT_TOKENS = 1024
MAX_DIALOGUE_MESSAGES = 200
MAX_TRANSCRIPT_CHARS = 60_000

SUMMARY_SYSTEM_PROMPT = """\
Ти створюєш точне добове самарі одного приватного діалогу для власника акаунта.
У транскрипті IN — повідомлення контакту, OUT — відповідь власника або секретаря.

Правила:
- topic: конкретна тема українською у 2–3 словах;
- agreements: лише явно погоджені факти, строки або наступні дії;
- open_questions: лише запитання або прохання IN, на які немає змістовної пізнішої OUT-відповіді;
- якщо не впевнений у домовленості чи закритті питання — не вигадуй;
- questions_asked: кількість змістовних запитань або прохань IN;
- questions_closed: скільки з них отримали змістовну OUT-відповідь;
- не включай привітання, підписи секретаря та службові фрази.

Відповідай лише JSON за наданою схемою. Не додавай імена або ідентифікатори.
"""

SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "topic": {"type": "string"},
        "agreements": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
        "questions_asked": {"type": "integer", "minimum": 0},
        "questions_closed": {"type": "integer", "minimum": 0},
    },
    "required": [
        "topic",
        "agreements",
        "open_questions",
        "questions_asked",
        "questions_closed",
    ],
    "additionalProperties": False,
}


class SummaryError(RuntimeError):
    """The summary could not be generated or safely interpreted."""


class SummaryLanguageModel(Protocol):
    async def summarize_dialogue(
        self, transcript: str, *, system_prompt: str, model: str
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class DialogueMessage:
    direction: str
    occurred_at: datetime
    text: str
    tg_message_id: int | None = None


@dataclass(frozen=True, slots=True)
class RetainedDialogue:
    contact_id: int
    contact_name: str | None
    messages: tuple[DialogueMessage, ...]
    contact_username: str | None = None

    @property
    def last_incoming_message_id(self) -> int | None:
        ids = [
            message.tg_message_id
            for message in self.messages
            if message.direction == "in" and message.tg_message_id is not None
        ]
        return ids[-1] if ids else None


@dataclass(frozen=True, slots=True)
class DialogueSummary:
    topic: str
    agreements: tuple[str, ...]
    open_questions: tuple[str, ...]
    questions_asked: int
    questions_closed: int


async def summarize_dialogue(
    dialogue: RetainedDialogue,
    *,
    model: SummaryLanguageModel,
    model_name: str,
    timeout_seconds: float,
) -> DialogueSummary:
    transcript = render_transcript(dialogue.messages)
    try:
        raw = await asyncio.wait_for(
            model.summarize_dialogue(
                transcript,
                system_prompt=SUMMARY_SYSTEM_PROMPT,
                model=model_name,
            ),
            timeout=timeout_seconds,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise SummaryError(f"summary model unavailable: {type(exc).__name__}") from exc
    return parse_summary(raw)


def render_transcript(messages: tuple[DialogueMessage, ...]) -> str:
    selected = messages[-MAX_DIALOGUE_MESSAGES:]
    lines = [
        f"[{message.occurred_at.isoformat()}] "
        f"{'IN' if message.direction == 'in' else 'OUT'}: {message.text}"
        for message in selected
    ]
    transcript = "\n".join(lines)
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        transcript = transcript[-MAX_TRANSCRIPT_CHARS:]
        transcript = transcript.partition("\n")[2]
    if not transcript:
        raise SummaryError("dialogue has no messages")
    return transcript


def parse_summary(raw: str) -> DialogueSummary:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise SummaryError("summary response is not JSON") from exc
    if not isinstance(payload, dict):
        raise SummaryError("summary response must be an object")

    topic = _text(payload.get("topic"), max_length=100)
    if len(topic.split()) not in {2, 3}:
        raise SummaryError("summary topic must contain 2-3 words")
    agreements = _text_list(payload.get("agreements"), max_items=20)
    open_questions = _text_list(payload.get("open_questions"), max_items=20)
    asked = _counter(payload.get("questions_asked"), "questions_asked")
    closed = _counter(payload.get("questions_closed"), "questions_closed")
    if closed > asked:
        raise SummaryError("closed questions cannot exceed asked questions")
    if len(open_questions) > asked - closed:
        raise SummaryError("open question list conflicts with counters")
    return DialogueSummary(topic, agreements, open_questions, asked, closed)


def _text(value: object, *, max_length: int = 500) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SummaryError("summary text field is empty")
    return " ".join(value.split())[:max_length]


def _text_list(value: object, *, max_items: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > max_items:
        raise SummaryError("summary list field is invalid")
    result: list[str] = []
    for item in value:
        text = _text(item)
        if text not in result:
            result.append(text)
    return tuple(result)


def _counter(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SummaryError(f"{field} must be a non-negative integer")
    return value
