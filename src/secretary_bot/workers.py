from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from secretary_bot import models
from secretary_bot.daily_summary import DailySummary
from secretary_bot.delayed import DelayedReplyQueue, ReplyTask
from secretary_bot.morning import MorningDigest
from secretary_bot.pipeline import Pipeline
from secretary_bot.storage import Database, delete_expired_messages

logger = logging.getLogger(__name__)

DELAYED_POLL_SECONDS = 1.0
MORNING_POLL_SECONDS = 60.0
SUMMARY_POLL_SECONDS = 60.0
DELIVERY_RETRY_SECONDS = 5
MAX_DELIVERY_ATTEMPTS = 3
RECONCILE_SECONDS = 30.0
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
            await pipeline.deliver(task, now=now)
            if isinstance(pipeline, Pipeline):
                async with pipeline.database.session() as session, session.begin():
                    job = await session.get(models.ReplyJob, reply_job_key(task))
                    if job is not None:
                        job.completed_at = datetime.now(UTC)
            await queue.acknowledge(task)
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
                await queue.acknowledge(task)
                logger.warning(
                    "delayed reply failed; retry scheduled: %s attempt=%s/%s",
                    type(exc).__name__,
                    attempt,
                    MAX_DELIVERY_ATTEMPTS,
                )
            else:
                if isinstance(pipeline, Pipeline):
                    async with pipeline.database.session() as session, session.begin():
                        job = await session.get(models.ReplyJob, reply_job_key(task))
                        if job is not None:
                            job.completed_at = datetime.now(UTC)
                await queue.acknowledge(task)
                logger.error(
                    "delayed reply failed permanently: %s attempts=%s",
                    type(exc).__name__,
                    MAX_DELIVERY_ATTEMPTS,
                )


async def reconcile_reply_jobs(
    pipeline: Pipeline, queue: DelayedReplyQueue, *, now: datetime | None = None
) -> int:
    """Republish outbox jobs that Redis no longer holds.

    The database is the source of truth for scheduled replies; Redis only
    orders and leases them. A job whose member is present in any form, leased
    or carrying retry attempts, is left alone so that leases and attempt
    counters survive. Only a job missing entirely is published again.
    """
    del now  # the due time comes from the job itself
    present = {reply_job_key(task) for task in await queue.snapshot()}
    async with pipeline.database.session() as session:
        jobs = list(
            await session.scalars(
                select(models.ReplyJob).where(models.ReplyJob.completed_at.is_(None))
            )
        )
    republished = 0
    for job in jobs:
        if job.key in present:
            continue
        await queue.schedule(ReplyTask.from_json(json.dumps(job.payload)), due_at=job.due_at)
        republished += 1
    if republished:
        logger.warning("reply jobs republished to redis: count=%s", republished)
    return republished


async def run_delayed_replies(
    pipeline: Pipeline,
    queue: DelayedReplyQueue,
    *,
    interval: float = DELAYED_POLL_SECONDS,
    reconcile_interval: float = RECONCILE_SECONDS,
) -> None:
    """Deliver replies whose delay has elapsed, including ones left by a restart."""
    loop = asyncio.get_running_loop()
    last_reconcile: float | None = None
    while not asyncio.current_task().cancelling():
        try:
            if last_reconcile is None or loop.time() - last_reconcile >= reconcile_interval:
                # Runs at startup and then periodically, so a queue entry lost
                # while the process is up is recovered without a restart.
                await reconcile_reply_jobs(pipeline, queue)
                last_reconcile = loop.time()
            await deliver_due_once(pipeline, queue)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("delayed reply worker failed: %s", type(exc).__name__)
        await asyncio.sleep(interval)


async def run_morning_digest(
    digest: MorningDigest, *, interval: float = MORNING_POLL_SECONDS
) -> None:
    while not asyncio.current_task().cancelling():
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
    while not asyncio.current_task().cancelling():
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
        moment = now or datetime.now(UTC)
        await session.execute(
            delete(models.MorningQueue).where(
                models.MorningQueue.occurred_at < moment - timedelta(days=30)
            )
        )
        for model in (models.ReplyJob, models.NotificationJob):
            await session.execute(
                delete(model).where(model.completed_at < moment - timedelta(days=30))
            )
        await session.execute(delete(models.PdfToken).where(models.PdfToken.expires_at < moment))
        return await delete_expired_messages(session, now=moment, batch_size=batch_size)


async def run_retention_cleanup(
    database: Database, *, interval: float = RETENTION_CLEANUP_SECONDS
) -> None:
    while not asyncio.current_task().cancelling():
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


def reply_job_key(task: ReplyTask) -> str:
    return f"reply:{task.business_connection_id}:{task.contact_id}:{task.message_id}"
