from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from secretary_bot.summary import (
    DialogueMessage,
    RetainedDialogue,
    SummaryError,
    parse_summary,
    render_transcript,
    summarize_dialogue,
)

NOW = datetime(2026, 9, 2, 9, tzinfo=UTC)


class FakeSummaryModel:
    def __init__(self, payload: dict[str, object] | Exception) -> None:
        self.payload = payload
        self.calls: list[dict[str, str]] = []

    async def summarize_dialogue(
        self, transcript: str, *, system_prompt: str, model: str
    ) -> str:
        self.calls.append(
            {"transcript": transcript, "system_prompt": system_prompt, "model": model}
        )
        if isinstance(self.payload, Exception):
            raise self.payload
        return json.dumps(self.payload, ensure_ascii=False)


def dialogue() -> RetainedDialogue:
    return RetainedDialogue(
        contact_id=123,
        contact_name="Секретне Ім’я",
        messages=(
            DialogueMessage("in", NOW, "Коли буде рахунок?", 10),
            DialogueMessage("out", NOW + timedelta(minutes=3), "Надішлю сьогодні.", 11),
            DialogueMessage("in", NOW + timedelta(minutes=5), "А оплата до п’ятниці?", 12),
        ),
    )


@pytest.mark.asyncio
async def test_summary_sends_only_anonymous_transcript_and_parses_answer() -> None:
    model = FakeSummaryModel(
        {
            "topic": "Рахунок та оплата",
            "agreements": ["Рахунок буде надіслано сьогодні"],
            "open_questions": ["Чи буде оплата до п’ятниці?"],
            "questions_asked": 2,
            "questions_closed": 1,
        }
    )

    result = await summarize_dialogue(
        dialogue(), model=model, model_name="summary-model", timeout_seconds=1
    )

    assert result.topic == "Рахунок та оплата"
    assert result.questions_asked == 2
    assert result.questions_closed == 1
    assert result.open_questions == ("Чи буде оплата до п’ятниці?",)
    assert "Секретне Ім’я" not in model.calls[0]["transcript"]
    assert "contact_id" not in model.calls[0]["transcript"]
    assert "IN: Коли буде рахунок?" in model.calls[0]["transcript"]
    assert model.calls[0]["model"] == "summary-model"


def test_answered_question_is_not_open_in_valid_summary() -> None:
    result = parse_summary(
        json.dumps(
            {
                "topic": "Строк рахунку",
                "agreements": ["Рахунок сьогодні"],
                "open_questions": [],
                "questions_asked": 1,
                "questions_closed": 1,
            }
        )
    )

    assert result.open_questions == ()
    assert result.questions_asked == result.questions_closed == 1


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        json.dumps({"topic": "Одне", "agreements": [], "open_questions": []}),
        json.dumps(
            {
                "topic": "Коректна тема",
                "agreements": [],
                "open_questions": [],
                "questions_asked": 1,
                "questions_closed": 2,
            }
        ),
        json.dumps(
            {
                "topic": "Коректна тема",
                "agreements": [],
                "open_questions": ["Перше", "Друге"],
                "questions_asked": 1,
                "questions_closed": 0,
            }
        ),
    ],
)
def test_invalid_summary_is_rejected(payload: str) -> None:
    with pytest.raises(SummaryError):
        parse_summary(payload)


def test_transcript_preserves_chronology_and_directions() -> None:
    transcript = render_transcript(dialogue().messages)

    assert transcript.index("IN: Коли") < transcript.index("OUT: Надішлю")
    assert transcript.index("OUT: Надішлю") < transcript.index("IN: А оплата")
