from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from aiogram import Bot
from aiogram.types import Update
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import text as sql_text

from secretary_bot.classifier import ClassifierSettings, LanguageModel
from secretary_bot.config import Settings
from secretary_bot.control import ControlPlane
from secretary_bot.daily_summary import DailySummary
from secretary_bot.delayed import DelayedReplyQueue
from secretary_bot.escalation import EscalationActions
from secretary_bot.ingest import (
    Deduplicator,
    RedisDeduplicator,
    UpdateIngestor,
)
from secretary_bot.llm import AnthropicLanguageModel, OpenAILanguageModel
from secretary_bot.morning import MorningDigest
from secretary_bot.notifications import OwnerNotifier, TelegramOwnerNotifier
from secretary_bot.outbox import run_notifications
from secretary_bot.pipeline import Pipeline
from secretary_bot.retention import MessageCipher
from secretary_bot.runtime import RuntimeState, TelegramBot, handle_update
from secretary_bot.sender import BusinessReplySender
from secretary_bot.storage import Database, ensure_master
from secretary_bot.summary_actions import SummaryActions
from secretary_bot.summary_channel import SummaryChannelConnector
from secretary_bot.web_api import build_web_router
from secretary_bot.workers import (
    run_daily_summary,
    run_delayed_replies,
    run_morning_digest,
    run_retention_cleanup,
)

WEB_ROOT = Path(__file__).parent / "web" / "static"


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

    owns_language_model = model is None
    language_model = model or _language_model(settings)
    message_cipher = (
        None
        if settings.message_encryption_key is None
        else MessageCipher.from_encoded_key(settings.message_encryption_key)
    )
    reply_sender = BusinessReplySender(bot=telegram_bot)
    pipeline = Pipeline(
        database=connection_database,
        queue=replies,
        sender=reply_sender,
        notifier=notifier or TelegramOwnerNotifier(bot=telegram_bot),
        model=language_model,
        classifier_defaults=ClassifierSettings(timeout_seconds=settings.classifier_timeout_seconds),
        message_cipher=message_cipher,
        require_contact_setup=settings.require_contact_setup,
    )
    daily_summary = DailySummary(
        database=connection_database,
        bot=telegram_bot,
        cipher=message_cipher,
        model=language_model,
        classifier_defaults=ClassifierSettings(timeout_seconds=settings.classifier_timeout_seconds),
        summary_timeout_seconds=settings.summary_timeout_seconds,
    )
    summary_channel_connector = SummaryChannelConnector(
        database=connection_database,
        bot=telegram_bot,
    )
    state = RuntimeState(
        bot=telegram_bot,
        pipeline=pipeline,
        control=ControlPlane(
            database=connection_database,
            bot=telegram_bot,
            bot_username=settings.bot_username,
            public_base_url=settings.public_base_url or "",
            delayed_queue=replies,
        ),
        escalation_actions=EscalationActions(
            database=connection_database,
            bot=telegram_bot,
            sender=reply_sender,
        ),
        summary_actions=SummaryActions(
            database=connection_database,
            bot=telegram_bot,
            sender=reply_sender,
            cipher=message_cipher,
        ),
        summary_channel_connector=summary_channel_connector,
        queue_size=settings.update_queue_size,
        allowed_chat_ids=settings.allowed_chat_ids,
    )

    async def process_webhook(update: Update) -> None:
        await handle_update(update, state)
        state.processed_updates += 1

    ingestor = UpdateIngestor(
        queue=state.queue, deduplicator=update_deduplicator, processor=process_webhook
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        logging.basicConfig(
            level=settings.log_level,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        async with connection_database.session() as session, session.begin():
            await ensure_master(session, settings.master_user_id)
        tasks = [
            asyncio.create_task(
                run_notifications(connection_database, reply_sender), name="notification-worker"
            ),
            asyncio.create_task(
                run_morning_digest(
                    MorningDigest(
                        database=connection_database,
                        notifier=notifier or TelegramOwnerNotifier(bot=telegram_bot),
                        summary_available=message_cipher is not None and language_model is not None,
                    )
                ),
                name="morning-digest-worker",
            ),
            asyncio.create_task(
                run_delayed_replies(pipeline, replies), name="delayed-reply-worker"
            ),
            asyncio.create_task(run_daily_summary(daily_summary), name="daily-summary-worker"),
            asyncio.create_task(
                run_retention_cleanup(connection_database), name="retention-cleanup-worker"
            ),
        ]
        app.state.background_tasks = tasks
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            _, pending = await asyncio.wait(tasks, timeout=3)
            for task in pending:
                logging.getLogger(__name__).warning("worker still stopping: %r", task)
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=3)
            for task in tasks:
                # A worker that died with an exception must be visible in the
                # log, but must not prevent the connections below from closing.
                if task.done() and not task.cancelled() and task.exception() is not None:
                    logging.getLogger(__name__).error(
                        "worker %s exited with %s",
                        task.get_name(),
                        type(task.exception()).__name__,
                    )
            if owns_deduplicator:
                await update_deduplicator.aclose()
            if owns_redis and redis is not None:
                await redis.aclose()
            if owns_database:
                await connection_database.aclose()
            if owns_language_model and language_model is not None:
                await language_model.aclose()  # type: ignore[attr-defined]
            if owns_bot:
                await telegram_bot.session.close()  # type: ignore[union-attr]

    app = FastAPI(title="Telegram Secretary Bot", lifespan=lifespan)
    app.state.runtime = state
    app.state.ingestor = ingestor
    app.state.pipeline = pipeline
    app.include_router(
        build_web_router(
            database=connection_database,
            settings=settings,
            summary_channel_connector=summary_channel_connector,
            language_model=language_model,
            bot=telegram_bot,
            delayed_queue=replies,
        )
    )
    app.mount("/assets", StaticFiles(directory=WEB_ROOT), name="web-assets")

    @app.get("/app", include_in_schema=False)
    async def web_app_redirect() -> RedirectResponse:
        return RedirectResponse("/app/", status_code=status.HTTP_308_PERMANENT_REDIRECT)

    @app.get("/app/", response_class=HTMLResponse, include_in_schema=False)
    async def web_app(request: Request) -> HTMLResponse:
        base_url = (settings.public_base_url or str(request.base_url)).rstrip("/")
        html = (WEB_ROOT / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html.replace("{{BASE_URL}}", base_url))

    @app.get("/healthz")
    async def health() -> dict[str, object]:
        tasks = getattr(app.state, "background_tasks", ())
        if any(task.done() for task in tasks):
            raise HTTPException(status_code=503, detail="background worker unavailable")
        return {
            "status": "ok",
            "classifier": "llm" if language_model is not None else "keywords",
            "allowed_chat_count": len(state.allowed_chat_ids),
            "queue_depth": state.queue.qsize(),
            "processed_updates": state.processed_updates,
            "accepted_updates": ingestor.accepted_updates,
            "duplicate_updates": ingestor.duplicate_updates,
        }

    @app.get("/readyz")
    async def ready() -> dict[str, str]:
        await health()
        try:
            async with asyncio.timeout(3):
                async with connection_database.session() as session:
                    await session.execute(sql_text("SELECT 1"))
                if redis is not None:
                    await redis.ping()
        except Exception as exc:
            raise HTTPException(status_code=503, detail="storage unavailable") from exc
        return {"status": "ready"}

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
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="ingest unavailable"
            ) from exc

        return {"ok": True}

    return app


def _language_model(settings: Settings) -> LanguageModel | None:
    """No API key means the keyword dictionary decides — never a crash."""
    provider = settings.llm_provider
    if provider in {"auto", "openai"} and settings.openai_api_key is not None:
        return OpenAILanguageModel.from_api_key(
            settings.openai_api_key,
            timeout_seconds=max(
                settings.classifier_timeout_seconds, settings.summary_timeout_seconds
            ),
            default_model=settings.openai_model,
        )
    if provider in {"auto", "anthropic"} and settings.anthropic_api_key is not None:
        return AnthropicLanguageModel.from_api_key(
            settings.anthropic_api_key,
            timeout_seconds=max(
                settings.classifier_timeout_seconds, settings.summary_timeout_seconds
            ),
        )
    return None
