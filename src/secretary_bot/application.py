from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
from collections.abc import AsyncIterator

from aiogram import Bot
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request, status
from pydantic import ValidationError

from secretary_bot.config import Settings
from secretary_bot.ingest import (
    DeduplicationUnavailable,
    Deduplicator,
    IngestQueueFull,
    RedisDeduplicator,
    UpdateIngestor,
)
from secretary_bot.runtime import RuntimeState, TelegramBot, process_updates


def create_app(
    *,
    settings: Settings | None = None,
    bot: TelegramBot | None = None,
    deduplicator: Deduplicator | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    owns_bot = bot is None
    telegram_bot = bot or Bot(token=settings.bot_token)
    owns_deduplicator = deduplicator is None
    update_deduplicator = deduplicator or RedisDeduplicator.from_url(
        settings.redis_url, ttl_seconds=settings.dedup_ttl_seconds
    )
    state = RuntimeState(
        bot=telegram_bot,
        echo_enabled=settings.echo_enabled,
        allowed_chat_ids=settings.allowed_chat_ids,
        queue_size=settings.update_queue_size,
    )
    ingestor = UpdateIngestor(queue=state.queue, deduplicator=update_deduplicator)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(
            level=settings.log_level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        worker = asyncio.create_task(process_updates(state), name="telegram-update-worker")
        try:
            yield
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            if owns_deduplicator:
                await update_deduplicator.aclose()
            if owns_bot:
                await telegram_bot.session.close()  # type: ignore[union-attr]

    app = FastAPI(title="Telegram Secretary Bot PoC", lifespan=lifespan)
    app.state.runtime = state
    app.state.ingestor = ingestor

    @app.get("/healthz")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "echo_enabled": state.echo_enabled,
            "allowed_chat_count": len(state.allowed_chat_ids),
            "queue_depth": state.queue.qsize(),
            "processed_updates": state.processed_updates,
            "accepted_updates": ingestor.accepted_updates,
            "duplicate_updates": ingestor.duplicate_updates,
        }

    @app.post("/telegram/webhook", status_code=status.HTTP_200_OK)
    async def telegram_webhook(
        request: Request,
        telegram_secret: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
    ) -> dict[str, bool]:
        if telegram_secret is None or not hmac.compare_digest(
            telegram_secret, settings.webhook_secret
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid secret")

        try:
            payload = await request.json()
            update = Update.model_validate(payload, context={"bot": telegram_bot})
        except (ValueError, ValidationError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="invalid update"
            ) from exc

        try:
            await ingestor.enqueue(update)
        except (DeduplicationUnavailable, IngestQueueFull) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="ingest unavailable"
            ) from exc

        return {"ok": True}

    return app
