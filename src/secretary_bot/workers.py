from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from secretary_bot.daily_summary import DailySummary
from secretary_bot.delayed import DelayedReplyQueue
from secretary_bot.morning import MorningDigest
from secretary_bot.pipeline import Pipeline
from secretary_bot.storage import Database, delete_expired_messages

logger = logging.getLogger(__name__)

DELAYED_POLL_SECONDS = 1.0
MORNING_POLL_SECONDS = 60.0
SUMMARY_POLL_SECONDS = 60.0
DELIVERY_RETRY_SECONDS = 5
MAX_DELIVERY_ATTEMPTS = 3
RETENTION_CLEANUP_SECONDS = 600.0


async def deliver_due_once(
    pipeline: Pipeline,
    queue: DelayedReplyQueue,
    *,
    now: datetime | None = None,
) -> None:
    """Claim due tasks and return failed deliveries to Redis for a bounded retry."""
    moment = now or datetime.now(UTC)
    tasks = await queue.pop_due(now=moment)
    for index, task in enumerate(tasks):
        try:
            await pipeline.deliver(task, now=moment)
        except asyncio.CancelledError:
            # pop_due claims the whole batch. A shutdown must not lose the current
            # task or the unvisited tail of that batch.
            for pending in tasks[index:]:
                await queue.schedule(pending, due_at=moment)
            raise
        except Exception as exc:
            attempt = task.delivery_attempts + 1
            if attempt < MAX_DELIVERY_ATTEMPTS:
                retry = replace(task, delivery_attempts=attempt)
                await queue.schedule(
                    retry,
                    due_at=moment + timedelta(seconds=DELIVERY_RETRY_SECONDS),
                )
                logger.warning(
                    "delayed reply failed; retry scheduled: %s attempt=%s/%s",
                    type(exc).__name__,
                    attempt,
                    MAX_DELIVERY_ATTEMPTS,
                )
            else:
                logger.error(
                    "delayed reply failed permanently: %s attempts=%s",
                    type(exc).__name__,
                    MAX_DELIVERY_ATTEMPTS,
                )


async def run_delayed_replies(
    pipeline: Pipeline, queue: DelayedReplyQueue, *, interval: float = DELAYED_POLL_SECONDS
) -> None:
    """Deliver replies whose delay has elapsed, including ones left by a restart."""
    while True:
        try:
            await deliver_due_once(pipeline, queue)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("delayed reply worker failed: %s", type(exc).__name__)
        await asyncio.sleep(interval)


async def run_morning_digest(
    digest: MorningDigest, *, interval: float = MORNING_POLL_SECONDS
) -> None:
    while True:
        try:
            await digest.run_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("morning digest worker failed: %s", type(exc).__name__)
        await asyncio.sleep(interval)


async def run_daily_summary(
    summary: DailySummary, *, interval: float = SUMMARY_POLL_SECONDS
) -> None:
    while True:
        try:
            await summary.run_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("daily summary worker failed: %s", type(exc).__name__)
        await asyncio.sleep(interval)


async def cleanup_retention_once(
    database: Database, *, now: datetime | None = None, batch_size: int = 1000
) -> int:
    """Delete expired encrypted bodies and commit one bounded batch."""
    async with database.session() as session, session.begin():
        return await delete_expired_messages(
            session, now=now or datetime.now(UTC), batch_size=batch_size
        )


async def run_retention_cleanup(
    database: Database, *, interval: float = RETENTION_CLEANUP_SECONDS
) -> None:
    while True:
        # Starting with a wait avoids racing the startup transaction and keeps
        # shutdown cancellation independent from an in-flight database session.
        await asyncio.sleep(interval)
        try:
            deleted = await cleanup_retention_once(database)
            if deleted:
                logger.info("expired retained messages deleted: count=%s", deleted)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("retention cleanup worker failed: %s", type(exc).__name__)
