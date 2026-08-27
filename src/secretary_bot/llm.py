from __future__ import annotations

from dataclasses import dataclass

from anthropic import AsyncAnthropic

from secretary_bot.classifier import CLASSIFICATION_SCHEMA, MAX_OUTPUT_TOKENS


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

    async def aclose(self) -> None:
        await self.client.close()
