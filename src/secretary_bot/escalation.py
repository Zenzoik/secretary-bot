from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import update

from secretary_bot import models
from secretary_bot.sender import BusinessReplySender
from secretary_bot.storage import Database, load_owner_connection, normalize_contact_username
from secretary_bot.texts import as_bot_reply


class EscalationBot(Protocol):
    async def send_message(self, **kwargs: Any) -> Any: ...

    async def answer_callback_query(self, callback_query_id: str, **kwargs: Any) -> Any: ...

    async def edit_message_reply_markup(self, **kwargs: Any) -> Any: ...

    async def edit_message_text(self, **kwargs: Any) -> Any: ...

    async def get_chat(self, chat_id: int | str) -> Any: ...


def escalation_offer_keyboard(request_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚨 Терміново",
                    callback_data=f"escalation:offer:{request_id}",
                )
            ]
        ]
    )


def escalation_confirmation_keyboard(
    request_id: int, *, amount: Decimal, currency: str
) -> InlineKeyboardMarkup:
    price = _price(amount, currency)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"💳 Підтвердити · {price}",
                    callback_data=f"escalation:confirm:{request_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Скасувати",
                    callback_data=f"escalation:cancel:{request_id}",
                )
            ],
        ]
    )


def escalation_owner_keyboard(
    request_id: int, *, contact_username: str | None
) -> InlineKeyboardMarkup:
    open_button = (
        InlineKeyboardButton(text="💬 Перейти в чат", url=f"https://t.me/{contact_username}")
        if contact_username
        else InlineKeyboardButton(
            text="💬 Перейти в чат",
            callback_data=f"escalation:open:{request_id}",
        )
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [open_button],
            [
                InlineKeyboardButton(
                    text="⛔ Відмовити",
                    callback_data=f"escalation:decline:{request_id}",
                )
            ],
        ]
    )


@dataclass(slots=True)
class EscalationActions:
    database: Database
    bot: EscalationBot
    sender: BusinessReplySender

    async def handle_callback(
        self, query: CallbackQuery, *, now: datetime | None = None
    ) -> bool:
        parsed = parse_escalation_callback(query.data)
        if parsed is None:
            return False
        action, request_id = parsed
        moment = now or datetime.now(UTC)
        if action == "offer":
            return await self._offer(query, request_id=request_id, now=moment)
        if action == "confirm":
            return await self._confirm(query, request_id=request_id, now=moment)
        if action == "cancel":
            return await self._cancel(query, request_id=request_id, now=moment)
        if action == "decline":
            return await self._decline(query, request_id=request_id, now=moment)
        return await self._open(query, request_id=request_id)

    async def _offer(self, query: CallbackQuery, *, request_id: int, now: datetime) -> bool:
        async with self.database.session() as session, session.begin():
            request = await session.get(models.ContactRequest, request_id)
            if request is None or request.contact_id != query.from_user.id:
                return False
            connection = await session.get(models.Connection, request.connection_id)
            if connection is None:
                return False
            if not connection.escalation_enabled or connection.escalation_price_amount <= 0:
                await self._answer(
                    query, "Платні термінові звернення зараз вимкнені.", alert=True
                )
                return True
            if request.status == "paid":
                await self._answer(query, "Це звернення вже підтверджено як платне.")
                return True
            if request.status == "offered":
                await self._answer(query, "Підтвердження вже відкрите нижче.")
                await self._clear_keyboard(query, connection.business_connection_id)
                return True
            if request.offer_expires_at <= now:
                await self._answer(
                    query, "Час підтвердження минув. Надішліть нове повідомлення.", alert=True
                )
                await self._clear_keyboard(query, connection.business_connection_id)
                return True
            request.status = "offered"
            request.price_amount = connection.escalation_price_amount
            request.currency = connection.escalation_currency
            offer_text = connection.escalation_offer_text
            business_connection_id = connection.business_connection_id
            contact_id = request.contact_id
            amount = request.price_amount
            currency = request.currency

        assert amount is not None and currency is not None
        result = await self.sender.send(
            business_connection_id=business_connection_id,
            chat_id=contact_id,
            text=as_bot_reply(
                f"{offer_text}\n\nВартість: {_price(amount, currency)}. "
                "Підтвердіть платне звернення."
            ),
            reply_markup=escalation_confirmation_keyboard(
                request_id, amount=amount, currency=currency
            ),
        )
        if not result.is_sent:
            async with self.database.session() as session, session.begin():
                await session.execute(
                    update(models.ContactRequest)
                    .where(
                        models.ContactRequest.id == request_id,
                        models.ContactRequest.status == "offered",
                    )
                    .values(status="normal", price_amount=None, currency=None)
                )
            await self._answer(
                query, "Не вдалося відкрити підтвердження. Спробуйте ще раз.", alert=True
            )
            return True
        await self._clear_keyboard(query, business_connection_id)
        await self._answer(query, "Перевірте вартість і підтвердьте звернення.")
        return True

    async def _confirm(
        self, query: CallbackQuery, *, request_id: int, now: datetime
    ) -> bool:
        async with self.database.session() as session, session.begin():
            request = await session.get(models.ContactRequest, request_id)
            if request is None or request.contact_id != query.from_user.id:
                return False
            connection = await session.get(models.Connection, request.connection_id)
            if connection is None:
                return False
            if request.status == "paid":
                await self._answer(query, "Платне звернення вже підтверджено.")
                return True
            if not connection.escalation_enabled:
                await self._answer(
                    query, "Платні термінові звернення вже вимкнені.", alert=True
                )
                await self._clear_keyboard(query, connection.business_connection_id)
                return True
            if request.offer_expires_at <= now:
                await self._answer(
                    query, "Час підтвердження минув. Надішліть нове повідомлення.", alert=True
                )
                await self._clear_keyboard(query, connection.business_connection_id)
                return True
            if request.status != "offered" or request.price_amount is None:
                await self._answer(query, "Спочатку відкрийте умови платного звернення.")
                return True

            claimed = await session.scalar(
                update(models.ContactRequest)
                .where(
                    models.ContactRequest.id == request_id,
                    models.ContactRequest.status == "offered",
                    models.ContactRequest.offer_expires_at > now,
                )
                .values(status="paid", paid_at=now, owner_decision="pending")
                .returning(models.ContactRequest.id)
            )
            if claimed is None:
                await self._answer(query, "Платне звернення вже оброблено.")
                return True
            await session.execute(
                update(models.ContactActivity)
                .where(
                    models.ContactActivity.connection_id == request.connection_id,
                    models.ContactActivity.contact_id == request.contact_id,
                )
                .values(
                    paid_escalation_count=models.ContactActivity.paid_escalation_count + 1
                )
            )
            activity = await session.get(
                models.ContactActivity, (request.connection_id, request.contact_id)
            )
            owner_chat_id = connection.owner_chat_id
            business_connection_id = connection.business_connection_id
            confirm_text = connection.escalation_confirm_text
            amount = request.price_amount
            currency = request.currency or connection.escalation_currency
            contact_id = request.contact_id
            contact_name = None if activity is None else activity.contact_name
            contact_username = normalize_contact_username(
                None if activity is None else activity.contact_username
            )
            total_count = 0 if activity is None else activity.off_hours_request_count
            paid_count = 0 if activity is None else activity.paid_escalation_count

        await self._clear_keyboard(query, business_connection_id)
        await self._answer(query, "Платне звернення підтверджено.")
        await self.sender.send(
            business_connection_id=business_connection_id,
            chat_id=contact_id,
            text=as_bot_reply(confirm_text),
        )
        if owner_chat_id is not None:
            who = contact_name or f"ID {contact_id}"
            sent = await self.bot.send_message(
                chat_id=owner_chat_id,
                text=(
                    f"🚨 Платне термінове звернення\n"
                    f"Контакт: {who}\n"
                    f"Вартість: {_price(amount, currency)}\n"
                    f"Статистика контакту: {paid_count} платних із {total_count} "
                    "звернень поза графіком"
                ),
                reply_markup=escalation_owner_keyboard(
                    request_id, contact_username=contact_username
                ),
            )
            async with self.database.session() as session, session.begin():
                stored = await session.get(models.ContactRequest, request_id)
                if stored is not None:
                    stored.owner_notification_message_id = getattr(sent, "message_id", None)
        return True

    async def _cancel(self, query: CallbackQuery, *, request_id: int, now: datetime) -> bool:
        async with self.database.session() as session, session.begin():
            request = await session.get(models.ContactRequest, request_id)
            if request is None or request.contact_id != query.from_user.id:
                return False
            connection = await session.get(models.Connection, request.connection_id)
            if connection is None:
                return False
            if request.status == "offered" and request.offer_expires_at > now:
                request.status = "normal"
                request.price_amount = None
                request.currency = None
            business_connection_id = connection.business_connection_id
        await self._clear_keyboard(query, business_connection_id)
        await self._answer(query, "Платне звернення не створено.")
        return True

    async def _decline(
        self, query: CallbackQuery, *, request_id: int, now: datetime
    ) -> bool:
        async with self.database.session() as session, session.begin():
            connection = await load_owner_connection(session, query.from_user.id)
            request = await session.get(models.ContactRequest, request_id)
            if (
                connection is None
                or request is None
                or request.connection_id != connection.id
                or request.status != "paid"
            ):
                return False
            if request.owner_decision == "declined":
                await self._answer(query, "Відмову вже надіслано.")
                return True
            business_connection_id = connection.business_connection_id
            contact_id = request.contact_id
            decline_text = connection.escalation_decline_text

        result = await self.sender.send(
            business_connection_id=business_connection_id,
            chat_id=contact_id,
            text=as_bot_reply(decline_text),
        )
        if result.is_sent:
            async with self.database.session() as session, session.begin():
                await session.execute(
                    update(models.ContactRequest)
                    .where(
                        models.ContactRequest.id == request_id,
                        models.ContactRequest.owner_decision == "pending",
                    )
                    .values(owner_decision="declined", owner_decided_at=now)
                )
            await self._finalize_owner(query, "⛔ Відмову надіслано", "Відмову надіслано")
        else:
            await self._answer(
                query,
                "Повідомлення не доставлено. Перевірте доступ і повторіть.",
                alert=True,
            )
        return True

    async def _open(self, query: CallbackQuery, *, request_id: int) -> bool:
        async with self.database.session() as session:
            connection = await load_owner_connection(session, query.from_user.id)
            request = await session.get(models.ContactRequest, request_id)
            if connection is None or request is None or request.connection_id != connection.id:
                return False
            contact_id = request.contact_id
        try:
            chat = await self.bot.get_chat(contact_id)
            username = normalize_contact_username(getattr(chat, "username", None))
        except Exception:
            username = None
        if username is None:
            await self._answer(
                query,
                "У контакту немає публічного username. "
                "Напишіть через кнопку відправлення від бота.",
                alert=True,
            )
            return True
        if query.message is not None:
            with contextlib.suppress(Exception):
                await self.bot.edit_message_reply_markup(
                    chat_id=query.message.chat.id,
                    message_id=query.message.message_id,
                    reply_markup=escalation_owner_keyboard(
                        request_id, contact_username=username
                    ),
                )
        await self._answer(query, "Посилання оновлено. Натисніть кнопку ще раз.")
        return True

    async def _clear_keyboard(
        self, query: CallbackQuery, business_connection_id: str
    ) -> None:
        if query.message is None:
            return
        with contextlib.suppress(Exception):
            await self.bot.edit_message_reply_markup(
                business_connection_id=business_connection_id,
                chat_id=query.message.chat.id,
                message_id=query.message.message_id,
                reply_markup=None,
            )

    async def _answer(self, query: CallbackQuery, text: str, *, alert: bool = False) -> None:
        await self.bot.answer_callback_query(query.id, text=text, show_alert=alert)

    async def _finalize_owner(self, query: CallbackQuery, note: str, toast: str) -> None:
        await self._answer(query, toast)
        if query.message is None:
            return
        with contextlib.suppress(Exception):
            await self.bot.edit_message_text(
                chat_id=query.message.chat.id,
                message_id=query.message.message_id,
                text=f"{query.message.text or ''}\n\n{note}".strip(),
                reply_markup=None,
            )


def parse_escalation_callback(data: str | None) -> tuple[str, int] | None:
    parts = (data or "").split(":")
    if (
        len(parts) != 3
        or parts[0] != "escalation"
        or parts[1] not in {"offer", "confirm", "cancel", "decline", "open"}
        or not parts[2].isdigit()
    ):
        return None
    request_id = int(parts[2])
    return (parts[1], request_id) if request_id > 0 else None


def _price(amount: Decimal, currency: str) -> str:
    rendered = format(amount, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return f"{rendered} {currency}"
