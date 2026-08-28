from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from aiogram.types import Message

from secretary_bot.storage import (
    ConnectionRecord,
    Database,
    daily_action_counts,
    load_owner_connection,
    set_connection_control,
)

MAX_MUTE_HOURS = 168


class ControlBot(Protocol):
    async def send_message(self, **kwargs: Any) -> Any: ...


@dataclass(slots=True)
class ControlPlane:
    database: Database
    bot: ControlBot

    async def handle_message(self, message: Message, *, now: datetime | None = None) -> bool:
        """Handle a private owner command; return whether it belonged to us."""
        parsed = _parse_command(message.text)
        sender = message.from_user
        if parsed is None or sender is None or message.chat.type != "private":
            return False

        command, argument = parsed
        if command not in {"status", "off", "on", "mute", "today"}:
            return False

        moment = now or datetime.now(UTC)
        async with self.database.session() as session, session.begin():
            connection = await load_owner_connection(session, sender.id)
            if connection is None or (
                connection.owner_chat_id is not None and connection.owner_chat_id != message.chat.id
            ):
                return False

            response = await self._execute(
                session, connection, command=command, argument=argument, now=moment
            )

        await self.bot.send_message(chat_id=message.chat.id, text=response)
        return True

    async def _execute(
        self,
        session: Any,
        connection: ConnectionRecord,
        *,
        command: str,
        argument: str,
        now: datetime,
    ) -> str:
        if command == "status":
            return _render_status(connection, now=now)
        if command == "off":
            await set_connection_control(session, connection.id, kill_switch=True, muted_until=None)
            return "⛔ Секретарь выключен. Уже запланированные ответы тоже остановлены."
        if command == "on":
            await set_connection_control(
                session, connection.id, kill_switch=False, muted_until=None
            )
            return "✅ Секретарь включён. Временная пауза снята."
        if command == "mute":
            hours = _mute_hours(argument)
            if hours is None:
                return f"Использование: /mute N, где N — от 1 до {MAX_MUTE_HOURS} часов."
            until = now + timedelta(hours=hours)
            await set_connection_control(
                session, connection.id, kill_switch=False, muted_until=until
            )
            local_until = until.astimezone(ZoneInfo(connection.policy.timezone))
            return f"⏸ Пауза до {local_until:%d.%m %H:%M} ({hours} ч)."

        zone = ZoneInfo(connection.policy.timezone)
        local_now = now.astimezone(zone)
        day_start = datetime.combine(local_now.date(), time.min, tzinfo=zone)
        next_day = day_start + timedelta(days=1)
        counts = await daily_action_counts(
            session,
            connection.id,
            since=day_start.astimezone(UTC),
            until=next_day.astimezone(UTC),
        )
        return _render_today(counts, local_date=local_now.strftime("%d.%m.%Y"))


def _parse_command(text: str | None) -> tuple[str, str] | None:
    if not text:
        return None
    head, _, argument = text.strip().partition(" ")
    if not head.startswith("/"):
        return None
    command = head[1:].split("@", 1)[0].lower()
    return command, argument.strip()


def _mute_hours(argument: str) -> int | None:
    try:
        hours = int(argument)
    except ValueError:
        return None
    return hours if 1 <= hours <= MAX_MUTE_HOURS else None


def _render_status(connection: ConnectionRecord, *, now: datetime) -> str:
    muted_until = connection.policy.muted_until
    if connection.policy.kill_switch:
        state = "выключен"
        pause = "нет"
    elif muted_until is not None and now < muted_until:
        state = "пауза"
        local_until = muted_until.astimezone(ZoneInfo(connection.policy.timezone))
        pause = f"до {local_until:%d.%m %H:%M}"
    else:
        state = "включён"
        pause = "нет"
    mode = "dry-run" if connection.dry_run else "live"
    return (
        f"🤖 Секретарь: {state}\n"
        f"Режим: {mode}\n"
        f"Пауза: {pause}\n"
        f"Часовой пояс: {connection.policy.timezone}"
    )


def _render_today(counts: list[tuple[str, str | None, int]], *, local_date: str) -> str:
    if not counts:
        return f"📊 {local_date}: действий пока нет."
    lines = [f"📊 {local_date}:"]
    for action, category, count in counts:
        label = action if category is None else f"{action}/{category}"
        lines.append(f"• {label}: {count}")
    return "\n".join(lines)
