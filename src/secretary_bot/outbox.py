"""Retryable payment notifications stored together with the paid transition."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from secretary_bot import models
from secretary_bot.actions import LogAction
from secretary_bot.sender import BusinessReplySender
from secretary_bot.storage import Database, log_decision

logger = logging.getLogger(__name__)

# Roughly two hours of retries at the longest backoff; after that the failure
# is written to the journal, shown on the overview and can be retried by hand.
MAX_NOTIFICATION_ATTEMPTS = 24
NOTIFICATION_FAILED = "NOTIFICATION_FAILED"


async def deliver_notifications(database: Database, sender: BusinessReplySender) -> None:
    now = datetime.now(UTC)
    async with database.session() as session:
        jobs = list(
            await session.scalars(
                select(models.NotificationJob)
                .where(
                    models.NotificationJob.completed_at.is_(None),
                    models.NotificationJob.due_at <= now,
                )
                .limit(50)
            )
        )
    for job in jobs:
        async with database.session() as session, session.begin():
            claimed = await session.scalar(
                update(models.NotificationJob)
                .where(
                    models.NotificationJob.key == job.key,
                    models.NotificationJob.completed_at.is_(None),
                    models.NotificationJob.due_at <= now,
                )
                .values(due_at=now + timedelta(minutes=5))
                .returning(models.NotificationJob.key)
            )
            if claimed is None:
                continue
        try:
            result = await sender.send(**job.payload)
            error = None if result.is_sent else result.error_code or "SEND_FAILED"
        except Exception as exc:
            error = type(exc).__name__
            result = None
        async with database.session() as session, session.begin():
            row = await session.get(models.NotificationJob, job.key)
            if row is None:
                continue
            row.attempts += 1
            row.error_code = error
            row.due_at = now + timedelta(seconds=min(300, 5 * 2 ** min(row.attempts, 6)))
            if error is not None and row.attempts >= MAX_NOTIFICATION_ATTEMPTS:
                logger.error("notification abandoned: key=%s error=%s", job.key, error)
                row.completed_at = now
                await _record_failure(session, row, now=now)
            if error is None:
                row.completed_at = now
                if job.key.startswith("paid-owner:"):
                    request = await session.get(models.ContactRequest, int(job.key.split(":")[1]))
                    if request is not None and result is not None:
                        request.owner_notification_message_id = result.message_id


async def retry_failed_notifications(session, *, connection_id: int, now: datetime) -> int:
    """Put abandoned notifications back in the queue; the paid record is untouched."""
    result = await session.execute(
        update(models.NotificationJob)
        .where(
            models.NotificationJob.connection_id == connection_id,
            models.NotificationJob.completed_at.is_not(None),
            models.NotificationJob.error_code.is_not(None),
        )
        .values(completed_at=None, attempts=0, error_code=None, due_at=now)
    )
    return result.rowcount or 0


async def _record_failure(session, job: models.NotificationJob, *, now: datetime) -> None:
    """Make the final failure visible where the owner already looks: the journal."""
    contact_id = await _contact_for(session, job)
    if contact_id is None:
        return
    await log_decision(
        session,
        connection_id=job.connection_id,
        contact_id=contact_id,
        action=LogAction.ERROR,
        direction="out",
        error_code=NOTIFICATION_FAILED,
        occurred_at=now,
    )


async def _contact_for(session, job: models.NotificationJob) -> int | None:
    kind, _, raw_id = job.key.partition(":")
    if kind in {"paid-owner", "paid-contact"} and raw_id.isdigit():
        request = await session.get(models.ContactRequest, int(raw_id))
        if request is not None:
            return request.contact_id
    if job.payload.get("business_connection_id"):
        return job.payload.get("chat_id")
    return None


async def run_notifications(database: Database, sender: BusinessReplySender) -> None:
    while not asyncio.current_task().cancelling():
        try:
            await deliver_notifications(database, sender)
        except Exception as exc:
            logger.error("notification worker failed: %s", type(exc).__name__)
        await asyncio.sleep(5)
