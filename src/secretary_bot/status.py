"""One effective operating status shared by the owner interface."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretary_bot import models
from secretary_bot.gate import current_window
from secretary_bot.storage import load_connection


async def operating_status(session: AsyncSession, connection: models.Connection) -> dict:
    now = datetime.now(UTC)
    record = await load_connection(session, connection.business_connection_id)
    assert record is not None
    policy = record.policy
    local = now.astimezone(ZoneInfo(policy.timezone))
    window = current_window(policy.windows, local)
    next_start = None
    if window is None:
        candidates = []
        for offset in range(8):
            day = local.date() + timedelta(days=offset)
            for rule in policy.windows:
                point = datetime.combine(day, rule.time_from, tzinfo=local.tzinfo)
                if rule.is_active and rule.duration and rule.starts_on(day) and point > local:
                    candidates.append(point)
        next_start = min(candidates).isoformat() if candidates else None
    if not connection.is_active or not record.rights.get("can_reply"):
        code, label = "inactive", "Немає підключення або права відповіді"
    elif connection.kill_switch:
        code, label = "stopped", "Секретаря вимкнено"
    elif policy.muted_until and policy.muted_until > now:
        code, label = "paused", "Тимчасова пауза"
    elif window is None:
        code, label = "outside_schedule", "Зараз поза основним розкладом"
    elif connection.dry_run:
        code, label = "dry_run", "Тестовий режим: лише прев’ю"
    else:
        code, label = "live", "Відповідає клієнтам"
    last_reply = await session.scalar(
        select(models.MessageLog.occurred_at)
        .where(
            models.MessageLog.connection_id == connection.id, models.MessageLog.action == "replied"
        )
        .order_by(models.MessageLog.occurred_at.desc())
        .limit(1)
    )
    last_error = await session.scalar(
        select(models.MessageLog)
        .where(
            models.MessageLog.connection_id == connection.id, models.MessageLog.action == "error"
        )
        .order_by(models.MessageLog.occurred_at.desc())
        .limit(1)
    )
    summary = await session.scalar(
        select(models.SummaryRun)
        .where(models.SummaryRun.connection_id == connection.id)
        .order_by(models.SummaryRun.period_end.desc())
        .limit(1)
    )
    pending = await session.scalar(
        select(func.count())
        .select_from(models.NotificationJob)
        .where(
            models.NotificationJob.connection_id == connection.id,
            models.NotificationJob.completed_at.is_(None),
        )
    )
    uncertain = await session.scalar(
        select(func.count())
        .select_from(models.DeliveryReceipt)
        .where(
            models.DeliveryReceipt.connection_id == connection.id,
            models.DeliveryReceipt.state.in_(["sending", "uncertain"]),
        )
    )
    failed_notifications = await session.scalar(
        select(func.count())
        .select_from(models.NotificationJob)
        .where(
            models.NotificationJob.connection_id == connection.id,
            models.NotificationJob.completed_at.is_not(None),
            models.NotificationJob.error_code.is_not(None),
        )
    )
    return {
        "code": code,
        "label": label,
        "timezone": policy.timezone,
        "muted_until": policy.muted_until.isoformat() if policy.muted_until else None,
        "next_start": next_start,
        "window_end": window.ends_at.isoformat() if window else None,
        "last_reply_at": last_reply.isoformat() if last_reply else None,
        "last_error": last_error.error_code if last_error else None,
        "summary_status": summary.status if summary else "none",
        "pending_notifications": pending,
        "failed_notifications": failed_notifications,
        "uncertain_deliveries": uncertain,
        "note": "Персональні правила контактів можуть відрізнятися від основного розкладу.",
    }
