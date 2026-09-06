from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from secretary_bot.classifier import CLASSIFICATION_SCHEMA, MAX_OUTPUT_TOKENS
from secretary_bot.summary import SUMMARY_OUTPUT_TOKENS, SUMMARY_SCHEMA


@dataclass(slots=True)
class AnthropicLanguageModel:
    """Anthropic-backed ``LanguageModel``: one call, schema-constrained JSON."""

    client: AsyncAnthropic

    @classmethod
    def from_api_key(cls, api_key: str, *, timeout_seconds: float) -> AnthropicLanguageModel:
        return cls(client=AsyncAnthropic(api_key=api_key, timeout=timeout_seconds))

    async def classify(self, text: str, *, system_prompt: str, model: str) -> str:
        response = await self.client.messages.create(
            model=model,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": text}],
            output_config={"format": {"type": "json_schema", "schema": CLASSIFICATION_SCHEMA}},
        )
        return next(block.text for block in response.content if block.type == "text")

    async def summarize_dialogue(
        self, transcript: str, *, system_prompt: str, model: str
    ) -> str:
        response = await self.client.messages.create(
            model=model,
            max_tokens=SUMMARY_OUTPUT_TOKENS,
            system=system_prompt,
            messages=[{"role": "user", "content": transcript}],
            output_config={"format": {"type": "json_schema", "schema": SUMMARY_SCHEMA}},
        )
        return next(block.text for block in response.content if block.type == "text")

    async def aclose(self) -> None:
        await self.client.close()


@dataclass(slots=True)
class OpenAILanguageModel:
    """OpenAI Responses API adapter with strict, non-persisted JSON output."""

    client: AsyncOpenAI
    default_model: str = "gpt-5-mini"

    @classmethod
    def from_api_key(
        cls,
        api_key: str,
        *,
        timeout_seconds: float,
        default_model: str = "gpt-5-mini",
    ) -> OpenAILanguageModel:
        return cls(
            client=AsyncOpenAI(api_key=api_key, timeout=timeout_seconds),
            default_model=default_model,
        )

    async def classify(self, text: str, *, system_prompt: str, model: str) -> str:
        return await self._respond(
            text,
            system_prompt=system_prompt,
            model=model,
            schema=CLASSIFICATION_SCHEMA,
            schema_name="message_classification",
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )

    async def summarize_dialogue(
        self, transcript: str, *, system_prompt: str, model: str
    ) -> str:
        return await self._respond(
            transcript,
            system_prompt=system_prompt,
            model=model,
            schema=SUMMARY_SCHEMA,
            schema_name="dialogue_summary",
            max_output_tokens=SUMMARY_OUTPUT_TOKENS,
        )

    async def _respond(
        self,
        prompt: str,
        *,
        system_prompt: str,
        model: str,
        schema: dict[str, Any],
        schema_name: str,
        max_output_tokens: int,
    ) -> str:
        response = await self.client.responses.create(
            model=self._model(model),
            instructions=system_prompt,
            input=prompt,
            max_output_tokens=max_output_tokens,
            text={
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                }
            },
            store=False,
        )
        if not response.output_text:
            details = response.incomplete_details
            raise ValueError(
                "OpenAI response has no output text: "
                f"status={response.status}, reason={details.reason if details else None}"
            )
        return response.output_text

    def _model(self, configured_model: str) -> str:
        if configured_model.startswith("claude-"):
            return self.default_model
        return configured_model

    async def aclose(self) -> None:
        await self.client.close()
