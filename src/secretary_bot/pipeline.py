from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from secretary_bot.actions import LogAction
from secretary_bot.classifier import Category, ClassifierSettings, LanguageModel, classify
from secretary_bot.delayed import DelayedReplyQueue, ReplyTask, reply_delay
from secretary_bot.gate import GateDecision, evaluate_gate
from secretary_bot.hard_filter import HardFilterResult
from secretary_bot.notifications import OwnerNotifier, Preview
from secretary_bot.sender import BusinessReplySender, SendOutcome
from secretary_bot.storage import (
    ConnectionRecord,
    Database,
    claim_window,
    enqueue_morning,
    load_classifier_settings,
    load_connection,
    load_contact_state,
    load_forced_template_code,
    load_templates,
    log_decision,
    owner_replied_since,
    record_auto_reply,
    record_incoming,
    record_owner_reply,
)
from secretary_bot.templates import TemplateCode, render, template_for

logger = logging.getLogger(__name__)

CONNECTION_LOST_ALERT = (
    "⚠️ Telegram отклонил отправку: соединение недействительно. "
    "Автоответы остановлены, проверьте подключение бота в настройках."
)


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """One ``business_message``, already stripped of Telegram specifics."""

    business_connection_id: str
    chat_id: int
    message_id: int
    filter_result: HardFilterResult
    received_at: datetime
    text: str = ""
    contact_name: str | None = None

    @property
    def contact_id(self) -> int:
        # Business messages only ever arrive from private chats (§2.2).
        return self.chat_id


@dataclass(slots=True)
class Pipeline:
    database: Database
    queue: DelayedReplyQueue
    sender: BusinessReplySender
    notifier: OwnerNotifier
    model: LanguageModel | None = None
    classifier_defaults: ClassifierSettings = field(default_factory=ClassifierSettings)
    rng: random.Random | None = None

    async def process_incoming(self, incoming: IncomingMessage) -> None:
        """Steps 1–4 of §4: filter, gate, classify, then wait out the delay."""
        async with self.database.session() as session, session.begin():
            connection = await load_connection(session, incoming.business_connection_id)
            if connection is None:
                _log(logging.WARNING, "unknown_connection", chat_id=incoming.chat_id)
                return

            if incoming.filter_result is HardFilterResult.OWNER_MESSAGE:
                # FR-9: the owner answered this chat himself.
                await record_owner_reply(
                    session, connection.id, incoming.contact_id, at=incoming.received_at
                )
                return
            if incoming.filter_result is HardFilterResult.UNSUPPORTED_CONTENT:
                await record_incoming(
                    session, connection.id, incoming.contact_id, at=incoming.received_at
                )
                await self._log(
                    session, connection, incoming, LogAction.SKIPPED_UNSUPPORTED_CONTENT
                )
                return
            if incoming.filter_result is not HardFilterResult.ALLOWED:
                # Bots, service accounts and group chats leave no trace at all.
                return

            await record_incoming(
                session, connection.id, incoming.contact_id, at=incoming.received_at
            )
            contact = await load_contact_state(session, connection.id, incoming.contact_id)
            gate = evaluate_gate(connection.policy, contact, now=incoming.received_at)
            if gate.decision is not GateDecision.ALLOWED:
                await self._log(session, connection, incoming, LogAction(gate.decision.value))
                return

            await claim_window(
                session, connection.id, incoming.contact_id, window_key=gate.window_key
            )
            settings = await load_classifier_settings(
                session, connection.id, defaults=self.classifier_defaults
            )
            classification = await classify(incoming.text, model=self.model, settings=settings)
            forced_template = await load_forced_template_code(
                session, connection.id, incoming.contact_id
            )

        task = ReplyTask(
            connection_id=connection.id,
            business_connection_id=connection.business_connection_id,
            contact_id=incoming.contact_id,
            chat_id=incoming.chat_id,
            message_id=incoming.message_id,
            template_code=forced_template or template_for(classification.category).value,
            category=classification.category.value,
            incoming_at=incoming.received_at.isoformat(),
            confidence=None
            if classification.confidence is None
            else str(classification.confidence),
            window_key=gate.window_key,
            contact_name=incoming.contact_name,
        )
        due_at = incoming.received_at + reply_delay(rng=self.rng)
        await self.queue.schedule(task, due_at=due_at)
        _log(
            logging.INFO,
            "reply_scheduled",
            connection_id=connection.id,
            contact_id=incoming.contact_id,
            category=classification.category.value,
            source=classification.source.value,
            due_at=due_at.isoformat(),
        )

    async def deliver(self, task: ReplyTask, *, now: datetime | None = None) -> LogAction:
        """Steps 5–9: the owner may have answered; otherwise send or preview.

        Telegram calls happen between transactions, never inside one: a flood
        wait can pause a send for a minute, and no database transaction should
        be held open for that long.
        """
        moment = now or datetime.now(UTC)
        async with self.database.session() as session, session.begin():
            connection = await load_connection(session, task.business_connection_id)
            if connection is None:
                _log(logging.WARNING, "unknown_connection", connection_id=task.connection_id)
                return LogAction.ERROR

            refusal = _blocked(connection, now=moment) or await _owner_answered(
                session, connection, task
            )
            if refusal is not None:
                await self._log_task(session, connection, task, refusal)
                return refusal

            overrides = await load_templates(session, connection.id)

        text = render(TemplateCode(task.template_code), overrides=overrides)
        if connection.dry_run:
            return await self._preview(connection, task, text, at=moment)
        return await self._send(connection, task, text, at=moment)

    async def _send(
        self, connection: ConnectionRecord, task: ReplyTask, text: str, *, at: datetime
    ) -> LogAction:
        result = await self.sender.send(
            business_connection_id=connection.business_connection_id,
            chat_id=task.chat_id,
            text=text,
        )
        async with self.database.session() as session, session.begin():
            if not result.is_sent:
                await self._log_task(
                    session, connection, task, LogAction.ERROR, error_code=result.error_code
                )
            else:
                await record_auto_reply(
                    session, connection.id, task.contact_id, at=at, window_key=task.window_key
                )
                await self._log_task(
                    session,
                    connection,
                    task,
                    LogAction.REPLIED,
                    direction="out",
                    tg_message_id=result.message_id,
                )
                await self._flag_money(session, connection, task)

        if result.outcome is SendOutcome.CONNECTION_INVALID:
            await self._alert(connection, CONNECTION_LOST_ALERT)
        return LogAction.REPLIED if result.is_sent else LogAction.ERROR

    async def _preview(
        self, connection: ConnectionRecord, task: ReplyTask, text: str, *, at: datetime
    ) -> LogAction:
        """FR-11: the contact gets nothing; the owner gets the would-be answer."""
        async with self.database.session() as session, session.begin():
            await record_auto_reply(
                session, connection.id, task.contact_id, at=at, window_key=task.window_key
            )
            log_id = await self._log_task(session, connection, task, LogAction.DRY_RUN)
            await self._flag_money(session, connection, task)

        if connection.owner_chat_id is None:
            _log(logging.WARNING, "owner_chat_unknown", connection_id=connection.id)
            return LogAction.DRY_RUN

        local_time = task.incoming_moment.astimezone(ZoneInfo(connection.policy.timezone))
        await self.notifier.preview(
            connection.owner_chat_id,
            Preview(
                log_id=log_id,
                contact_id=task.contact_id,
                contact_name=task.contact_name,
                occurred_at=local_time,
                category=task.category,
                confidence=task.confidence,
                reply_text=text,
            ),
        )
        return LogAction.DRY_RUN

    async def _flag_money(
        self, session: Any, connection: ConnectionRecord, task: ReplyTask
    ) -> None:
        """§6.7. Dry run flags too: the morning list only ever reaches the owner,
        and the shadow week has to exercise the same path as live mode."""
        if task.category != Category.MONEY.value:
            return
        await enqueue_morning(
            session,
            connection_id=connection.id,
            contact_id=task.contact_id,
            contact_name=task.contact_name,
            occurred_at=task.incoming_moment,
        )

    async def _alert(self, connection: ConnectionRecord, text: str) -> None:
        if connection.owner_chat_id is None:
            _log(logging.ERROR, "alert_undeliverable", connection_id=connection.id)
            return
        await self.notifier.alert(connection.owner_chat_id, text)

    async def _log(
        self,
        session: AsyncSession,
        connection: ConnectionRecord,
        incoming: IncomingMessage,
        action: LogAction,
    ) -> None:
        await log_decision(
            session,
            connection_id=connection.id,
            contact_id=incoming.contact_id,
            tg_message_id=incoming.message_id,
            action=action,
            occurred_at=incoming.received_at,
        )
        _log(
            logging.INFO,
            "decision",
            connection_id=connection.id,
            contact_id=incoming.contact_id,
            action=action.value,
        )

    async def _log_task(
        self,
        session: AsyncSession,
        connection: ConnectionRecord,
        task: ReplyTask,
        action: LogAction,
        *,
        direction: str = "in",
        tg_message_id: int | None = None,
        error_code: str | None = None,
    ) -> int:
        log_id = await log_decision(
            session,
            connection_id=connection.id,
            contact_id=task.contact_id,
            tg_message_id=tg_message_id or task.message_id,
            direction=direction,
            action=action,
            category=task.category,
            confidence=None if task.confidence is None else Decimal(task.confidence),
            template_code=task.template_code,
            error_code=error_code,
        )
        _log(
            logging.INFO,
            "decision",
            connection_id=connection.id,
            contact_id=task.contact_id,
            action=action.value,
            category=task.category,
            error_code=error_code,
        )
        return log_id


async def _owner_answered(
    session: AsyncSession, connection: ConnectionRecord, task: ReplyTask
) -> LogAction | None:
    """FR-9: a live answer during the delay wins over the auto-reply."""
    replied = await owner_replied_since(
        session, connection.id, task.contact_id, moment=task.incoming_moment
    )
    return LogAction.SKIPPED_OWNER_REPLIED if replied else None


def _blocked(connection: ConnectionRecord, *, now: datetime) -> LogAction | None:
    """The gate ran before the delay; re-check what may have changed since."""
    if not connection.policy.is_active:
        return LogAction.SKIPPED_INACTIVE
    muted_until = connection.policy.muted_until
    if connection.policy.kill_switch or (muted_until is not None and now < muted_until):
        return LogAction.SKIPPED_KILL_SWITCH
    return None


def _log(level: int, event: str, **fields: object) -> None:
    logger.log(level, json.dumps({"event": event, **fields}, ensure_ascii=False, default=str))
