from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import delete, select

from secretary_bot import models
from secretary_bot.classifier import ClassifierSettings
from secretary_bot.retention import MessageCipher
from secretary_bot.storage import (
    ConnectionRecord,
    Database,
    list_connections,
    load_classifier_settings,
    load_retained_dialogues,
    mark_morning_delivered,
    pending_morning_for_period,
)
from secretary_bot.summary import DialogueSummary, SummaryLanguageModel, summarize_dialogue

logger = logging.getLogger(__name__)

SUMMARY_WINDOW = timedelta(minutes=30)
SUMMARY_PERIOD = timedelta(hours=24)


class SummaryBot(Protocol):
    async def send_message(self, **kwargs: Any) -> Any: ...


@dataclass(slots=True)
class DailySummary:
    database: Database
    bot: SummaryBot
    cipher: MessageCipher | None
    model: SummaryLanguageModel | None
    classifier_defaults: ClassifierSettings
    summary_timeout_seconds: float = 60.0

    async def run_once(self, *, now: datetime | None = None) -> int:
        moment = now or datetime.now(UTC)
        if self.cipher is None or self.model is None:
            return 0
        async with self.database.session() as session:
            connections = await list_connections(session)

        delivered = 0
        for connection in connections:
            period = summary_period(connection, now=moment)
            if period is None or not connection.message_retention_enabled:
                continue
            destination = connection.summary_channel_id or connection.owner_chat_id
            if destination is None:
                continue
            try:
                completed = await self._process(
                    connection,
                    period_start=period[0],
                    period_end=period[1],
                    destination=destination,
                    now=moment,
                )
                delivered += int(completed)
            except Exception as exc:
                logger.error(
                    "daily summary failed: connection_id=%s error=%s",
                    connection.id,
                    type(exc).__name__,
                )
                await self._mark_error(connection.id, period[0], period[1], type(exc).__name__)
        return delivered

    async def _process(
        self,
        connection: ConnectionRecord,
        *,
        period_start: datetime,
        period_end: datetime,
        destination: int,
        now: datetime,
    ) -> bool:
        run_id = await self._ensure_run(
            connection.id,
            period_start=period_start,
            period_end=period_end,
            destination=destination,
        )
        async with self.database.session() as session:
            run = await session.get(models.SummaryRun, run_id)
            if run is None or run.status == "delivered":
                return False
            items = list(
                await session.scalars(
                    select(models.SummaryItem)
                    .where(models.SummaryItem.run_id == run_id)
                    .order_by(models.SummaryItem.id)
                )
            )

        if not items:
            items = await self._generate_items(
                connection,
                run_id=run_id,
                period_start=period_start,
                period_end=period_end,
                now=now,
            )
        if not items:
            await self._mark_delivered(run_id, delivered_at=now)
            return True

        run = await self._load_run(run_id)
        if run.telegram_message_id is None:
            sent = await self.bot.send_message(
                chat_id=destination,
                text=render_summary_header(period_end, items, connection.policy.timezone),
                reply_markup=summary_header_keyboard(run_id),
            )
            await self._save_run_message(run_id, getattr(sent, "message_id", None))

        for item in items:
            if item.telegram_message_id is not None:
                continue
            sent = await self.bot.send_message(
                chat_id=destination,
                text=render_summary_item(item),
                reply_markup=summary_item_keyboard(item.id, item.contact_id),
            )
            await self._save_item_message(item.id, getattr(sent, "message_id", None))
        await self._mark_delivered(run_id, delivered_at=now)
        return True

    async def _generate_items(
        self,
        connection: ConnectionRecord,
        *,
        run_id: int,
        period_start: datetime,
        period_end: datetime,
        now: datetime,
    ) -> list[models.SummaryItem]:
        assert self.cipher is not None and self.model is not None
        async with self.database.session() as session:
            dialogues = await load_retained_dialogues(
                session,
                connection_id=connection.id,
                period_start=period_start,
                period_end=period_end,
                now=now,
                cipher=self.cipher,
            )
            settings = await load_classifier_settings(
                session, connection.id, defaults=self.classifier_defaults
            )
            morning_rows = await pending_morning_for_period(
                session,
                connection.id,
                period_start=period_start,
                period_end=period_end,
            )
        money_contacts = {row.contact_id for row in morning_rows}

        generated: list[tuple[Any, DialogueSummary]] = []
        for dialogue in dialogues:
            summary = await summarize_dialogue(
                dialogue,
                model=self.model,
                model_name=settings.model,
                timeout_seconds=self.summary_timeout_seconds,
            )
            generated.append((dialogue, summary))

        async with self.database.session() as session, session.begin():
            await session.execute(
                delete(models.SummaryItem).where(models.SummaryItem.run_id == run_id)
            )
            items = [
                models.SummaryItem(
                    run_id=run_id,
                    contact_id=dialogue.contact_id,
                    contact_name=dialogue.contact_name,
                    topic=summary.topic,
                    agreements_json=list(summary.agreements),
                    open_questions_json=list(summary.open_questions),
                    questions_asked=summary.questions_asked,
                    questions_closed=summary.questions_closed,
                    money_priority=dialogue.contact_id in money_contacts,
                    last_incoming_message_id=dialogue.last_incoming_message_id,
                )
                for dialogue, summary in generated
            ]
            session.add_all(items)
            await session.flush()
            return items

    async def _ensure_run(
        self,
        connection_id: int,
        *,
        period_start: datetime,
        period_end: datetime,
        destination: int,
    ) -> int:
        async with self.database.session() as session, session.begin():
            run = await session.scalar(
                select(models.SummaryRun).where(
                    models.SummaryRun.connection_id == connection_id,
                    models.SummaryRun.period_start == period_start,
                    models.SummaryRun.period_end == period_end,
                )
            )
            if run is None:
                run = models.SummaryRun(
                    connection_id=connection_id,
                    period_start=period_start,
                    period_end=period_end,
                    destination_chat_id=destination,
                )
                session.add(run)
                await session.flush()
            return run.id

    async def _load_run(self, run_id: int) -> models.SummaryRun:
        async with self.database.session() as session:
            run = await session.get(models.SummaryRun, run_id)
            if run is None:
                raise LookupError("summary run not found")
            return run

    async def _save_run_message(self, run_id: int, message_id: int | None) -> None:
        async with self.database.session() as session, session.begin():
            run = await session.get(models.SummaryRun, run_id)
            if run is None:
                raise LookupError("summary run not found")
            run.telegram_message_id = message_id

    async def _save_item_message(self, item_id: int, message_id: int | None) -> None:
        async with self.database.session() as session, session.begin():
            item = await session.get(models.SummaryItem, item_id)
            if item is None:
                raise LookupError("summary item not found")
            item.telegram_message_id = message_id

    async def _mark_delivered(self, run_id: int, *, delivered_at: datetime) -> None:
        async with self.database.session() as session, session.begin():
            run = await session.get(models.SummaryRun, run_id)
            if run is None:
                raise LookupError("summary run not found")
            run.status = "delivered"
            run.delivered_at = delivered_at
            run.error_code = None
            morning_rows = await pending_morning_for_period(
                session,
                run.connection_id,
                period_start=run.period_start,
                period_end=run.period_end,
            )
            summarized_contacts = set(
                await session.scalars(
                    select(models.SummaryItem.contact_id).where(
                        models.SummaryItem.run_id == run.id
                    )
                )
            )
            await mark_morning_delivered(
                session,
                [row.id for row in morning_rows if row.contact_id in summarized_contacts],
            )

    async def _mark_error(
        self,
        connection_id: int,
        period_start: datetime,
        period_end: datetime,
        error_code: str,
    ) -> None:
        async with self.database.session() as session, session.begin():
            run = await session.scalar(
                select(models.SummaryRun).where(
                    models.SummaryRun.connection_id == connection_id,
                    models.SummaryRun.period_start == period_start,
                    models.SummaryRun.period_end == period_end,
                )
            )
            if run is not None:
                run.status = "error"
                run.error_code = error_code[:200]


def summary_period(
    connection: ConnectionRecord, *, now: datetime
) -> tuple[datetime, datetime] | None:
    if not connection.policy.is_active or connection.policy.kill_switch:
        return None
    local_now = now.astimezone(ZoneInfo(connection.policy.timezone))
    scheduled = local_now.replace(
        hour=connection.summary_time.hour,
        minute=connection.summary_time.minute,
        second=0,
        microsecond=0,
    )
    if not scheduled <= local_now < scheduled + SUMMARY_WINDOW:
        return None
    period_end = scheduled.astimezone(UTC)
    return period_end - SUMMARY_PERIOD, period_end


def render_summary_header(
    period_end: datetime, items: list[models.SummaryItem], timezone: str
) -> str:
    local_end = period_end.astimezone(ZoneInfo(timezone))
    asked = sum(item.questions_asked for item in items)
    closed = sum(item.questions_closed for item in items)
    money = sum(item.money_priority for item in items)
    return (
        f"📋 Добове самарі · {local_end:%d.%m.%Y}\n"
        f"Діалогів: {len(items)}\n"
        f"Питань: {asked} задано · {closed} закрито\n"
        f"💸 Відповісти вранці: {money}"
    )


def render_summary_item(item: models.SummaryItem) -> str:
    who = item.contact_name or f"ID {item.contact_id}"
    priority = "💸 Обіцяли відповісти вранці\n" if item.money_priority else ""
    agreements = "\n".join(f"• {text}" for text in item.agreements_json) or "• немає"
    questions = "\n".join(f"• {text}" for text in item.open_questions_json) or "• немає"
    return (
        f"👤 {who}\n{priority}Тема: {item.topic}\n\n"
        f"Домовленості:\n{agreements}\n\n"
        f"Відкриті питання:\n{questions}\n\n"
        f"Питання: {item.questions_asked} задано · {item.questions_closed} закрито"
    )


def summary_header_keyboard(run_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="👁 Прочитати все", callback_data=f"summary:read:{run_id}"
                )
            ]
        ]
    )


def summary_item_keyboard(item_id: int, contact_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Resolve", callback_data=f"summary:resolve:{item_id}")],
            [
                InlineKeyboardButton(
                    text="💬 Перейти в чат",
                    url=f"tg://openmessage?user_id={contact_id}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="🤖 Відповісти від бота", callback_data=f"summary:reply:{item_id}"
                )
            ],
        ]
    )
