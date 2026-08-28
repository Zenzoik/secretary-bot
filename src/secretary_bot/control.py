from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from secretary_bot.storage import (
    ConnectionRecord,
    ContactCardRecord,
    Database,
    daily_action_counts,
    load_contact_card,
    load_owner_connection,
    set_connection_control,
    set_contact_exclusion,
    set_contact_template_override,
)
from secretary_bot.templates import DEFAULT_TEMPLATES, TemplateCode

MAX_MUTE_HOURS = 168


class ControlBot(Protocol):
    async def send_message(self, **kwargs: Any) -> Any: ...

    async def answer_callback_query(self, callback_query_id: str, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class ControlResponse:
    text: str
    reply_markup: InlineKeyboardMarkup | None = None


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
        if command not in {"start", "status", "off", "on", "mute", "today"}:
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

        kwargs: dict[str, Any] = {"chat_id": message.chat.id, "text": response.text}
        if response.reply_markup is not None:
            kwargs["reply_markup"] = response.reply_markup
        await self.bot.send_message(**kwargs)
        return True

    async def handle_callback(self, query: CallbackQuery, *, now: datetime | None = None) -> bool:
        parsed = _parse_contact_callback(query.data)
        if parsed is None:
            return False
        sender = query.from_user
        moment = now or datetime.now(UTC)
        contact_id, action, argument = parsed

        async with self.database.session() as session, session.begin():
            connection = await load_owner_connection(session, sender.id)
            if connection is None:
                return False
            response = await self._contact_action(
                session,
                connection,
                contact_id=contact_id,
                action=action,
                argument=argument,
                now=moment,
            )

        await self.bot.answer_callback_query(query.id, text="Готово")
        if response is not None and connection.owner_chat_id is not None:
            kwargs: dict[str, Any] = {
                "chat_id": connection.owner_chat_id,
                "text": response.text,
            }
            if response.reply_markup is not None:
                kwargs["reply_markup"] = response.reply_markup
            await self.bot.send_message(**kwargs)
        return True

    async def _execute(
        self,
        session: Any,
        connection: ConnectionRecord,
        *,
        command: str,
        argument: str,
        now: datetime,
    ) -> ControlResponse:
        if command == "start":
            contact_id = _business_contact_id(argument)
            if contact_id is None:
                return ControlResponse("Откройте Manage Bot из нужного управляемого чата.")
            card = await load_contact_card(session, connection.id, contact_id, now=now)
            return _render_contact_card(card, connection=connection)
        if command == "status":
            return ControlResponse(_render_status(connection, now=now))
        if command == "off":
            await set_connection_control(session, connection.id, kill_switch=True, muted_until=None)
            return ControlResponse(
                "⛔ Секретарь выключен. Уже запланированные ответы тоже остановлены."
            )
        if command == "on":
            await set_connection_control(
                session, connection.id, kill_switch=False, muted_until=None
            )
            return ControlResponse("✅ Секретарь включён. Временная пауза снята.")
        if command == "mute":
            hours = _mute_hours(argument)
            if hours is None:
                return ControlResponse(
                    f"Использование: /mute N, где N — от 1 до {MAX_MUTE_HOURS} часов."
                )
            until = now + timedelta(hours=hours)
            await set_connection_control(
                session, connection.id, kill_switch=False, muted_until=until
            )
            local_until = until.astimezone(ZoneInfo(connection.policy.timezone))
            return ControlResponse(f"⏸ Пауза до {local_until:%d.%m %H:%M} ({hours} ч).")

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
        return ControlResponse(_render_today(counts, local_date=local_now.strftime("%d.%m.%Y")))

    async def _contact_action(
        self,
        session: Any,
        connection: ConnectionRecord,
        *,
        contact_id: int,
        action: str,
        argument: str | None,
        now: datetime,
    ) -> ControlResponse | None:
        if action == "exclude":
            await set_contact_exclusion(
                session,
                connection.id,
                contact_id,
                until=None,
                reason="owner_card_permanent",
            )
            return ControlResponse("🚫 Контакт исключён навсегда.")
        if action == "today":
            zone = ZoneInfo(connection.policy.timezone)
            local_now = now.astimezone(zone)
            until = datetime.combine(local_now.date(), time.min, tzinfo=zone) + timedelta(days=1)
            await set_contact_exclusion(
                session,
                connection.id,
                contact_id,
                until=until.astimezone(UTC),
                reason="owner_card_today",
            )
            return ControlResponse(f"😴 Контакт исключён до {until:%d.%m %H:%M}.")
        if action == "templates":
            return ControlResponse(
                "Выберите шаблон для этого контакта:", _template_keyboard(contact_id)
            )
        if action == "template" and argument in {code.value for code in TemplateCode}:
            code = TemplateCode(argument)
            await set_contact_template_override(
                session,
                connection.id,
                contact_id,
                template_code=code.value,
                template_text=DEFAULT_TEMPLATES[code],
            )
            return ControlResponse(f"✏️ Для контакта выбран шаблон: {code.value}.")
        return None


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


def _business_contact_id(argument: str) -> int | None:
    prefix = "bizChat"
    raw_id = argument[len(prefix) :] if argument.startswith(prefix) else ""
    if not raw_id.isdigit():
        return None
    contact_id = int(raw_id)
    return contact_id if contact_id > 0 else None


def _parse_contact_callback(data: str | None) -> tuple[int, str, str | None] | None:
    parts = (data or "").split(":")
    if len(parts) not in {3, 4} or parts[0] != "contact" or not parts[1].isdigit():
        return None
    contact_id = int(parts[1])
    action = parts[2]
    if contact_id <= 0 or action not in {"exclude", "today", "templates", "template", "ok"}:
        return None
    argument = parts[3] if len(parts) == 4 else None
    if action == "template" and argument is None:
        return None
    return contact_id, action, argument


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


def _render_contact_card(
    card: ContactCardRecord, *, connection: ConnectionRecord
) -> ControlResponse:
    zone = ZoneInfo(connection.policy.timezone)
    name = card.contact_name or f"Контакт {card.contact_id}"
    last = (
        "нет"
        if card.last_auto_reply_at is None
        else card.last_auto_reply_at.astimezone(zone).strftime("%d.%m %H:%M")
    )
    if card.permanently_excluded:
        exclusion = "навсегда"
    elif card.exclusion_until is not None:
        exclusion = f"до {card.exclusion_until.astimezone(zone):%d.%m %H:%M}"
    else:
        exclusion = "нет"
    forced = card.forced_template_code or "автоматически"
    text = (
        f"👤 {name}\n"
        f"Автоответов за 30 дней: {card.auto_reply_count}\n"
        f"Последний: {last}\n"
        f"Исключение: {exclusion}\n"
        f"Шаблон: {forced}"
    )
    return ControlResponse(text, _contact_keyboard(card.contact_id))


def _contact_keyboard(contact_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚫 Исключить навсегда",
                    callback_data=f"contact:{contact_id}:exclude",
                )
            ],
            [
                InlineKeyboardButton(
                    text="😴 Не трогать сегодня",
                    callback_data=f"contact:{contact_id}:today",
                )
            ],
            [
                InlineKeyboardButton(
                    text="✏️ Свой шаблон",
                    callback_data=f"contact:{contact_id}:templates",
                )
            ],
            [
                InlineKeyboardButton(
                    text="◀️ Всё в порядке", callback_data=f"contact:{contact_id}:ok"
                )
            ],
        ]
    )


def _template_keyboard(contact_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Обычный",
                    callback_data=f"contact:{contact_id}:template:off_hours_default",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Денежный",
                    callback_data=f"contact:{contact_id}:template:money_priority",
                )
            ],
        ]
    )
