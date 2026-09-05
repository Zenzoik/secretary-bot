from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

from secretary_bot.models import MorningQueue
from secretary_bot.notifications import OwnerNotifier
from secretary_bot.storage import (
    Database,
    list_connections,
    mark_morning_delivered,
    pending_morning,
)
from secretary_bot.texts import render_morning_digest

logger = logging.getLogger(__name__)

DELIVERY_TIME = time(8, 0)
# Missed morning reminders are caught up after 08:00.


@dataclass(slots=True)
class MorningDigest:
    """FR-10: the money messages the owner was promised an answer to."""

    database: Database
    notifier: OwnerNotifier
    summary_available: bool = False

    async def run_once(self, *, now: datetime | None = None) -> int:
        moment = now or datetime.now(UTC)
        delivered = 0
        async with self.database.session() as session:
            connections = await list_connections(session)
        for connection in connections:
            if (
                not connection.policy.is_active
                or connection.policy.kill_switch
                or connection.owner_chat_id is None
                or (self.summary_available and connection.message_retention_enabled)
            ):
                continue
            local_now = moment.astimezone(ZoneInfo(connection.policy.timezone))
            if not is_delivery_time(local_now):
                continue
            cutoff = local_now.replace(hour=8, minute=0, second=0, microsecond=0)
            async with self.database.session() as session:
                rows = [
                    row
                    for row in await pending_morning(session, connection.id)
                    if row.occurred_at <= cutoff
                ]
            if not rows:
                continue
            for offset in range(0, len(rows), 10):
                batch = rows[offset : offset + 10]
                await self.notifier.alert(
                    connection.owner_chat_id,
                    render_digest(batch, timezone=connection.policy.timezone),
                )
                async with self.database.session() as session, session.begin():
                    await mark_morning_delivered(session, [row.id for row in batch])
                delivered += len(batch)
        return delivered


def is_delivery_time(local_now: datetime) -> bool:
    start = local_now.replace(
        hour=DELIVERY_TIME.hour, minute=DELIVERY_TIME.minute, second=0, microsecond=0
    )
    return start <= local_now


def render_digest(rows: list[MorningQueue], *, timezone: str) -> str:
    return render_morning_digest(rows, timezone=timezone)
