from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from aiogram.types import BusinessConnection, Update

logger = logging.getLogger(__name__)


class TelegramBot(Protocol):
    async def get_business_connection(self, business_connection_id: str) -> BusinessConnection: ...

    async def send_message(self, **kwargs: Any) -> Any: ...

    async def read_business_message(
        self, business_connection_id: str, chat_id: int, message_id: int
    ) -> bool: ...


@dataclass(slots=True)
class RuntimeState:
    bot: TelegramBot
    echo_enabled: bool
    allowed_chat_ids: frozenset[int]
    queue_size: int
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
        except Exception as exc:  # keep the PoC worker alive after one bad update
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
        connection = update.business_connection
        state.connections[connection.id] = connection
        _log_connection(connection, update_id=update.update_id)
        return

    message = update.business_message
    if message is None:
        _log(logging.INFO, "update_ignored", update_id=update.update_id)
        return

    connection_id = message.business_connection_id
    if connection_id is None:
        _log(logging.WARNING, "message_missing_connection", update_id=update.update_id)
        return

    connection = state.connections.get(connection_id)
    if connection is None:
        connection = await state.bot.get_business_connection(connection_id)
        state.connections[connection.id] = connection
        _log_connection(connection, update_id=update.update_id, source="api_refresh")

    if message.sender_business_bot is not None:
        _log(logging.INFO, "message_skipped_bot_echo", update_id=update.update_id)
        return
    if message.from_user is not None and message.from_user.id == connection.user.id:
        _log(logging.INFO, "message_skipped_owner", update_id=update.update_id)
        return
    if message.chat.type != "private":
        _log(logging.INFO, "message_skipped_non_private", update_id=update.update_id)
        return
    if message.chat.id not in state.allowed_chat_ids:
        _log(
            logging.WARNING,
            "message_skipped_not_allowlisted",
            update_id=update.update_id,
            chat_id=message.chat.id,
        )
        return
    if message.text is None:
        _log(logging.INFO, "message_skipped_unsupported", update_id=update.update_id)
        return
    if not connection.is_enabled or connection.rights is None or not connection.rights.can_reply:
        _log(logging.WARNING, "message_skipped_no_reply_right", update_id=update.update_id)
        return
    if not state.echo_enabled:
        _log(logging.INFO, "message_skipped_echo_disabled", update_id=update.update_id)
        return

    sent = await state.bot.send_message(
        business_connection_id=connection_id,
        chat_id=message.chat.id,
        text=message.text,
    )
    _log(
        logging.INFO,
        "echo_sent",
        update_id=update.update_id,
        connection_id=connection_id,
        chat_id=message.chat.id,
        incoming_message_id=message.message_id,
        sent_message_id=getattr(sent, "message_id", None),
    )

    if connection.rights.can_read_messages:
        try:
            await state.bot.read_business_message(
                business_connection_id=connection_id,
                chat_id=message.chat.id,
                message_id=message.message_id,
            )
        except Exception as exc:
            _log(
                logging.WARNING,
                "message_read_failed",
                update_id=update.update_id,
                error_type=type(exc).__name__,
            )
        else:
            _log(
                logging.INFO,
                "message_marked_read",
                update_id=update.update_id,
                connection_id=connection_id,
                chat_id=message.chat.id,
                message_id=message.message_id,
            )


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
