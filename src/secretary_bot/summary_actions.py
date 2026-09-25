from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from aiogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import delete, select, update

from secretary_bot import models
from secretary_bot import texts as ui
from secretary_bot.actions import LogAction
from secretary_bot.callbacks import finalize_callback
from secretary_bot.daily_summary import summary_item_keyboard
from secretary_bot.delivery import send_once
from secretary_bot.identities import contact_label
from secretary_bot.retention import MESSAGE_RETENTION, MessageCipher, MessageContext
from secretary_bot.sender import BusinessReplySender, SendOutcome
from secretary_bot.storage import (
    ConnectionRecord,
    Database,
    capture_message,
    load_access_user,
    load_contact_state,
    load_owner_connection,
    log_decision,
    normalize_contact_username,
    record_owner_reply,
)
from secretary_bot.texts import as_bot_reply

REPLY_STATE_TTL = timedelta(minutes=15)


class SummaryActionBot(Protocol):
    async def send_message(self, **kwargs: Any) -> Any: ...

    async def answer_callback_query(self, callback_query_id: str, **kwargs: Any) -> Any: ...

    async def edit_message_text(self, **kwargs: Any) -> Any: ...

    async def edit_message_reply_markup(self, **kwargs: Any) -> Any: ...

    async def get_chat(self, chat_id: int | str) -> Any: ...


@dataclass(slots=True)
class SummaryActions:
    database: Database
    bot: SummaryActionBot
    sender: BusinessReplySender
    cipher: MessageCipher | None = None
    require_contact_setup: bool = True

    async def handle_callback(self, query: CallbackQuery, *, now: datetime | None = None) -> bool:
        direct = parse_direct_reply_callback(query.data)
        moment = now or datetime.now(UTC)
        if direct is not None:
            action, contact_id = direct
            if action == "select":
                return await self._request_direct_reply(query, contact_id=contact_id, now=moment)
            return await self._cancel_direct_reply(query)

        parsed = parse_summary_callback(query.data)
        if parsed is None:
            return False
        action, target_id = parsed
        if action == "resolve":
            return await self._resolve(query, item_id=target_id, now=moment)
        if action == "reply":
            return await self._request_reply(query, item_id=target_id, now=moment)
        if action == "open":
            return await self._open_chat(query, item_id=target_id)
        return await self._read_all(query, run_id=target_id)

    async def handle_message(self, message: Message, *, now: datetime | None = None) -> bool:
        sender = message.from_user
        if sender is None or message.chat.type != "private" or not message.text:
            return False
        moment = now or datetime.now(UTC)
        if message.text.strip() == ui.BUTTON_SEND_BOT:
            return await self._show_direct_reply_contacts(message)

        async with self.database.session() as session, session.begin():
            connection = await load_owner_connection(session, sender.id)
            if connection is None:
                return False
            direct_state = await session.get(models.DirectReplyState, connection.id)
            summary_state = await session.get(models.SummaryReplyState, connection.id)
            if direct_state is None and summary_state is None:
                return False
            if message.text.startswith("/"):
                if direct_state is not None:
                    await session.delete(direct_state)
                if summary_state is not None:
                    await session.delete(summary_state)
                return False

            replied_to = getattr(message.reply_to_message, "message_id", None)
            contact_id: int | None = None
            if direct_state is not None:
                if direct_state.expires_at <= moment:
                    await session.delete(direct_state)
                    expired = True
                elif (
                    direct_state.prompt_message_id is None
                    or replied_to != direct_state.prompt_message_id
                ):
                    return False
                else:
                    contact_id = direct_state.contact_id
                    expired = False
            elif summary_state is not None and summary_state.expires_at <= moment:
                await session.delete(summary_state)
                expired = True
            else:
                assert summary_state is not None
                if (
                    summary_state.prompt_message_id is None
                    or replied_to != summary_state.prompt_message_id
                ):
                    return False
                item = await session.get(models.SummaryItem, summary_state.summary_item_id)
                expired = item is None
                contact_id = None if item is None else item.contact_id

        if expired or contact_id is None:
            await self.bot.send_message(
                chat_id=sender.id,
                text="⌛ Запит на відповідь протерміновано. Запустіть надсилання ще раз.",
            )
            return True

        reply_text = message.text.strip()
        if not reply_text:
            return False
        text = as_bot_reply(reply_text)
        result = await send_once(
            self.database,
            self.sender,
            key=f"manual:{connection.id}:{contact_id}:{replied_to}",
            connection_id=connection.id,
            business_connection_id=connection.business_connection_id,
            chat_id=contact_id,
            text=text,
        )
        if not result.is_sent:
            failure = (
                "⚠️ Результат попереднього надсилання невідомий. Перевірте чат перед новою спробою."
                if result.error_code == "DELIVERY_UNCERTAIN"
                else "⚠️ 24-годинне вікно Telegram для цього контакту закрите. "
                "Попросіть клієнта надіслати нове повідомлення."
                if result.outcome is SendOutcome.CHAT_INACTIVE
                else "⚠️ Не вдалося надіслати відповідь. Спробуйте ще раз."
            )
            await self.bot.send_message(
                chat_id=sender.id,
                text=failure,
            )
            return True

        async with self.database.session() as session, session.begin():
            await log_decision(
                session,
                connection_id=connection.id,
                contact_id=contact_id,
                tg_message_id=result.message_id,
                direction="out",
                action=LogAction.REPLIED,
                occurred_at=moment,
            )
            await record_owner_reply(session, connection.id, contact_id, at=moment)
            contact = await load_contact_state(
                session, connection.id, contact_id, require_setup=self.require_contact_setup
            )
            # A contact the owner has not reviewed stays out of the daily summary.
            if (
                connection.message_retention_enabled
                and self.cipher is not None
                and not result.replayed
                and contact.configured
            ):
                context = MessageContext(connection.id, contact_id, result.message_id, "out")
                encrypted = self.cipher.encrypt(text, context=context)
                await capture_message(
                    session,
                    connection_id=connection.id,
                    contact_id=contact_id,
                    tg_message_id=result.message_id,
                    direction="out",
                    occurred_at=moment,
                    body_encrypted=encrypted,
                    retention_until=moment + MESSAGE_RETENTION,
                )
            await session.execute(
                delete(models.SummaryReplyState).where(
                    models.SummaryReplyState.connection_id == connection.id
                )
            )
            await session.execute(
                delete(models.DirectReplyState).where(
                    models.DirectReplyState.connection_id == connection.id
                )
            )
        await self.bot.send_message(chat_id=sender.id, text="✅ Відповідь надіслано від бота.")
        return True

    async def _resolve(self, query: CallbackQuery, *, item_id: int, now: datetime) -> bool:
        async with self.database.session() as session, session.begin():
            target = await _owned_item(session, owner_user_id=query.from_user.id, item_id=item_id)
            if target is None:
                return False
            changed = await session.scalar(
                update(models.SummaryItem)
                .where(
                    models.SummaryItem.id == item_id,
                    models.SummaryItem.resolved_at.is_(None),
                )
                .values(resolved_at=now)
                .returning(models.SummaryItem.id)
            )
        if changed is None:
            await self.bot.answer_callback_query(query.id, text="Уже вирішено")
            if query.message is not None:
                with contextlib.suppress(Exception):
                    await self.bot.edit_message_reply_markup(
                        chat_id=query.message.chat.id,
                        message_id=query.message.message_id,
                        reply_markup=None,
                    )
        else:
            await finalize_callback(
                self.bot, query, note="✅ Вирішено", toast="Позначено як вирішене"
            )
        return True

    async def _request_reply(self, query: CallbackQuery, *, item_id: int, now: datetime) -> bool:
        async with self.database.session() as session, session.begin():
            target = await _owned_item(session, owner_user_id=query.from_user.id, item_id=item_id)
            if target is None:
                return False
            item, connection = target
            await session.execute(
                delete(models.DirectReplyState).where(
                    models.DirectReplyState.connection_id == connection.id
                )
            )
            state = await session.get(models.SummaryReplyState, connection.id)
            if state is None:
                state = models.SummaryReplyState(
                    connection_id=connection.id,
                    summary_item_id=item.id,
                    expires_at=now + REPLY_STATE_TTL,
                )
                session.add(state)
            else:
                state.summary_item_id = item.id
                state.prompt_message_id = None
                state.expires_at = now + REPLY_STATE_TTL
        prompt = await self.bot.send_message(
            chat_id=query.from_user.id,
            text=(
                "✍️ Напишіть відповідь для "
                f"{contact_label(item.contact_name, item.contact_username)}. "
                "Відповідь на цей запит одразу надійде контакту, навіть у тестовому режимі."
            ),
            reply_markup=ForceReply(
                selective=True,
                input_field_placeholder="Відповідь буде надіслана від бота",
            ),
        )
        async with self.database.session() as session, session.begin():
            state = await session.get(models.SummaryReplyState, connection.id)
            if state is not None and state.summary_item_id == item.id:
                state.prompt_message_id = getattr(prompt, "message_id", None)
        await finalize_callback(
            self.bot,
            query,
            note="✍️ Очікується відповідь у приватному чаті з ботом",
            toast="Форму відповіді відкрито",
        )
        return True

    async def _show_direct_reply_contacts(self, message: Message) -> bool:
        assert message.from_user is not None
        async with self.database.session() as session, session.begin():
            access = await load_access_user(session, message.from_user.id)
            connection = await load_owner_connection(session, message.from_user.id)
            if access is None or not access.can_process or connection is None:
                return False
            if not connection.rights.get("can_reply", False):
                response_text = ui.DIRECT_REPLY_NO_PERMISSION
                keyboard = None
            else:
                contacts = list(
                    await session.scalars(
                        select(models.ContactActivity)
                        .where(
                            models.ContactActivity.connection_id == connection.id,
                            models.ContactActivity.last_incoming_at.is_not(None),
                        )
                        .order_by(models.ContactActivity.last_incoming_at.desc())
                        .limit(20)
                    )
                )
                response_text = ui.DIRECT_REPLY_SELECT if contacts else ui.DIRECT_REPLY_NO_CONTACTS
                keyboard = _direct_reply_contacts_keyboard(contacts) if contacts else None
                await session.execute(
                    delete(models.DirectReplyState).where(
                        models.DirectReplyState.connection_id == connection.id
                    )
                )
                await session.execute(
                    delete(models.SummaryReplyState).where(
                        models.SummaryReplyState.connection_id == connection.id
                    )
                )

        await self.bot.send_message(
            chat_id=message.chat.id,
            text=response_text,
            **({"reply_markup": keyboard} if keyboard is not None else {}),
        )
        return True

    async def _request_direct_reply(
        self, query: CallbackQuery, *, contact_id: int, now: datetime
    ) -> bool:
        async with self.database.session() as session, session.begin():
            access = await load_access_user(session, query.from_user.id)
            connection = await load_owner_connection(session, query.from_user.id)
            if access is None or not access.can_process or connection is None:
                return False
            if not connection.rights.get("can_reply", False):
                await self.bot.answer_callback_query(
                    query.id, text=ui.DIRECT_REPLY_NO_PERMISSION, show_alert=True
                )
                return True
            contact = await session.get(models.ContactActivity, (connection.id, contact_id))
            if contact is None or contact.last_incoming_at is None:
                return False
            await session.execute(
                delete(models.SummaryReplyState).where(
                    models.SummaryReplyState.connection_id == connection.id
                )
            )
            state = await session.get(models.DirectReplyState, connection.id)
            if state is None:
                state = models.DirectReplyState(
                    connection_id=connection.id,
                    contact_id=contact_id,
                    expires_at=now + REPLY_STATE_TTL,
                )
                session.add(state)
            else:
                state.contact_id = contact_id
                state.prompt_message_id = None
                state.expires_at = now + REPLY_STATE_TTL
            selected_label = contact_label(contact.contact_name, contact.contact_username)

        prompt = await self.bot.send_message(
            chat_id=query.from_user.id,
            text=(
                f"✍️ Напишіть повідомлення для {selected_label}. "
                "Відповідь на цей запит одразу надійде контакту, навіть у тестовому режимі."
            ),
            reply_markup=ForceReply(
                selective=True,
                input_field_placeholder="Повідомлення буде надіслане від бота",
            ),
        )
        async with self.database.session() as session, session.begin():
            state = await session.get(models.DirectReplyState, connection.id)
            if state is not None and state.contact_id == contact_id:
                state.prompt_message_id = getattr(prompt, "message_id", None)
        await finalize_callback(
            self.bot,
            query,
            note=f"✍️ Обрано: {selected_label}",
            toast="Форму повідомлення відкрито",
        )
        return True

    async def _cancel_direct_reply(self, query: CallbackQuery) -> bool:
        async with self.database.session() as session, session.begin():
            access = await load_access_user(session, query.from_user.id)
            connection = await load_owner_connection(session, query.from_user.id)
            if access is None or not access.can_process or connection is None:
                return False
            await session.execute(
                delete(models.DirectReplyState).where(
                    models.DirectReplyState.connection_id == connection.id
                )
            )
        await finalize_callback(
            self.bot,
            query,
            note=ui.DIRECT_REPLY_CANCELLED,
            toast=ui.DIRECT_REPLY_CANCELLED,
        )
        return True

    async def _read_all(self, query: CallbackQuery, *, run_id: int) -> bool:
        async with self.database.session() as session:
            connection = await _owned_run_connection(
                session, owner_user_id=query.from_user.id, run_id=run_id
            )
            if connection is None:
                return False
            can_read = connection.rights.get("can_read_messages", False)
            items = (
                list(
                    await session.scalars(
                        select(models.SummaryItem).where(
                            models.SummaryItem.run_id == run_id,
                            models.SummaryItem.last_incoming_message_id.is_not(None),
                        )
                    )
                )
                if can_read
                else []
            )

        if not can_read:
            await self.bot.answer_callback_query(
                query.id, text="Немає права позначати повідомлення прочитаними"
            )
            return True

        read = 0
        seen: set[int] = set()
        for item in items:
            if item.contact_id in seen or item.last_incoming_message_id is None:
                continue
            seen.add(item.contact_id)
            read += int(
                await self.sender.mark_read(
                    business_connection_id=connection.business_connection_id,
                    chat_id=item.contact_id,
                    message_id=item.last_incoming_message_id,
                )
            )
        await finalize_callback(
            self.bot,
            query,
            note=f"👁 Прочитано діалогів: {read}",
            toast=f"Прочитано: {read}",
        )
        return True

    async def _open_chat(self, query: CallbackQuery, *, item_id: int) -> bool:
        async with self.database.session() as session:
            target = await _owned_item(session, owner_user_id=query.from_user.id, item_id=item_id)
            if target is None:
                return False
            item, _ = target
            contact_id = item.contact_id

        try:
            chat = await self.bot.get_chat(contact_id)
            username = normalize_contact_username(getattr(chat, "username", None))
        except Exception:
            username = None

        if username is None:
            await self.bot.answer_callback_query(
                query.id,
                text=(
                    "У контакту немає публічного username. Прямий перехід "
                    "недоступний — скористайтеся «Відповісти від бота»."
                ),
                show_alert=True,
            )
            return True

        async with self.database.session() as session, session.begin():
            target = await _owned_item(session, owner_user_id=query.from_user.id, item_id=item_id)
            if target is None:
                return False
            item, connection = target
            item.contact_username = username
            activity = await session.get(models.ContactActivity, (connection.id, item.contact_id))
            if activity is not None:
                activity.contact_username = username

        if query.message is not None:
            with contextlib.suppress(Exception):
                await self.bot.edit_message_reply_markup(
                    chat_id=query.message.chat.id,
                    message_id=query.message.message_id,
                    reply_markup=summary_item_keyboard(item_id, username),
                )
        await self.bot.answer_callback_query(
            query.id,
            text="Посилання оновлено. Натисніть «Перейти в чат» ще раз.",
        )
        return True


def parse_summary_callback(data: str | None) -> tuple[str, int] | None:
    parts = (data or "").split(":")
    if (
        len(parts) != 3
        or parts[0] != "summary"
        or parts[1] not in {"resolve", "reply", "read", "open"}
        or not parts[2].isdigit()
    ):
        return None
    target_id = int(parts[2])
    return (parts[1], target_id) if target_id > 0 else None


def parse_direct_reply_callback(data: str | None) -> tuple[str, int] | None:
    parts = (data or "").split(":")
    if (
        len(parts) != 3
        or parts[0] != "direct"
        or parts[1] not in {"select", "cancel"}
        or not parts[2].isdigit()
    ):
        return None
    target_id = int(parts[2])
    if parts[1] == "cancel":
        return ("cancel", 0) if target_id == 0 else None
    return ("select", target_id) if target_id > 0 else None


def _direct_reply_contacts_keyboard(
    contacts: list[models.ContactActivity],
) -> InlineKeyboardMarkup:
    rows = []
    for contact in contacts:
        name = contact_label(contact.contact_name, contact.contact_username)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"👤 {name}"[:60],
                    callback_data=f"direct:select:{contact.contact_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Скасувати", callback_data="direct:cancel:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _owned_item(
    session: Any, *, owner_user_id: int, item_id: int
) -> tuple[models.SummaryItem, ConnectionRecord] | None:
    access = await load_access_user(session, owner_user_id)
    connection = await load_owner_connection(session, owner_user_id)
    if access is None or not access.can_process or connection is None:
        return None
    item = await session.scalar(
        select(models.SummaryItem)
        .join(models.SummaryRun, models.SummaryRun.id == models.SummaryItem.run_id)
        .where(
            models.SummaryItem.id == item_id,
            models.SummaryRun.connection_id == connection.id,
        )
    )
    return None if item is None else (item, connection)


async def _owned_run_connection(
    session: Any, *, owner_user_id: int, run_id: int
) -> ConnectionRecord | None:
    access = await load_access_user(session, owner_user_id)
    connection = await load_owner_connection(session, owner_user_id)
    if access is None or not access.can_process or connection is None:
        return None
    run = await session.scalar(
        select(models.SummaryRun.id).where(
            models.SummaryRun.id == run_id,
            models.SummaryRun.connection_id == connection.id,
        )
    )
    return connection if run is not None else None
