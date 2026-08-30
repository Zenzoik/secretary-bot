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

from secretary_bot import texts as ui
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

BUTTON_STATUS = ui.BUTTON_STATUS
BUTTON_TODAY = ui.BUTTON_TODAY
BUTTON_OFF = ui.BUTTON_OFF
BUTTON_ON = ui.BUTTON_ON
BUTTON_MUTE = ui.BUTTON_MUTE
BUTTON_LIVE = ui.BUTTON_LIVE
BUTTON_LIVE_ACTIVE = ui.BUTTON_LIVE_ACTIVE
BUTTON_BACK = ui.BUTTON_BACK
BUTTON_LIVE_CONFIRM = ui.BUTTON_LIVE_CONFIRM
BUTTON_CANCEL = ui.BUTTON_CANCEL
BUTTON_USERS = ui.BUTTON_USERS
BUTTON_INVITE = ui.BUTTON_INVITE
BUTTON_USERS_REFRESH = ui.BUTTON_USERS_REFRESH
BUTTON_ADMIN_BACK = ui.BUTTON_ADMIN_BACK
BUTTON_SCOPE_CONFIRMED = ui.BUTTON_SCOPE_CONFIRMED
BUTTON_RECHECK_CONNECTION = ui.BUTTON_RECHECK_CONNECTION
TIMEZONE_BUTTONS = ui.TIMEZONE_LABELS
SCHEDULE_BUTTONS = {
    ui.SCHEDULE_LABELS[0]: (127, time(22, 0), time(8, 0)),
    ui.SCHEDULE_LABELS[1]: (31, time(18, 0), time(9, 0)),
    ui.SCHEDULE_LABELS[2]: (127, time(0, 0), time(23, 59)),
}
MUTE_BUTTONS = ui.MUTE_LABELS


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
                    response = ControlResponse(ui.INVITE_INVALID)
                elif pending.status == "pending":
                    response = ControlResponse(ui.INVITE_REQUESTED)
                    if pending.invited_by is not None:
                        master_notification = (pending.invited_by, ui.MASTER_ACCESS_REQUEST)
                else:
                    response = ControlResponse(ui.ACCESS_ALREADY_ACTIVE)
                connection = None
                access = pending
            else:
                access = await load_access_user(session, sender.id)
                if access is None:
                    return False
                if access.status == "pending":
                    response = ControlResponse(ui.ACCESS_PENDING)
                    connection = None
                elif access.status == "revoked":
                    response = ControlResponse(ui.ACCESS_REVOKED)
                    connection = None
                else:
                    connection = await load_owner_connection(session, sender.id)

            if connection is None:
                if invite_token is None and access is not None and access.status == "active":
                    response = ControlResponse(ui.CONNECT_BOT, _connection_keyboard())
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
                    ui.CONNECTION_OFF,
                    _connection_keyboard(),
                )
            missing = _missing_onboarding_rights(connection)
            if missing:
                return ControlResponse(ui.missing_rights(missing), _connection_keyboard())
            await set_onboarding_state(session, connection.owner_user_id, "timezone", now=now)
            return ControlResponse(ui.ONBOARDING_TIMEZONE, _timezone_keyboard())
        if state == "timezone":
            timezone = TIMEZONE_BUTTONS.get(choice)
            if timezone is None:
                return ControlResponse(ui.ONBOARDING_TIMEZONE_BUTTON, _timezone_keyboard())
            await set_owner_timezone(
                session,
                connection_id=connection.id,
                user_id=connection.owner_user_id,
                timezone=timezone,
                now=now,
            )
            return ControlResponse(ui.timezone_selected(timezone), _schedule_keyboard())
        if state == "schedule":
            if choice == BUTTON_BACK:
                await set_onboarding_state(session, connection.owner_user_id, "timezone", now=now)
                return ControlResponse(ui.ONBOARDING_BACK_TIMEZONE, _timezone_keyboard())
            preset = SCHEDULE_BUTTONS.get(choice)
            if preset is None:
                return ControlResponse(ui.ONBOARDING_SCHEDULE, _schedule_keyboard())
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
            return ControlResponse(ui.ONBOARDING_SCOPE, _scope_keyboard())
        if state == "scope":
            if choice == BUTTON_BACK:
                await set_onboarding_state(session, connection.owner_user_id, "schedule", now=now)
                return ControlResponse(ui.ONBOARDING_BACK_SCHEDULE, _schedule_keyboard())
            if choice != BUTTON_SCOPE_CONFIRMED:
                return ControlResponse(ui.ONBOARDING_SCOPE_CONFIRM, _scope_keyboard())
            missing = _missing_onboarding_rights(connection)
            if missing or not connection.policy.is_active:
                await set_onboarding_state(
                    session, connection.owner_user_id, "awaiting_connection", now=now
                )
                return ControlResponse(ui.ONBOARDING_CONNECTION_CHANGED, _connection_keyboard())
            await complete_onboarding(
                session,
                connection_id=connection.id,
                user_id=connection.owner_user_id,
                now=now,
            )
            fresh = await self._fresh_connection(session, connection)
            return ControlResponse(ui.ONBOARDING_DONE, _main_keyboard(fresh, now=now))
        fresh = await self._fresh_connection(session, connection)
        return ControlResponse(ui.ONBOARDING_ALREADY_DONE, _main_keyboard(fresh, now=now))

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
                    ui.PANEL_OPEN, _main_keyboard(fresh, now=now, is_master=is_master)
                )
            await cancel_live_confirmation(session, connection.id)
            await set_control_state(session, connection.id, "main")
            card = await load_contact_card(session, connection.id, contact_id, now=now)
            return _render_contact_card(card, connection=connection)
        if command == "users":
            if not is_master:
                return ControlResponse(ui.FORBIDDEN)
            users = await list_access_users(session)
            return ControlResponse(_render_access_users(users), _access_users_keyboard(users))
        if command == "invite":
            if not is_master:
                return ControlResponse(ui.FORBIDDEN)
            token = await create_access_invite(
                session, created_by=connection.owner_user_id, now=now, ttl=INVITE_TTL
            )
            link = f"https://t.me/{self.bot_username}?start=invite_{token}"
            return ControlResponse(
                ui.invite_link(link),
                _main_keyboard(connection, now=now, is_master=True),
            )
        if command == "state_help":
            if connection.control_state == "mute_hours":
                return ControlResponse(ui.MUTE_SELECT, _mute_keyboard())
            return ControlResponse(ui.LIVE_SELECT, _live_confirmation_keyboard())
        if command == "menu":
            await cancel_live_confirmation(session, connection.id)
            await set_control_state(session, connection.id, "main")
            fresh = await self._fresh_connection(session, connection)
            return ControlResponse(
                ui.MAIN_MENU, _main_keyboard(fresh, now=now, is_master=is_master)
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
                ui.SECRETARY_OFF,
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
                ui.SECRETARY_ON,
                _main_keyboard(fresh, now=now, is_master=is_master),
            )
        if command == "mute":
            if not argument:
                await set_control_state(session, connection.id, "mute_hours")
                return ControlResponse(ui.MUTE_QUESTION, _mute_keyboard())
            hours = _mute_hours(argument)
            if hours is None:
                await set_control_state(session, connection.id, "mute_hours")
                return ControlResponse(
                    ui.invalid_mute_hours(MAX_MUTE_HOURS),
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
                ui.muted_until(local_until, hours),
                _main_keyboard(fresh, now=now, is_master=is_master),
            )
        if command == "live":
            if not connection.dry_run:
                await set_control_state(session, connection.id, "main")
                return ControlResponse(
                    ui.LIVE_ALREADY,
                    _main_keyboard(connection, now=now, is_master=is_master),
                )
            await request_live_confirmation(
                session, connection.id, until=now + LIVE_CONFIRMATION_TTL
            )
            await set_control_state(session, connection.id, "live_confirm")
            return ControlResponse(
                ui.LIVE_PROMPT,
                _live_confirmation_keyboard(),
            )
        if command == "live_confirm":
            enabled = await confirm_live_mode(session, connection.id, now=now)
            await set_control_state(session, connection.id, "main")
            fresh = await self._fresh_connection(session, connection)
            text = ui.LIVE_ENABLED if enabled else ui.LIVE_EXPIRED
            return ControlResponse(text, _main_keyboard(fresh, now=now, is_master=is_master))
        if command == "live_cancel":
            await cancel_live_confirmation(session, connection.id)
            await set_control_state(session, connection.id, "main")
            fresh = await self._fresh_connection(session, connection)
            return ControlResponse(
                ui.DRY_RUN_SAVED,
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
            return ControlResponse(ui.CONTACT_EXCLUDED)
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
            return ControlResponse(ui.contact_excluded_until(until))
        if action == "templates":
            return ControlResponse(ui.CONTACT_TEMPLATE_PROMPT, _template_keyboard(contact_id))
        if action == "template" and argument in {code.value for code in TemplateCode}:
            code = TemplateCode(argument)
            await set_contact_template_override(
                session,
                connection.id,
                contact_id,
                template_code=code.value,
                template_text=DEFAULT_TEMPLATES[code],
            )
            return ControlResponse(ui.contact_template_selected(code.value))
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
                ui.DRY_RUN_SAVED,
                _main_keyboard(fresh, now=now, is_master=is_master),
            )
        enabled = await confirm_live_mode(session, connection.id, now=now)
        await set_control_state(session, connection.id, "main")
        fresh = await self._fresh_connection(session, connection)
        text = ui.LIVE_ENABLED if enabled else ui.LIVE_EXPIRED_RETRY
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
            text = ui.USER_APPROVED if changed else ui.USER_STATE_CHANGED
            notification = (
                (
                    target_id,
                    ui.USER_APPROVED_NOTICE,
                )
                if changed
                else None
            )
        else:
            target_connection = await load_owner_connection(session, target_id)
            changed = await revoke_access_user(
                session, user_id=target_id, revoked_by=master_id, now=now
            )
            text = ui.USER_REVOKED if changed else ui.USER_NOT_REVOCABLE
            notification = (
                (
                    target_id,
                    ui.USER_REVOKED_NOTICE,
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
    return ui.callback_feedback(contact, live_action, access_action)


def _render_status(connection: ConnectionRecord, *, now: datetime) -> str:
    return ui.render_status(connection, now=now)


def _render_today(counts: list[tuple[str, str | None, int]], *, local_date: str) -> str:
    return ui.render_today(counts, local_date=local_date)


def _render_access_users(users: list[AccessUserRecord]) -> str:
    return ui.render_access_users(users)


def _render_contact_card(
    card: ContactCardRecord, *, connection: ConnectionRecord
) -> ControlResponse:
    text = ui.render_contact_card(card, timezone=connection.policy.timezone)
    return ControlResponse(text, _contact_keyboard(card.contact_id))


def _contact_keyboard(contact_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=ui.CONTACT_EXCLUDE_BUTTON,
                    callback_data=f"contact:{contact_id}:exclude",
                )
            ],
            [
                InlineKeyboardButton(
                    text=ui.CONTACT_TODAY_BUTTON,
                    callback_data=f"contact:{contact_id}:today",
                )
            ],
            [
                InlineKeyboardButton(
                    text=ui.CONTACT_TEMPLATE_BUTTON,
                    callback_data=f"contact:{contact_id}:templates",
                )
            ],
            [
                InlineKeyboardButton(
                    text=ui.CONTACT_OK_BUTTON, callback_data=f"contact:{contact_id}:ok"
                )
            ],
        ]
    )


def _template_keyboard(contact_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=ui.TEMPLATE_GENERAL_BUTTON,
                    callback_data=f"contact:{contact_id}:template:off_hours_default",
                )
            ],
            [
                InlineKeyboardButton(
                    text=ui.TEMPLATE_MONEY_BUTTON,
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
                        text=f"{ui.ACCESS_REVOKE_BUTTON} {identity}",
                        callback_data=f"access:revoke:{user.user_id}",
                    )
                ]
            )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def _missing_onboarding_rights(connection: ConnectionRecord) -> list[str]:
    labels = {
        "can_reply": ui.RIGHT_REPLY,
    }
    return [label for key, label in labels.items() if not connection.rights.get(key, False)]


def _connection_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BUTTON_RECHECK_CONNECTION)]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=ui.PLACEHOLDER_CONNECTION,
    )


def _timezone_keyboard() -> ReplyKeyboardMarkup:
    buttons = [KeyboardButton(text=label) for label in TIMEZONE_BUTTONS]
    return ReplyKeyboardMarkup(
        keyboard=[buttons[:2], buttons[2:]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=ui.PLACEHOLDER_TIMEZONE,
    )


def _schedule_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=label)] for label in SCHEDULE_BUTTONS]
        + [[KeyboardButton(text=BUTTON_BACK)]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=ui.PLACEHOLDER_SCHEDULE,
    )


def _scope_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BUTTON_SCOPE_CONFIRMED)],
            [KeyboardButton(text=BUTTON_BACK)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=ui.PLACEHOLDER_SCOPE,
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
        input_field_placeholder=ui.PLACEHOLDER_MAIN,
    )


def _mute_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=label) for label in list(MUTE_BUTTONS)[:2]],
            [KeyboardButton(text=label) for label in list(MUTE_BUTTONS)[2:]],
            [KeyboardButton(text=BUTTON_BACK)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder=ui.PLACEHOLDER_MUTE,
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
        input_field_placeholder=ui.PLACEHOLDER_LIVE,
    )
