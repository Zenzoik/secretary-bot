from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from secretary_bot.classifier import CLASSIFICATION_SCHEMA
from secretary_bot.llm import OpenAILanguageModel
from secretary_bot.summary import SUMMARY_SCHEMA


@dataclass
class FakeResponses:
    output_text: str
    status: str = "completed"
    incomplete_details: SimpleNamespace | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_text=self.output_text,
            status=self.status,
            incomplete_details=self.incomplete_details,
        )


@dataclass
class FakeClient:
    responses: FakeResponses


@pytest.mark.asyncio
async def test_openai_classifier_uses_responses_structured_output_without_storage() -> None:
    responses = FakeResponses('{"category":"general","confidence":0.9,"reason":"ok"}')
    model = OpenAILanguageModel(client=FakeClient(responses), default_model="gpt-5-mini")  # type: ignore[arg-type]

    result = await model.classify("hello", system_prompt="classify", model="claude-sonnet-4-6")

    assert result == responses.output_text
    assert responses.calls == [
        {
            "model": "gpt-5-mini",
            "instructions": "classify",
            "input": "hello",
            "max_output_tokens": 256,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "message_classification",
                    "strict": True,
                    "schema": CLASSIFICATION_SCHEMA,
                }
            },
            "store": False,
        }
    ]


@pytest.mark.asyncio
async def test_openai_summary_preserves_an_openai_model_name() -> None:
    responses = FakeResponses(
        '{"topic":"тестова тема","agreements":[],"open_questions":[],'
        '"questions_asked":0,"questions_closed":0}'
    )
    model = OpenAILanguageModel(client=FakeClient(responses), default_model="gpt-5-mini")  # type: ignore[arg-type]

    result = await model.summarize_dialogue(
        "IN: hello", system_prompt="summarize", model="gpt-5-mini-custom"
    )

    assert result == responses.output_text
    call = responses.calls[0]
    assert call["model"] == "gpt-5-mini-custom"
    assert call["text"]["format"]["schema"] == SUMMARY_SCHEMA
    assert call["text"]["format"]["name"] == "dialogue_summary"
    assert call["store"] is False


@pytest.mark.asyncio
async def test_openai_empty_output_is_an_error() -> None:
    model = OpenAILanguageModel(
        client=FakeClient(FakeResponses("")),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="no output text"):
        await model.classify("hello", system_prompt="classify", model="gpt-5-mini")


@pytest.mark.asyncio
async def test_openai_exhausted_output_budget_names_the_reason() -> None:
    """A reasoning model can burn the whole ceiling and return no answer."""
    responses = FakeResponses(
        "",
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
    )
    model = OpenAILanguageModel(client=FakeClient(responses))  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="max_output_tokens"):
        await model.summarize_dialogue(
            "IN: hello", system_prompt="summarize", model="gpt-5-mini"
        )
