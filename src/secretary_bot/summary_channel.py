from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    ChatAdministratorRights,
    KeyboardButton,
    KeyboardButtonRequestChat,
    Message,
)
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretary_bot import models
from secretary_bot.storage import Database

REQUEST_TTL = timedelta(minutes=15)
MAX_REQUEST_ID = 2_147_483_647
PRIVATE_POST_RE = re.compile(r"^https?://(?:www\.)?t\.me/c/(\d+)/(\d+)(?:[/?#].*)?$", re.I)
PUBLIC_LINK_RE = re.compile(
    r"^https?://(?:www\.)?t\.me/([A-Za-z][A-Za-z0-9_]{3,31})(?:/\d+)?(?:[/?#].*)?$",
    re.I,
)
USERNAME_RE = re.compile(r"^@[A-Za-z][A-Za-z0-9_]{3,31}$")


class SummaryChannelBot(Protocol):
    id: int

    async def save_prepared_keyboard_button(self, **kwargs: Any) -> Any: ...

    async def get_chat(self, chat_id: int | str) -> Any: ...

    async def get_chat_member(self, chat_id: int, user_id: int) -> Any: ...

    async def send_message(self, **kwargs: Any) -> Any: ...


class SummaryChannelError(RuntimeError):
    """A summary channel could not be safely connected."""


@dataclass(frozen=True, slots=True)
class ConnectedChannel:
    chat_id: int
    title: str


@dataclass(frozen=True, slots=True)
class PreparedChannelRequest:
    request_id: int
    prepared_id: str


@dataclass(slots=True)
class SummaryChannelConnector:
    database: Database
    bot: SummaryChannelBot

    async def prepare_request(
        self,
        session: AsyncSession,
        *,
        connection: models.Connection,
        owner_user_id: int,
        now: datetime | None = None,
    ) -> PreparedChannelRequest:
        moment = now or datetime.now(UTC)
        await session.execute(
            delete(models.SummaryChannelRequest).where(
                models.SummaryChannelRequest.connection_id == connection.id
            )
        )
        request = models.SummaryChannelRequest(
            connection_id=connection.id,
            owner_user_id=owner_user_id,
            expires_at=moment + REQUEST_TTL,
        )
        session.add(request)
        await session.flush()
        if request.id > MAX_REQUEST_ID:
            raise SummaryChannelError("Ліміт запитів на вибір каналу вичерпано")
        try:
            prepared = await self.bot.save_prepared_keyboard_button(
                user_id=owner_user_id,
                button=KeyboardButton(
                    text="Обрати канал для підсумків",
                    style="primary",
                    request_chat=KeyboardButtonRequestChat(
                        request_id=request.id,
                        chat_is_channel=True,
                        user_administrator_rights=_channel_rights(),
                        bot_administrator_rights=_channel_rights(),
                        request_title=True,
                        request_username=True,
                    ),
                ),
            )
        except TelegramAPIError as exc:
            raise SummaryChannelError(
                "Telegram не зміг відкрити вибір каналу. Спробуйте посилання на допис."
            ) from exc
        return PreparedChannelRequest(request_id=request.id, prepared_id=prepared.id)

    async def connect_reference(
        self,
        session: AsyncSession,
        *,
        connection: models.Connection,
        owner_user_id: int,
        reference: str,
    ) -> ConnectedChannel:
        lookup = parse_channel_reference(reference)
        channel = await self._inspect_channel(lookup, owner_user_id=owner_user_id)
        _save_channel(connection, channel)
        await session.flush()
        return channel

    async def handle_message(self, message: Message, *, now: datetime | None = None) -> bool:
        shared = message.chat_shared
        if shared is None:
            return False
        moment = now or datetime.now(UTC)
        owner_user_id = message.from_user.id if message.from_user is not None else None
        if owner_user_id is None or message.chat.id != owner_user_id:
            return True

        error: str | None = None
        channel: ConnectedChannel | None = None
        async with self.database.session() as session, session.begin():
            request = await session.scalar(
                select(models.SummaryChannelRequest).where(
                    models.SummaryChannelRequest.id == shared.request_id,
                    models.SummaryChannelRequest.owner_user_id == owner_user_id,
                    models.SummaryChannelRequest.consumed_at.is_(None),
                )
            )
            if request is None or request.expires_at <= moment:
                error = "Запит на підключення каналу прострочений. Відкрийте панель і повторіть."
            else:
                request.consumed_at = moment
                connection = await session.get(models.Connection, request.connection_id)
                if connection is None or connection.owner_user_id != owner_user_id:
                    error = "Підключення власника не знайдено."
                else:
                    try:
                        channel = await self._inspect_channel(
                            shared.chat_id, owner_user_id=owner_user_id
                        )
                    except SummaryChannelError as exc:
                        error = str(exc)
                    else:
                        _save_channel(connection, channel)
                        request.status = "connected"
                        request.channel_id = channel.chat_id
                if error is not None:
                    request.status = "error"
                    request.error_message = error
                await session.flush()

        if channel is not None:
            await self.bot.send_message(
                chat_id=message.chat.id,
                text=(
                    f"✅ Канал «{channel.title}» підключено. "
                    "Наступний щоденний підсумок надійде туди."
                ),
            )
        elif error is not None:
            await self.bot.send_message(chat_id=message.chat.id, text=f"⚠️ {error}")
        return True

    async def _inspect_channel(self, lookup: int | str, *, owner_user_id: int) -> ConnectedChannel:
        try:
            chat = await self.bot.get_chat(lookup)
            if _value(chat.type) != "channel":
                raise SummaryChannelError("Оберіть саме канал, а не групу або приватний чат.")
            owner = await self.bot.get_chat_member(chat.id, owner_user_id)
            bot_member = await self.bot.get_chat_member(chat.id, self.bot.id)
        except SummaryChannelError:
            raise
        except TelegramAPIError as exc:
            raise SummaryChannelError(
                "Канал недоступний боту. Додайте бота адміністратором і повторіть."
            ) from exc

        if _value(owner.status) not in {"creator", "administrator"}:
            raise SummaryChannelError("Підключити канал може лише його адміністратор.")
        if _value(bot_member.status) not in {"creator", "administrator"}:
            raise SummaryChannelError("Додайте бота адміністратором каналу.")
        if getattr(bot_member, "can_post_messages", None) is False:
            raise SummaryChannelError("Надайте боту право публікувати повідомлення.")
        return ConnectedChannel(chat_id=chat.id, title=(chat.title or "Telegram-канал"))


def parse_channel_reference(reference: str) -> int | str:
    value = reference.strip()
    if re.fullmatch(r"-100\d{6,16}", value):
        return int(value)
    private = PRIVATE_POST_RE.fullmatch(value)
    if private is not None:
        return -int(f"100{private.group(1)}")
    public = PUBLIC_LINK_RE.fullmatch(value)
    if public is not None:
        return f"@{public.group(1)}"
    if USERNAME_RE.fullmatch(value):
        return value
    if "t.me/+" in value or "joinchat" in value:
        raise SummaryChannelError(
            "За посиланням-запрошенням канал не визначити. "
            "Скопіюйте посилання на допис каналу."
        )
    raise SummaryChannelError("Вкажіть @username або посилання на допис у каналі.")


def _save_channel(connection: models.Connection, channel: ConnectedChannel) -> None:
    connection.summary_channel_id = channel.chat_id
    connection.summary_channel_title = channel.title


def _channel_rights() -> ChatAdministratorRights:
    return ChatAdministratorRights(
        is_anonymous=False,
        can_manage_chat=True,
        can_delete_messages=False,
        can_manage_video_chats=False,
        can_restrict_members=False,
        can_promote_members=False,
        can_change_info=False,
        can_invite_users=False,
        can_post_stories=False,
        can_edit_stories=False,
        can_delete_stories=False,
        can_send_welcome_messages=False,
        can_post_messages=True,
        can_edit_messages=False,
        can_pin_messages=False,
        can_manage_topics=False,
        can_manage_direct_messages=False,
        can_manage_tags=False,
    )


def _value(value: Any) -> str:
    return str(getattr(value, "value", value))
