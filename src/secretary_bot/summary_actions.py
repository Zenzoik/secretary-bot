from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from aiogram.types import CallbackQuery, ForceReply, Message
from sqlalchemy import delete, select, update

from secretary_bot import models
from secretary_bot.actions import LogAction
from secretary_bot.callbacks import finalize_callback
from secretary_bot.daily_summary import summary_item_keyboard
from secretary_bot.retention import MESSAGE_RETENTION, MessageCipher, MessageContext
from secretary_bot.sender import BusinessReplySender
from secretary_bot.storage import (
    ConnectionRecord,
    Database,
    capture_message,
    load_access_user,
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

    async def handle_callback(
        self, query: CallbackQuery, *, now: datetime | None = None
    ) -> bool:
        parsed = parse_summary_callback(query.data)
        if parsed is None:
            return False
        action, target_id = parsed
        moment = now or datetime.now(UTC)
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
        async with self.database.session() as session, session.begin():
            connection = await load_owner_connection(session, sender.id)
            if connection is None:
                return False
            state = await session.get(models.SummaryReplyState, connection.id)
            if state is None:
                return False
            if message.text.startswith("/"):
                await session.delete(state)
                return False
            if state.expires_at <= moment:
                await session.delete(state)
                expired = True
                item = None
            else:
                replied_to = getattr(message.reply_to_message, "message_id", None)
                if state.prompt_message_id is None or replied_to != state.prompt_message_id:
                    return False
                item = await session.get(models.SummaryItem, state.summary_item_id)
                expired = item is None

        if expired or item is None:
            await self.bot.send_message(
                chat_id=sender.id,
                text="⌛ Запит на відповідь протерміновано. Натисніть кнопку в новому самарі.",
            )
            return True

        reply_text = message.text.strip()
        if not reply_text:
            return False
        text = as_bot_reply(reply_text)
        result = await self.sender.send(
            business_connection_id=connection.business_connection_id,
            chat_id=item.contact_id,
            text=text,
        )
        if not result.is_sent:
            await self.bot.send_message(
                chat_id=sender.id,
                text="⚠️ Не вдалося надіслати відповідь. Спробуйте ще раз.",
            )
            return True

        async with self.database.session() as session, session.begin():
            await log_decision(
                session,
                connection_id=connection.id,
                contact_id=item.contact_id,
                tg_message_id=result.message_id,
                direction="out",
                action=LogAction.REPLIED,
                occurred_at=moment,
            )
            await record_owner_reply(session, connection.id, item.contact_id, at=moment)
            if connection.message_retention_enabled and self.cipher is not None:
                context = MessageContext(
                    connection.id, item.contact_id, result.message_id, "out"
                )
                encrypted = self.cipher.encrypt(text, context=context)
                await capture_message(
                    session,
                    connection_id=connection.id,
                    contact_id=item.contact_id,
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

    async def _request_reply(
        self, query: CallbackQuery, *, item_id: int, now: datetime
    ) -> bool:
        async with self.database.session() as session, session.begin():
            target = await _owned_item(session, owner_user_id=query.from_user.id, item_id=item_id)
            if target is None:
                return False
            item, connection = target
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
            text=f"✍️ Напишіть відповідь для {item.contact_name or f'ID {item.contact_id}' }.",
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
            target = await _owned_item(
                session, owner_user_id=query.from_user.id, item_id=item_id
            )
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
            target = await _owned_item(
                session, owner_user_id=query.from_user.id, item_id=item_id
            )
            if target is None:
                return False
            item, connection = target
            item.contact_username = username
            activity = await session.get(
                models.ContactActivity, (connection.id, item.contact_id)
            )
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
