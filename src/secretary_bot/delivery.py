"""Durable send receipts: a lost response must never trigger a blind duplicate."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from secretary_bot import models
from secretary_bot.sender import BusinessReplySender, SendOutcome, SendResult
from secretary_bot.storage import Database


async def send_once(
    database: Database,
    sender: BusinessReplySender,
    *,
    key: str,
    connection_id: int,
    retry_uncertain_after: timedelta | None = None,
    **kwargs,
) -> SendResult:
    """Send at most once per key.

    A definite failure may always be retried. An unknown outcome (``sending``
    after a crash, ``uncertain`` after a lost response) is retried only when
    the caller accepts a possible duplicate and passes ``retry_uncertain_after``:
    messages to contacts never do, summaries to the owner's own chat may.
    """
    now = datetime.now(UTC)
    async with database.session() as session, session.begin():
        receipt = await session.get(models.DeliveryReceipt, key)
        if receipt is not None:
            if receipt.state == "sent":
                return SendResult(SendOutcome.SENT, message_id=receipt.message_id, replayed=True)
            retryable = ["failed"]
            if (
                retry_uncertain_after is not None
                and receipt.updated_at <= now - retry_uncertain_after
            ):
                retryable += ["sending", "uncertain"]
            if receipt.state not in retryable:
                return SendResult(SendOutcome.FAILED, error_code="DELIVERY_UNCERTAIN")
            claimed = await session.scalar(
                update(models.DeliveryReceipt)
                .where(
                    models.DeliveryReceipt.key == key,
                    models.DeliveryReceipt.state.in_(retryable),
                )
                .values(state="sending", error_code=None, updated_at=now)
                .returning(models.DeliveryReceipt.key)
            )
            if claimed is None:
                return SendResult(SendOutcome.FAILED, error_code="DELIVERY_UNCERTAIN")
        else:
            try:
                async with session.begin_nested():
                    session.add(models.DeliveryReceipt(key=key, connection_id=connection_id))
                    await session.flush()
            except IntegrityError:
                return SendResult(SendOutcome.FAILED, error_code="DELIVERY_UNCERTAIN")
    # A crash after this durable claim leaves an explicit uncertain delivery.
    # No message body is retained for manual messages when retention is disabled.
    result = await sender.send(**kwargs)
    async with database.session() as session, session.begin():
        receipt = await session.get(models.DeliveryReceipt, key)
        assert receipt is not None
        receipt.state = (
            "sent"
            if result.is_sent
            else "uncertain"
            if result.error_code == "DELIVERY_UNCERTAIN"
            else "failed"
        )
        receipt.message_id = result.message_id
        receipt.error_code = result.error_code
        receipt.updated_at = datetime.now(UTC)
    return result
