from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from aiogram.types import BusinessConnection, Message, Update

from secretary_bot.callbacks import finalize_callback
from secretary_bot.control import ControlPlane
from secretary_bot.hard_filter import apply_hard_filter
from secretary_bot.notifications import parse_feedback
from secretary_bot.pipeline import IncomingMessage, Pipeline
from secretary_bot.storage import (
    ConnectionSnapshot,
    deactivate_connection,
    feedback_belongs_to_owner,
    load_access_user,
    load_connection,
    record_feedback,
    set_connection_control,
    upsert_connection,
)
from secretary_bot.texts import (
    CONNECTION_DISABLED_ALERT,
    READ_PERMISSION_LOST_ALERT,
    REPLY_PERMISSION_LOST_ALERT,
)

logger = logging.getLogger(__name__)


class TelegramBot(Protocol):
    async def get_business_connection(self, business_connection_id: str) -> BusinessConnection: ...

    async def send_message(self, **kwargs: Any) -> Any: ...

    async def read_business_message(self, **kwargs: Any) -> Any: ...

    async def answer_callback_query(self, callback_query_id: str, **kwargs: Any) -> Any: ...

    async def edit_message_text(self, **kwargs: Any) -> Any: ...

    async def edit_message_reply_markup(self, **kwargs: Any) -> Any: ...


@dataclass(slots=True)
class RuntimeState:
    bot: TelegramBot
    pipeline: Pipeline
    control: ControlPlane
    queue_size: int
    # Optional safety net for early operation: when set, only these chats are
    # processed. Empty means the FR-2 policy — every chat except exclusions.
    allowed_chat_ids: frozenset[int] = frozenset()
    connections: dict[str, BusinessConnection] = field(default_factory=dict)
    processed_updates: int = 0
    queue: asyncio.Queue[Update] = field(init=False)

    def __post_init__(self) -> None:
        self.queue = asyncio.Queue(maxsize=self.queue_size)


async def process_updates(state: RuntimeState) -> None:
    while True:
        update = await state.queue.get()
        try:
            await handle_update(update, state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # one bad update must not stop the worker
            _log(
                logging.ERROR,
                "update_failed",
                update_id=update.update_id,
                error_type=type(exc).__name__,
            )
        finally:
            state.processed_updates += 1
            state.queue.task_done()


async def handle_update(update: Update, state: RuntimeState) -> None:
    if update.business_connection is not None:
        await _store_connection(update.business_connection, state, update_id=update.update_id)
        return

    if update.callback_query is not None:
        if await state.control.handle_callback(update.callback_query):
            return
        await _handle_feedback(update, state)
        return

    if update.message is not None:
        if not await state.control.handle_message(update.message):
            _log(logging.INFO, "message_ignored", update_id=update.update_id)
        return

    if update.business_message is not None:
        await _handle_business_message(update.business_message, state, update_id=update.update_id)
        return

    _log(logging.INFO, "update_ignored", update_id=update.update_id)


async def _handle_business_message(
    message: Message, state: RuntimeState, *, update_id: int
) -> None:
    connection_id = message.business_connection_id
    if connection_id is None:
        _log(logging.WARNING, "message_missing_connection", update_id=update_id)
        return

    connection = await _connection(connection_id, state, update_id=update_id)
    if connection is None:
        return
    async with state.pipeline.database.session() as session:
        access = await load_access_user(session, connection.user.id)
        if access is None or not access.can_process:
            _log(logging.INFO, "business_message_denied", update_id=update_id)
            return
    if state.allowed_chat_ids and message.chat.id not in state.allowed_chat_ids:
        _log(
            logging.INFO,
            "message_skipped_not_allowlisted",
            update_id=update_id,
            chat_id=message.chat.id,
        )
        return

    filter_result = apply_hard_filter(message, owner_user_id=connection.user.id)
    incoming = IncomingMessage(
        business_connection_id=connection_id,
        chat_id=message.chat.id,
        message_id=message.message_id,
        filter_result=filter_result,
        received_at=message.date.astimezone(UTC) if message.date else datetime.now(UTC),
        text=message.text or "",
        contact_name=_contact_name(message),
    )
    await state.pipeline.process_incoming(incoming)


async def _handle_feedback(update: Update, state: RuntimeState) -> None:
    query = update.callback_query
    assert query is not None
    parsed = parse_feedback(query.data or "")
    if parsed is None:
        _log(logging.INFO, "callback_ignored", update_id=update.update_id)
        return

    log_id, verdict = parsed
    database = state.pipeline.database
    async with database.session() as session, session.begin():
        access = await load_access_user(session, query.from_user.id)
        if access is None or not access.can_process:
            _log(logging.INFO, "feedback_denied", update_id=update.update_id)
            return
        if not await feedback_belongs_to_owner(
            session, log_id=log_id, owner_user_id=query.from_user.id
        ):
            _log(logging.INFO, "feedback_cross_tenant_denied", update_id=update.update_id)
            return
        await record_feedback(session, log_id=log_id, verdict=verdict)
    _log(logging.INFO, "shadow_feedback", log_id=log_id, verdict=verdict)
    labels = {
        "ok": ("✅ Обработано: оценка «Норм»", "Записал: Норм"),
        "wrong": ("✅ Обработано: оценка «Не надо было»", "Записал оценку"),
        "exclude": ("✅ Обработано: кандидат на исключение", "Записал оценку"),
    }
    note, toast = labels[verdict]
    await finalize_callback(state.bot, query, note=note, toast=toast)


async def _connection(
    connection_id: str, state: RuntimeState, *, update_id: int
) -> BusinessConnection | None:
    connection = state.connections.get(connection_id)
    if connection is not None:
        return connection
    connection = await state.bot.get_business_connection(connection_id)
    stored = await _store_connection(connection, state, update_id=update_id, source="api_refresh")
    return connection if stored else None


async def _store_connection(
    connection: BusinessConnection,
    state: RuntimeState,
    *,
    update_id: int,
    source: str = "update",
) -> bool:
    cancel_connection_id: int | None = None
    alert: tuple[int, str] | None = None
    async with state.pipeline.database.session() as session, session.begin():
        access = await load_access_user(session, connection.user.id)
        if access is None or not access.can_connect:
            _log(
                logging.WARNING,
                "business_connection_denied",
                update_id=update_id,
                source=source,
            )
            return False
        previous = await load_connection(session, connection.id)
        rights = connection.rights.model_dump(exclude_none=True) if connection.rights else {}
        record = await upsert_connection(
            session,
            ConnectionSnapshot(
                business_connection_id=connection.id,
                owner_user_id=connection.user.id,
                owner_chat_id=connection.user_chat_id,
                owner_username=connection.user.username,
                rights=rights,
                is_enabled=connection.is_enabled,
            ),
        )
        if not access.can_process:
            await set_connection_control(session, record.id, kill_switch=True, muted_until=None)
        elif not connection.is_enabled:
            await deactivate_connection(session, record.id)
            cancel_connection_id = record.id
            if record.owner_chat_id is not None:
                alert = (record.owner_chat_id, CONNECTION_DISABLED_ALERT)
        elif not rights.get("can_reply", False):
            await deactivate_connection(session, record.id)
            cancel_connection_id = record.id
            if record.owner_chat_id is not None:
                alert = (record.owner_chat_id, REPLY_PERMISSION_LOST_ALERT)
        elif (
            record.mark_read
            and not rights.get("can_read_messages", False)
            and (previous is None or previous.rights.get("can_read_messages", False))
            and record.owner_chat_id is not None
        ):
            alert = (record.owner_chat_id, READ_PERMISSION_LOST_ALERT)
    state.connections[connection.id] = connection
    if cancel_connection_id is not None:
        await state.pipeline.queue.cancel_connection(cancel_connection_id)
    if alert is not None:
        await state.pipeline.notifier.alert(*alert)
    _log_connection(connection, update_id=update_id, source=source)
    await state.control.handle_business_connection(connection.user.id, connection.user_chat_id)
    return True


def _contact_name(message: Message) -> str | None:
    sender = message.from_user
    if sender is None:
        return message.chat.first_name
    return sender.full_name


def _log_connection(
    connection: BusinessConnection, *, update_id: int, source: str = "update"
) -> None:
    rights = connection.rights.model_dump(exclude_none=True) if connection.rights else {}
    _log(
        logging.INFO,
        "business_connection",
        update_id=update_id,
        source=source,
        connection_id=connection.id,
        owner_user_id=connection.user.id,
        user_chat_id=connection.user_chat_id,
        is_enabled=connection.is_enabled,
        rights=rights,
    )


def _log(level: int, event: str, **fields: Any) -> None:
    logger.log(level, json.dumps({"event": event, **fields}, ensure_ascii=False, default=str))
