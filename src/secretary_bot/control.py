from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from secretary_bot.callbacks import finalize_callback
from secretary_bot.storage import (
    ConnectionRecord,
    ContactCardRecord,
    Database,
    cancel_live_confirmation,
    confirm_live_mode,
    daily_action_counts,
    load_access_user,
    load_contact_card,
    load_owner_connection,
    request_live_confirmation,
    set_connection_control,
    set_contact_exclusion,
    set_contact_template_override,
    set_control_state,
)
from secretary_bot.templates import DEFAULT_TEMPLATES, TemplateCode

MAX_MUTE_HOURS = 168
LIVE_CONFIRMATION_TTL = timedelta(minutes=5)

BUTTON_STATUS = "📊 Статус"
BUTTON_TODAY = "🗓 Сегодня"
BUTTON_OFF = "⛔ Выключить"
BUTTON_ON = "▶️ Включить"
BUTTON_MUTE = "⏸ Пауза"
BUTTON_LIVE = "⚠️ Включить live"
BUTTON_LIVE_ACTIVE = "🔴 Live включён"
BUTTON_BACK = "↩️ Назад"
BUTTON_LIVE_CONFIRM = "⚠️ Подтверждаю live"
BUTTON_CANCEL = "Отмена"
MUTE_BUTTONS = {
    "1 час": 1,
    "3 часа": 3,
    "8 часов": 8,
    "24 часа": 24,
}


class ControlBot(Protocol):
    async def send_message(self, **kwargs: Any) -> Any: ...

    async def answer_callback_query(self, callback_query_id: str, **kwargs: Any) -> Any: ...

    async def edit_message_text(self, **kwargs: Any) -> Any: ...

    async def edit_message_reply_markup(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class ControlResponse:
    text: str
    reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None = None


@dataclass(slots=True)
class ControlPlane:
    database: Database
    bot: ControlBot

    async def handle_message(self, message: Message, *, now: datetime | None = None) -> bool:
        """Handle a private owner command; return whether it belonged to us."""
        sender = message.from_user
        if not message.text or sender is None or message.chat.type != "private":
            return False

        moment = now or datetime.now(UTC)
        async with self.database.session() as session, session.begin():
            access = await load_access_user(session, sender.id)
            if access is None or not access.can_process:
                return False
            connection = await load_owner_connection(session, sender.id)
            if connection is None or (
                connection.owner_chat_id is not None and connection.owner_chat_id != message.chat.id
            ):
                return False

            intent = _control_intent(message.text, state=connection.control_state)
            if intent is None:
                return False
            command, argument = intent

            response = await self._execute(
                session, connection, command=command, argument=argument, now=moment
            )

        kwargs: dict[str, Any] = {"chat_id": message.chat.id, "text": response.text}
        if response.reply_markup is not None:
            kwargs["reply_markup"] = response.reply_markup
        await self.bot.send_message(**kwargs)
        return True

    async def handle_callback(self, query: CallbackQuery, *, now: datetime | None = None) -> bool:
        contact = _parse_contact_callback(query.data)
        live_action = _parse_live_callback(query.data)
        if contact is None and live_action is None:
            return False
        sender = query.from_user
        moment = now or datetime.now(UTC)

        async with self.database.session() as session, session.begin():
            access = await load_access_user(session, sender.id)
            if access is None or not access.can_process:
                return False
            connection = await load_owner_connection(session, sender.id)
            if connection is None:
                return False
            if contact is not None:
                contact_id, action, argument = contact
                response = await self._contact_action(
                    session,
                    connection,
                    contact_id=contact_id,
                    action=action,
                    argument=argument,
                    now=moment,
                )
            else:
                assert live_action is not None
                response = await self._live_action(
                    session, connection, action=live_action, now=moment
                )

        note, toast = _callback_feedback(contact, live_action)
        await finalize_callback(self.bot, query, note=note, toast=toast)
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
                await cancel_live_confirmation(session, connection.id)
                await set_control_state(session, connection.id, "main")
                fresh = await self._fresh_connection(session, connection)
                return ControlResponse(
                    "Панель управления открыта. Для карточки контакта используйте Manage Bot.",
                    _main_keyboard(fresh, now=now),
                )
            await cancel_live_confirmation(session, connection.id)
            await set_control_state(session, connection.id, "main")
            card = await load_contact_card(session, connection.id, contact_id, now=now)
            return _render_contact_card(card, connection=connection)
        if command == "state_help":
            if connection.control_state == "mute_hours":
                return ControlResponse("Выберите длительность паузы кнопкой.", _mute_keyboard())
            return ControlResponse(
                "Подтвердите live или отмените переход кнопкой.", _live_confirmation_keyboard()
            )
        if command == "menu":
            await cancel_live_confirmation(session, connection.id)
            await set_control_state(session, connection.id, "main")
            fresh = await self._fresh_connection(session, connection)
            return ControlResponse("Главное меню.", _main_keyboard(fresh, now=now))
        if command == "status":
            await cancel_live_confirmation(session, connection.id)
            await set_control_state(session, connection.id, "main")
            fresh = await self._fresh_connection(session, connection)
            return ControlResponse(_render_status(fresh, now=now), _main_keyboard(fresh, now=now))
        if command == "off":
            await set_connection_control(session, connection.id, kill_switch=True, muted_until=None)
            await cancel_live_confirmation(session, connection.id)
            await set_control_state(session, connection.id, "main")
            fresh = await self._fresh_connection(session, connection)
            return ControlResponse(
                "⛔ Секретарь выключен. Уже запланированные ответы тоже остановлены.",
                _main_keyboard(fresh, now=now),
            )
        if command == "on":
            await set_connection_control(
                session, connection.id, kill_switch=False, muted_until=None
            )
            await cancel_live_confirmation(session, connection.id)
            await set_control_state(session, connection.id, "main")
            fresh = await self._fresh_connection(session, connection)
            return ControlResponse(
                "✅ Секретарь включён. Временная пауза снята.",
                _main_keyboard(fresh, now=now),
            )
        if command == "mute":
            if not argument:
                await set_control_state(session, connection.id, "mute_hours")
                return ControlResponse("На сколько часов поставить паузу?", _mute_keyboard())
            hours = _mute_hours(argument)
            if hours is None:
                await set_control_state(session, connection.id, "mute_hours")
                return ControlResponse(
                    f"Выберите кнопку или укажите от 1 до {MAX_MUTE_HOURS} часов.",
                    _mute_keyboard(),
                )
            until = now + timedelta(hours=hours)
            await set_connection_control(
                session, connection.id, kill_switch=False, muted_until=until
            )
            await set_control_state(session, connection.id, "main")
            local_until = until.astimezone(ZoneInfo(connection.policy.timezone))
            fresh = await self._fresh_connection(session, connection)
            return ControlResponse(
                f"⏸ Пауза до {local_until:%d.%m %H:%M} ({hours} ч).",
                _main_keyboard(fresh, now=now),
            )
        if command == "live":
            if not connection.dry_run:
                await set_control_state(session, connection.id, "main")
                return ControlResponse(
                    "Live-режим уже включён.", _main_keyboard(connection, now=now)
                )
            await request_live_confirmation(
                session, connection.id, until=now + LIVE_CONFIRMATION_TTL
            )
            await set_control_state(session, connection.id, "live_confirm")
            return ControlResponse(
                "⚠️ Включить live? После подтверждения бот сможет отвечать контактам.",
                _live_confirmation_keyboard(),
            )
        if command == "live_confirm":
            enabled = await confirm_live_mode(session, connection.id, now=now)
            await set_control_state(session, connection.id, "main")
            fresh = await self._fresh_connection(session, connection)
            text = (
                "⚠️ Live-режим включён. Бот может отвечать контактам."
                if enabled
                else "Подтверждение истекло. Режим остался без изменений."
            )
            return ControlResponse(text, _main_keyboard(fresh, now=now))
        if command == "live_cancel":
            await cancel_live_confirmation(session, connection.id)
            await set_control_state(session, connection.id, "main")
            fresh = await self._fresh_connection(session, connection)
            return ControlResponse(
                "Dry-run сохранён. Live-режим не включён.",
                _main_keyboard(fresh, now=now),
            )

        await cancel_live_confirmation(session, connection.id)
        await set_control_state(session, connection.id, "main")
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
        fresh = await self._fresh_connection(session, connection)
        return ControlResponse(
            _render_today(counts, local_date=local_now.strftime("%d.%m.%Y")),
            _main_keyboard(fresh, now=now),
        )

    async def _fresh_connection(
        self, session: Any, connection: ConnectionRecord
    ) -> ConnectionRecord:
        fresh = await load_owner_connection(session, connection.owner_user_id)
        if fresh is None:
            raise LookupError("connection not found")
        return fresh

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

    async def _live_action(
        self,
        session: Any,
        connection: ConnectionRecord,
        *,
        action: str,
        now: datetime,
    ) -> ControlResponse:
        if action == "cancel":
            await cancel_live_confirmation(session, connection.id)
            await set_control_state(session, connection.id, "main")
            fresh = await self._fresh_connection(session, connection)
            return ControlResponse(
                "Dry-run сохранён. Live-режим не включён.",
                _main_keyboard(fresh, now=now),
            )
        enabled = await confirm_live_mode(session, connection.id, now=now)
        await set_control_state(session, connection.id, "main")
        fresh = await self._fresh_connection(session, connection)
        text = (
            "⚠️ Live-режим включён. Бот может отвечать контактам."
            if enabled
            else "Подтверждение истекло. Повторите включение live."
        )
        return ControlResponse(text, _main_keyboard(fresh, now=now))


def _parse_command(text: str | None) -> tuple[str, str] | None:
    if not text:
        return None
    head, _, argument = text.strip().partition(" ")
    if not head.startswith("/"):
        return None
    command = head[1:].split("@", 1)[0].lower()
    return command, argument.strip()


def _control_intent(text: str, *, state: str) -> tuple[str, str] | None:
    parsed = _parse_command(text)
    allowed_commands = {"start", "status", "off", "on", "mute", "today", "live"}
    if parsed is not None:
        return parsed if parsed[0] in allowed_commands else None

    choice = text.strip()
    if state == "mute_hours":
        if choice in MUTE_BUTTONS:
            return "mute", str(MUTE_BUTTONS[choice])
        if choice in {BUTTON_BACK, BUTTON_CANCEL}:
            return "menu", ""
        return "state_help", ""
    if state == "live_confirm":
        if choice == BUTTON_LIVE_CONFIRM:
            return "live_confirm", ""
        if choice in {BUTTON_CANCEL, BUTTON_BACK}:
            return "live_cancel", ""
        return "state_help", ""

    main_actions = {
        BUTTON_STATUS: ("status", ""),
        BUTTON_TODAY: ("today", ""),
        BUTTON_OFF: ("off", ""),
        BUTTON_ON: ("on", ""),
        BUTTON_MUTE: ("mute", ""),
        BUTTON_LIVE: ("live", ""),
        BUTTON_LIVE_ACTIVE: ("status", ""),
    }
    return main_actions.get(choice)


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


def _parse_live_callback(data: str | None) -> str | None:
    parts = (data or "").split(":")
    if len(parts) != 2 or parts[0] != "live" or parts[1] not in {"confirm", "cancel"}:
        return None
    return parts[1]


def _callback_feedback(
    contact: tuple[int, str, str | None] | None, live_action: str | None
) -> tuple[str, str]:
    if live_action == "confirm":
        return "✅ Обработано: live включён", "Live включён"
    if live_action == "cancel":
        return "✅ Обработано: dry-run сохранён", "Отменено"
    assert contact is not None
    _, action, argument = contact
    if action == "exclude":
        return "✅ Обработано: контакт исключён навсегда", "Контакт исключён"
    if action == "today":
        return "✅ Обработано: контакт не трогаем сегодня", "Исключён до полуночи"
    if action == "templates":
        return "✅ Обработано: выбор персонального шаблона", "Выберите шаблон"
    if action == "template":
        return f"✅ Обработано: выбран шаблон {argument}", "Шаблон выбран"
    return "✅ Обработано: без изменений", "Без изменений"


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


def _main_keyboard(connection: ConnectionRecord, *, now: datetime) -> ReplyKeyboardMarkup:
    muted_until = connection.policy.muted_until
    stopped = connection.policy.kill_switch or (muted_until is not None and now < muted_until)
    power_button = BUTTON_ON if stopped else BUTTON_OFF
    live_button = BUTTON_LIVE if connection.dry_run else BUTTON_LIVE_ACTIVE
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BUTTON_STATUS), KeyboardButton(text=BUTTON_TODAY)],
            [KeyboardButton(text=power_button), KeyboardButton(text=BUTTON_MUTE)],
            [KeyboardButton(text=live_button)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Управление секретарём",
    )


def _mute_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="1 час"), KeyboardButton(text="3 часа")],
            [KeyboardButton(text="8 часов"), KeyboardButton(text="24 часа")],
            [KeyboardButton(text=BUTTON_BACK)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите длительность паузы",
    )


def _live_confirmation_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BUTTON_LIVE_CONFIRM)],
            [KeyboardButton(text=BUTTON_CANCEL)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        one_time_keyboard=True,
        input_field_placeholder="Подтвердите или отмените live",
    )
