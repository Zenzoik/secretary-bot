from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from secretary_bot.models import MorningQueue
from secretary_bot.notifications import OwnerNotifier
from secretary_bot.storage import (
    Database,
    list_connections,
    mark_morning_delivered,
    pending_morning,
)

logger = logging.getLogger(__name__)

DELIVERY_TIME = time(8, 0)
# The digest is delivered by a periodic tick, so "08:00" is really the first
# tick after it. The window is wide enough to survive a slow restart.
DELIVERY_WINDOW = timedelta(minutes=30)


@dataclass(slots=True)
class MorningDigest:
    """FR-10: the money messages the owner was promised an answer to."""

    database: Database
    notifier: OwnerNotifier

    async def run_once(self, *, now: datetime | None = None) -> int:
        moment = now or datetime.now(UTC)
        delivered = 0
        async with self.database.session() as session, session.begin():
            for connection in await list_connections(session):
                if (
                    not connection.policy.is_active
                    or connection.policy.kill_switch
                    or connection.owner_chat_id is None
                ):
                    continue
                local_now = moment.astimezone(ZoneInfo(connection.policy.timezone))
                if not is_delivery_time(local_now):
                    continue
                rows = await pending_morning(session, connection.id)
                if not rows:
                    continue
                await self.notifier.alert(
                    connection.owner_chat_id,
                    render_digest(rows, timezone=connection.policy.timezone),
                )
                await mark_morning_delivered(session, [row.id for row in rows])
                delivered += len(rows)
        return delivered


def is_delivery_time(local_now: datetime) -> bool:
    start = local_now.replace(
        hour=DELIVERY_TIME.hour, minute=DELIVERY_TIME.minute, second=0, microsecond=0
    )
    return start <= local_now < start + DELIVERY_WINDOW


def render_digest(rows: list[MorningQueue], *, timezone: str) -> str:
    zone = ZoneInfo(timezone)
    lines = ["☀️ Утром обещали ответить:"]
    for row in rows:
        who = row.contact_name or f"id {row.contact_id}"
        detail = row.summary or "вопрос про деньги"
        lines.append(f"• {row.occurred_at.astimezone(zone):%H:%M} · {who} — {detail}")
    return "\n".join(lines)
