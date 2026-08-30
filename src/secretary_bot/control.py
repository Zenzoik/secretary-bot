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
from secretary_bot.delayed import DelayedReplyQueue
from secretary_bot.storage import (
    AccessUserRecord,
    ConnectionRecord,
    ContactCardRecord,
    Database,
    approve_access_user,
    cancel_live_confirmation,
    complete_onboarding,
    confirm_live_mode,
    consume_access_invite,
    create_access_invite,
    daily_action_counts,
    list_access_users,
    load_access_user,
    load_contact_card,
    load_owner_connection,
    request_live_confirmation,
    revoke_access_user,
    set_connection_control,
    set_contact_exclusion,
    set_contact_template_override,
    set_control_state,
    set_onboarding_state,
    set_owner_schedule,
    set_owner_timezone,
)
from secretary_bot.templates import DEFAULT_TEMPLATES, TemplateCode

MAX_MUTE_HOURS = 168
LIVE_CONFIRMATION_TTL = timedelta(minutes=5)
INVITE_TTL = timedelta(hours=24)

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
BUTTON_USERS = "👥 Пользователи"
BUTTON_INVITE = "➕ Пригласить"
BUTTON_USERS_REFRESH = "🔄 Обновить список"
BUTTON_ADMIN_BACK = "↩️ Главное меню"
BUTTON_SCOPE_CONFIRMED = "✅ Only Selected Chats настроено"
BUTTON_RECHECK_CONNECTION = "🔄 Проверить подключение"
TIMEZONE_BUTTONS = {
    "🇺🇦 Киев": "Europe/Kyiv",
    "🇨🇿 Прага": "Europe/Prague",
    "🇵🇱 Варшава": "Europe/Warsaw",
    "🌐 UTC": "UTC",
}
SCHEDULE_BUTTONS = {
    "🌙 Каждый день 22–08": (127, time(22, 0), time(8, 0)),
    "💼 Будни 18–09": (31, time(18, 0), time(9, 0)),
    "🧪 Тестовый 24/7": (127, time(0, 0), time(23, 59)),
}
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
    bot_username: str = ""
    delayed_queue: DelayedReplyQueue | None = None

    async def handle_message(self, message: Message, *, now: datetime | None = None) -> bool:
        """Handle a private owner command; return whether it belonged to us."""
        sender = message.from_user
        if not message.text or sender is None or message.chat.type != "private":
            return False

        moment = now or datetime.now(UTC)
        master_notification: tuple[int, str] | None = None
        async with self.database.session() as session, session.begin():
            invite_token = _invite_token(message.text)
            if invite_token is not None:
                pending = await consume_access_invite(
                    session,
                    token=invite_token,
                    user_id=sender.id,
                    username=sender.username,
                    now=moment,
                )
                if pending is None:
                    response = ControlResponse(
                        "Ссылка приглашения недействительна, уже использована или истекла."
                    )
                elif pending.status == "pending":
                    response = ControlResponse(
                        "✅ Запрос отправлен мастеру. Дождитесь подтверждения доступа."
                    )
                    if pending.invited_by is not None:
                        master_notification = (
                            pending.invited_by,
                            "👤 Получен новый запрос доступа. "
                            "Откройте «Пользователи» для проверки.",
                        )
                else:
                    response = ControlResponse("Доступ уже активен. Отправьте /start.")
                connection = None
                access = pending
            else:
                access = await load_access_user(session, sender.id)
                if access is None:
                    return False
                if access.status == "pending":
                    response = ControlResponse("Запрос ожидает подтверждения мастером.")
                    connection = None
                elif access.status == "revoked":
                    response = ControlResponse("Доступ к боту отозван. Обратитесь к мастеру.")
                    connection = None
                else:
                    connection = await load_owner_connection(session, sender.id)

            if connection is None:
                if invite_token is None and access is not None and access.status == "active":
                    response = ControlResponse(
                        "Подключите бота в Telegram Chat Automation, затем нажмите проверку.",
                        _connection_keyboard(),
                    )
            else:
                if (
                    connection.owner_chat_id is not None
                    and connection.owner_chat_id != message.chat.id
                ):
                    return False
                if access is None or not access.can_process:
                    assert access is not None
                    response = await self._onboarding(
                        session,
                        connection,
                        access,
                        choice=message.text.strip(),
                        now=moment,
                    )
                else:
                    intent = _control_intent(message.text, state=connection.control_state)
                    if intent is None:
                        return False
                    command, argument = intent

                    response = await self._execute(
                        session,
                        connection,
                        command=command,
                        argument=argument,
                        now=moment,
                        is_master=access.is_master,
                    )

        kwargs: dict[str, Any] = {"chat_id": message.chat.id, "text": response.text}
        if response.reply_markup is not None:
            kwargs["reply_markup"] = response.reply_markup
        await self.bot.send_message(**kwargs)
        if master_notification is not None:
            chat_id, text = master_notification
            await self.bot.send_message(chat_id=chat_id, text=text)
        return True

    async def handle_callback(self, query: CallbackQuery, *, now: datetime | None = None) -> bool:
        contact = _parse_contact_callback(query.data)
        live_action = _parse_live_callback(query.data)
        access_action = _parse_access_callback(query.data)
        if contact is None and live_action is None and access_action is None:
            return False
        sender = query.from_user
        moment = now or datetime.now(UTC)
        target_notification: tuple[int, str] | None = None
        cancel_connection_id: int | None = None

        async with self.database.session() as session, session.begin():
            access = await load_access_user(session, sender.id)
            if access_action is not None:
                if access is None or not access.is_master:
                    return False
                action, target_id = access_action
                response, target_notification, cancel_connection_id = await self._access_action(
                    session,
                    action=action,
                    target_id=target_id,
                    master_id=sender.id,
                    now=moment,
                )
            elif access is None or not access.can_process:
                return False
            else:
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
                        session,
                        connection,
                        action=live_action,
                        now=moment,
                        is_master=access.is_master,
                    )

        note, toast = _callback_feedback(contact, live_action, access_action)
        await finalize_callback(self.bot, query, note=note, toast=toast)
        if cancel_connection_id is not None and self.delayed_queue is not None:
            await self.delayed_queue.cancel_connection(cancel_connection_id)
        if response is not None:
            kwargs: dict[str, Any] = {
                "chat_id": sender.id,
                "text": response.text,
            }
            if response.reply_markup is not None:
                kwargs["reply_markup"] = response.reply_markup
            await self.bot.send_message(**kwargs)
        if target_notification is not None:
            chat_id, text = target_notification
            await self.bot.send_message(chat_id=chat_id, text=text)
        return True

    async def handle_business_connection(
        self, owner_user_id: int, owner_chat_id: int, *, now: datetime | None = None
    ) -> bool:
        """Advance an approved user's onboarding after Telegram connects the bot."""
        moment = now or datetime.now(UTC)
        async with self.database.session() as session, session.begin():
            access = await load_access_user(session, owner_user_id)
            connection = await load_owner_connection(session, owner_user_id)
            if (
                access is None
                or access.status != "active"
                or access.can_process
                or connection is None
            ):
                return False
            response = await self._onboarding(session, connection, access, choice="", now=moment)
        kwargs: dict[str, Any] = {"chat_id": owner_chat_id, "text": response.text}
        if response.reply_markup is not None:
            kwargs["reply_markup"] = response.reply_markup
        await self.bot.send_message(**kwargs)
        return True

    async def _onboarding(
        self,
        session: Any,
        connection: ConnectionRecord,
        access: AccessUserRecord,
        *,
        choice: str,
        now: datetime,
    ) -> ControlResponse:
        state = access.onboarding_state
        if state == "awaiting_connection":
            if not connection.policy.is_active:
                return ControlResponse(
                    "Подключение выключено. Включите бота в Chat Automation и проверьте снова.",
                    _connection_keyboard(),
                )
            missing = _missing_onboarding_rights(connection)
            if missing:
                return ControlResponse(
                    "Не хватает прав Telegram: "
                    + ", ".join(missing)
                    + ". Разрешите чтение и ответы, затем нажмите проверку.",
                    _connection_keyboard(),
                )
            await set_onboarding_state(session, connection.owner_user_id, "timezone", now=now)
            return ControlResponse("Шаг 1/3. Выберите часовой пояс.", _timezone_keyboard())
        if state == "timezone":
            timezone = TIMEZONE_BUTTONS.get(choice)
            if timezone is None:
                return ControlResponse(
                    "Шаг 1/3. Выберите часовой пояс кнопкой.", _timezone_keyboard()
                )
            await set_owner_timezone(
                session,
                connection_id=connection.id,
                user_id=connection.owner_user_id,
                timezone=timezone,
                now=now,
            )
            return ControlResponse(
                f"Часовой пояс: {timezone}.\nШаг 2/3. Выберите расписание.",
                _schedule_keyboard(),
            )
        if state == "schedule":
            if choice == BUTTON_BACK:
                await set_onboarding_state(session, connection.owner_user_id, "timezone", now=now)
                return ControlResponse("Вернулись к выбору часового пояса.", _timezone_keyboard())
            preset = SCHEDULE_BUTTONS.get(choice)
            if preset is None:
                return ControlResponse(
                    "Шаг 2/3. Выберите безопасный preset расписания.",
                    _schedule_keyboard(),
                )
            weekday_mask, time_from, time_to = preset
            await set_owner_schedule(
                session,
                connection_id=connection.id,
                user_id=connection.owner_user_id,
                weekday_mask=weekday_mask,
                time_from=time_from,
                time_to=time_to,
                now=now,
            )
            return ControlResponse(
                "Шаг 3/3. В Chat Automation выберите Only Selected Chats "
                "и добавьте один тестовый контакт.",
                _scope_keyboard(),
            )
        if state == "scope":
            if choice == BUTTON_BACK:
                await set_onboarding_state(session, connection.owner_user_id, "schedule", now=now)
                return ControlResponse("Вернулись к выбору расписания.", _schedule_keyboard())
            if choice != BUTTON_SCOPE_CONFIRMED:
                return ControlResponse(
                    "Подтвердите Only Selected Chats только после настройки в Telegram.",
                    _scope_keyboard(),
                )
            missing = _missing_onboarding_rights(connection)
            if missing or not connection.policy.is_active:
                await set_onboarding_state(
                    session, connection.owner_user_id, "awaiting_connection", now=now
                )
                return ControlResponse(
                    "Права или подключение изменились. Проверьте Chat Automation ещё раз.",
                    _connection_keyboard(),
                )
            await complete_onboarding(
                session,
                connection_id=connection.id,
                user_id=connection.owner_user_id,
                now=now,
            )
            fresh = await self._fresh_connection(session, connection)
            return ControlResponse(
                "✅ Настройка завершена. Режим dry-run; "
                "live доступен только с ручным подтверждением.",
                _main_keyboard(fresh, now=now),
            )
        fresh = await self._fresh_connection(session, connection)
        return ControlResponse("Настройка уже завершена.", _main_keyboard(fresh, now=now))

    async def _execute(
        self,
        session: Any,
        connection: ConnectionRecord,
        *,
        command: str,
        argument: str,
        now: datetime,
        is_master: bool,
    ) -> ControlResponse:
        if command == "start":
            contact_id = _business_contact_id(argument)
            if contact_id is None:
                await cancel_live_confirmation(session, connection.id)
                await set_control_state(session, connection.id, "main")
                fresh = await self._fresh_connection(session, connection)
                return ControlResponse(
                    "Панель управления открыта. Для карточки контакта используйте Manage Bot.",
                    _main_keyboard(fresh, now=now, is_master=is_master),
                )
            await cancel_live_confirmation(session, connection.id)
            await set_control_state(session, connection.id, "main")
            card = await load_contact_card(session, connection.id, contact_id, now=now)
            return _render_contact_card(card, connection=connection)
        if command == "users":
            if not is_master:
                return ControlResponse("Недостаточно прав.")
            users = await list_access_users(session)
            return ControlResponse(_render_access_users(users), _access_users_keyboard(users))
        if command == "invite":
            if not is_master:
                return ControlResponse("Недостаточно прав.")
            token = await create_access_invite(
                session, created_by=connection.owner_user_id, now=now, ttl=INVITE_TTL
            )
            link = f"https://t.me/{self.bot_username}?start=invite_{token}"
            return ControlResponse(
                f"➕ Приглашение действует 24 часа и только один раз:\n{link}",
                _main_keyboard(connection, now=now, is_master=True),
            )
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
            return ControlResponse(
                "Главное меню.", _main_keyboard(fresh, now=now, is_master=is_master)
            )
        if command == "status":
            await cancel_live_confirmation(session, connection.id)
            await set_control_state(session, connection.id, "main")
            fresh = await self._fresh_connection(session, connection)
            return ControlResponse(
                _render_status(fresh, now=now),
                _main_keyboard(fresh, now=now, is_master=is_master),
            )
        if command == "off":
            await set_connection_control(session, connection.id, kill_switch=True, muted_until=None)
            await cancel_live_confirmation(session, connection.id)
            await set_control_state(session, connection.id, "main")
            fresh = await self._fresh_connection(session, connection)
            return ControlResponse(
                "⛔ Секретарь выключен. Уже запланированные ответы тоже остановлены.",
                _main_keyboard(fresh, now=now, is_master=is_master),
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
                _main_keyboard(fresh, now=now, is_master=is_master),
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
                _main_keyboard(fresh, now=now, is_master=is_master),
            )
        if command == "live":
            if not connection.dry_run:
                await set_control_state(session, connection.id, "main")
                return ControlResponse(
                    "Live-режим уже включён.",
                    _main_keyboard(connection, now=now, is_master=is_master),
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
            return ControlResponse(text, _main_keyboard(fresh, now=now, is_master=is_master))
        if command == "live_cancel":
            await cancel_live_confirmation(session, connection.id)
            await set_control_state(session, connection.id, "main")
            fresh = await self._fresh_connection(session, connection)
            return ControlResponse(
                "Dry-run сохранён. Live-режим не включён.",
                _main_keyboard(fresh, now=now, is_master=is_master),
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
            _main_keyboard(fresh, now=now, is_master=is_master),
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
        is_master: bool,
    ) -> ControlResponse:
        if action == "cancel":
            await cancel_live_confirmation(session, connection.id)
            await set_control_state(session, connection.id, "main")
            fresh = await self._fresh_connection(session, connection)
            return ControlResponse(
                "Dry-run сохранён. Live-режим не включён.",
                _main_keyboard(fresh, now=now, is_master=is_master),
            )
        enabled = await confirm_live_mode(session, connection.id, now=now)
        await set_control_state(session, connection.id, "main")
        fresh = await self._fresh_connection(session, connection)
        text = (
            "⚠️ Live-режим включён. Бот может отвечать контактам."
            if enabled
            else "Подтверждение истекло. Повторите включение live."
        )
        return ControlResponse(text, _main_keyboard(fresh, now=now, is_master=is_master))

    async def _access_action(
        self,
        session: Any,
        *,
        action: str,
        target_id: int,
        master_id: int,
        now: datetime,
    ) -> tuple[ControlResponse, tuple[int, str] | None, int | None]:
        cancel_connection_id = None
        if action == "approve":
            changed = await approve_access_user(
                session, user_id=target_id, approved_by=master_id, now=now
            )
            text = (
                "✅ Пользователь подтверждён. Ему отправлена инструкция подключения."
                if changed
                else "Состояние пользователя уже изменилось."
            )
            notification = (
                (
                    target_id,
                    "✅ Доступ подтверждён. Подключите бота в Chat Automation, "
                    "затем отправьте /start.",
                )
                if changed
                else None
            )
        else:
            target_connection = await load_owner_connection(session, target_id)
            changed = await revoke_access_user(
                session, user_id=target_id, revoked_by=master_id, now=now
            )
            text = (
                "⛔ Доступ пользователя отозван."
                if changed
                else "Пользователь не найден или защищён от отзыва."
            )
            notification = (
                (
                    target_id,
                    "⛔ Доступ к секретарю отозван мастером. Автоматические действия остановлены.",
                )
                if changed
                else None
            )
            if changed and target_connection is not None:
                cancel_connection_id = target_connection.id
        connection = await load_owner_connection(session, master_id)
        keyboard = (
            None if connection is None else _main_keyboard(connection, now=now, is_master=True)
        )
        return ControlResponse(text, keyboard), notification, cancel_connection_id


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
        BUTTON_USERS: ("users", ""),
        BUTTON_INVITE: ("invite", ""),
        BUTTON_USERS_REFRESH: ("users", ""),
        BUTTON_ADMIN_BACK: ("menu", ""),
    }
    return main_actions.get(choice)


def _invite_token(text: str) -> str | None:
    parsed = _parse_command(text)
    if parsed is None or parsed[0] != "start":
        return None
    prefix = "invite_"
    argument = parsed[1]
    if not argument.startswith(prefix):
        return None
    token = argument[len(prefix) :]
    return (
        token
        if 20 <= len(token) <= 48 and token.replace("-", "").replace("_", "").isalnum()
        else None
    )


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


def _parse_access_callback(data: str | None) -> tuple[str, int] | None:
    parts = (data or "").split(":")
    if (
        len(parts) != 3
        or parts[0] != "access"
        or parts[1] not in {"approve", "revoke"}
        or not parts[2].isdigit()
    ):
        return None
    user_id = int(parts[2])
    return (parts[1], user_id) if user_id > 0 else None


def _callback_feedback(
    contact: tuple[int, str, str | None] | None,
    live_action: str | None,
    access_action: tuple[str, int] | None,
) -> tuple[str, str]:
    if access_action is not None:
        action, _ = access_action
        return (
            ("✅ Обработано: доступ подтверждён", "Доступ подтверждён")
            if action == "approve"
            else ("✅ Обработано: доступ отозван", "Доступ отозван")
        )
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


def _render_access_users(users: list[AccessUserRecord]) -> str:
    lines = ["👥 Пользователи:"]
    labels = {"pending": "ожидает", "active": "активен", "revoked": "отозван"}
    for user in users:
        identity = f"@{user.username}" if user.username else f"ID {user.user_id}"
        role = "мастер" if user.role == "master" else labels[user.status]
        state = "" if user.role == "master" else f" · {user.onboarding_state}"
        lines.append(f"• {identity} — {role}{state}")
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


def _access_users_keyboard(users: list[AccessUserRecord]) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    for user in users:
        if user.role == "master" or user.status == "revoked":
            continue
        identity = f"@{user.username}" if user.username else str(user.user_id)
        if user.status == "pending":
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"✅ {identity}",
                        callback_data=f"access:approve:{user.user_id}",
                    ),
                    InlineKeyboardButton(
                        text=f"❌ {identity}",
                        callback_data=f"access:revoke:{user.user_id}",
                    ),
                ]
            )
        else:
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"⛔ Отозвать {identity}",
                        callback_data=f"access:revoke:{user.user_id}",
                    )
                ]
            )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _missing_onboarding_rights(connection: ConnectionRecord) -> list[str]:
    labels = {
        "can_reply": "ответы",
    }
    return [label for key, label in labels.items() if not connection.rights.get(key, False)]


def _connection_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BUTTON_RECHECK_CONNECTION)]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Подключите бота в Chat Automation",
    )


def _timezone_keyboard() -> ReplyKeyboardMarkup:
    buttons = [KeyboardButton(text=label) for label in TIMEZONE_BUTTONS]
    return ReplyKeyboardMarkup(
        keyboard=[buttons[:2], buttons[2:]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите часовой пояс",
    )


def _schedule_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=label)] for label in SCHEDULE_BUTTONS]
        + [[KeyboardButton(text=BUTTON_BACK)]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите расписание",
    )


def _scope_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BUTTON_SCOPE_CONFIRMED)],
            [KeyboardButton(text=BUTTON_BACK)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Подтвердите область чатов",
    )


def _main_keyboard(
    connection: ConnectionRecord, *, now: datetime, is_master: bool = False
) -> ReplyKeyboardMarkup:
    muted_until = connection.policy.muted_until
    stopped = connection.policy.kill_switch or (muted_until is not None and now < muted_until)
    power_button = BUTTON_ON if stopped else BUTTON_OFF
    live_button = BUTTON_LIVE if connection.dry_run else BUTTON_LIVE_ACTIVE
    rows = [
        [KeyboardButton(text=BUTTON_STATUS), KeyboardButton(text=BUTTON_TODAY)],
        [KeyboardButton(text=power_button), KeyboardButton(text=BUTTON_MUTE)],
        [KeyboardButton(text=live_button)],
    ]
    if is_master:
        rows.append([KeyboardButton(text=BUTTON_USERS), KeyboardButton(text=BUTTON_INVITE)])
    return ReplyKeyboardMarkup(
        keyboard=rows,
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
