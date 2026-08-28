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
from redis.asyncio import Redis

from secretary_bot.classifier import ClassifierSettings, LanguageModel
from secretary_bot.config import Settings
from secretary_bot.control import ControlPlane
from secretary_bot.delayed import DelayedReplyQueue
from secretary_bot.ingest import (
    DeduplicationUnavailable,
    Deduplicator,
    IngestQueueFull,
    RedisDeduplicator,
    UpdateIngestor,
)
from secretary_bot.llm import AnthropicLanguageModel
from secretary_bot.morning import MorningDigest
from secretary_bot.notifications import OwnerNotifier, TelegramOwnerNotifier
from secretary_bot.pipeline import Pipeline
from secretary_bot.runtime import RuntimeState, TelegramBot, process_updates
from secretary_bot.sender import BusinessReplySender
from secretary_bot.storage import Database
from secretary_bot.workers import run_delayed_replies, run_morning_digest


def create_app(
    *,
    settings: Settings | None = None,
    bot: TelegramBot | None = None,
    deduplicator: Deduplicator | None = None,
    database: Database | None = None,
    delayed_queue: DelayedReplyQueue | None = None,
    notifier: OwnerNotifier | None = None,
    model: LanguageModel | None = None,
) -> FastAPI:
    settings = settings or Settings.from_env()
    owns_bot = bot is None
    telegram_bot = bot or Bot(token=settings.bot_token)

    owns_deduplicator = deduplicator is None
    update_deduplicator = deduplicator or RedisDeduplicator.from_url(
        settings.redis_url, ttl_seconds=settings.dedup_ttl_seconds
    )
    owns_database = database is None
    connection_database = database or Database.from_url(settings.database_url)
    owns_redis = delayed_queue is None
    redis = Redis.from_url(settings.redis_url, decode_responses=True) if owns_redis else None
    replies = delayed_queue or DelayedReplyQueue(client=redis)  # type: ignore[arg-type]

    language_model = model or _language_model(settings)
    pipeline = Pipeline(
        database=connection_database,
        queue=replies,
        sender=BusinessReplySender(bot=telegram_bot),
        notifier=notifier or TelegramOwnerNotifier(bot=telegram_bot),
        model=language_model,
        classifier_defaults=ClassifierSettings(timeout_seconds=settings.classifier_timeout_seconds),
    )
    digest = MorningDigest(
        database=connection_database, notifier=notifier or TelegramOwnerNotifier(bot=telegram_bot)
    )
    state = RuntimeState(
        bot=telegram_bot,
        pipeline=pipeline,
        control=ControlPlane(database=connection_database, bot=telegram_bot),
        queue_size=settings.update_queue_size,
        allowed_chat_ids=settings.allowed_chat_ids,
    )
    ingestor = UpdateIngestor(queue=state.queue, deduplicator=update_deduplicator)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(
            level=settings.log_level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        tasks = [
            asyncio.create_task(process_updates(state), name="telegram-update-worker"),
            asyncio.create_task(
                run_delayed_replies(pipeline, replies), name="delayed-reply-worker"
            ),
            asyncio.create_task(run_morning_digest(digest), name="morning-digest-worker"),
        ]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if owns_deduplicator:
                await update_deduplicator.aclose()
            if owns_redis and redis is not None:
                await redis.aclose()
            if owns_database:
                await connection_database.aclose()
            if owns_bot:
                await telegram_bot.session.close()  # type: ignore[union-attr]

    app = FastAPI(title="Telegram Secretary Bot", lifespan=lifespan)
    app.state.runtime = state
    app.state.ingestor = ingestor
    app.state.pipeline = pipeline

    @app.get("/healthz")
    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "classifier": "llm" if language_model is not None else "keywords",
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


def _language_model(settings: Settings) -> LanguageModel | None:
    """No API key means the keyword dictionary decides — never a crash."""
    if settings.anthropic_api_key is None:
        return None
    return AnthropicLanguageModel.from_api_key(
        settings.anthropic_api_key, timeout_seconds=settings.classifier_timeout_seconds
    )
