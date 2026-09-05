from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretary_bot import models
from secretary_bot.actions import LogAction
from secretary_bot.analytics import build_analytics, local_period, render_monthly_pdf
from secretary_bot.classifier import (
    DEFAULT_CONFIDENCE_MIN,
    DEFAULT_MODEL,
    DEFAULT_SYSTEM_PROMPT,
    MONEY_KEYWORDS,
    classify,
)
from secretary_bot.config import Settings
from secretary_bot.gate import ContactState, evaluate_gate
from secretary_bot.identities import contact_label
from secretary_bot.outbox import retry_failed_notifications
from secretary_bot.status import operating_status
from secretary_bot.storage import (
    Database,
    load_classifier_settings,
    load_connection,
    load_contact_state,
    load_forced_template_code,
    load_templates,
    purge_retained_messages,
    set_delivery_preferences,
)
from secretary_bot.summary_channel import SummaryChannelConnector, SummaryChannelError
from secretary_bot.templates import DEFAULT_TEMPLATES, TemplateCode, render, template_for
from secretary_bot.texts import as_bot_reply
from secretary_bot.web_auth import (
    EXCHANGE_TTL,
    SESSION_COOKIE,
    WebAuthError,
    authenticate_session,
    consume_exchange,
    create_web_token,
    revoke_session,
    validate_init_data,
)

MAX_WINDOWS = 16
MAX_LOGS = 200
LOG_RETENTION = timedelta(days=30)

DEFAULT_DIRECTIONS = {
    "general": {
        "label": "Загальне",
        "description": "Усі повідомлення, що не стосуються оплати.",
        "keywords": [],
        "is_active": True,
    },
    "money": {
        "label": "Гроші",
        "description": "Оплата, рахунки, аванс, борги та реквізити.",
        "keywords": list(MONEY_KEYWORDS),
        "is_active": True,
    },
}


class WindowPayload(BaseModel):
    weekday_mask: Annotated[int, Field(ge=1, le=127)]
    time_from: time
    time_to: time
    is_active: bool = True


class DeliveryPayload(BaseModel):
    sender_identity: Literal["bot", "owner"]
    delay_min_seconds: Annotated[int, Field(ge=0, le=3600)]
    delay_max_seconds: Annotated[int, Field(ge=1, le=3600)]
    bot_delay_seconds: Annotated[int, Field(ge=1, le=60)]
    mark_read: bool
    max_auto_replies_per_window: Annotated[int | None, Field(ge=1, le=100)] = None

    @model_validator(mode="after")
    def validate_bounds(self) -> DeliveryPayload:
        if self.delay_min_seconds > self.delay_max_seconds:
            raise ValueError("Мінімальна затримка не може бути більшою за максимальну")
        if self.bot_delay_seconds > min(self.delay_max_seconds, 60):
            raise ValueError("Затримка бота виходить за дозволений діапазон")
        return self


class EscalationPayload(BaseModel):
    enabled: bool
    price_amount: Annotated[Decimal, Field(ge=0, le=1_000_000_000)]
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3,8}$")]
    offer_text: Annotated[str, Field(min_length=1, max_length=1000)]
    confirm_text: Annotated[str, Field(min_length=1, max_length=1000)]
    decline_text: Annotated[str, Field(min_length=1, max_length=1000)]

    @model_validator(mode="after")
    def validate_price(self) -> EscalationPayload:
        if self.enabled and self.price_amount <= 0:
            raise ValueError("Для платних звернень вкажіть ціну більшу за нуль")
        return self


class SchedulePayload(BaseModel):
    timezone: Annotated[str, Field(min_length=1, max_length=64)]
    windows: Annotated[list[WindowPayload], Field(min_length=1, max_length=MAX_WINDOWS)]

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Невідомий часовий пояс") from exc
        return value


class TemplatesPayload(BaseModel):
    off_hours_default: Annotated[str, Field(max_length=1000)]
    money_priority: Annotated[str, Field(max_length=1000)]

    @field_validator("off_hours_default", "money_priority")
    @classmethod
    def non_empty_template(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("Шаблон не може бути порожнім")
        return text


class DirectionPayload(BaseModel):
    code: Literal["general", "money"]
    label: Annotated[str, Field(min_length=1, max_length=80)]
    description: Annotated[str, Field(min_length=1, max_length=500)]
    keywords: Annotated[list[str], Field(max_length=100)] = Field(default_factory=list)
    is_active: bool = True

    @field_validator("keywords")
    @classmethod
    def clean_keywords(cls, values: list[str]) -> list[str]:
        cleaned = []
        for value in values:
            keyword = value.strip().lower()
            if not keyword or len(keyword) > 40:
                raise ValueError("Ключове слово має містити від 1 до 40 символів")
            if keyword not in cleaned:
                cleaned.append(keyword)
        return cleaned


class ClassifierPayload(BaseModel):
    directions: Annotated[list[DirectionPayload], Field(min_length=2, max_length=2)]
    system_prompt: Annotated[str, Field(min_length=20, max_length=8000)]
    model: Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{3,100}$")]
    confidence_min: Annotated[Decimal, Field(ge=Decimal("0"), le=Decimal("1"))]

    @model_validator(mode="after")
    def validate_directions(self) -> ClassifierPayload:
        if {direction.code for direction in self.directions} != {"general", "money"}:
            raise ValueError("Потрібні напрямки general і money")
        general = next(item for item in self.directions if item.code == "general")
        if not general.is_active:
            raise ValueError("Загальний напрямок має залишатися активним")
        return self


class SummaryPayload(BaseModel):
    summary_time: time
    summary_channel_id: int | None = None
    message_retention_enabled: bool | None = None

    @field_validator("summary_channel_id")
    @classmethod
    def validate_channel_id(cls, value: int | None) -> int | None:
        if value == 0:
            raise ValueError("Оберіть канал у Telegram")
        return value


class SummaryChannelPayload(BaseModel):
    reference: Annotated[str, Field(min_length=1, max_length=500)]


class ContactPayload(BaseModel):
    exclusion: Literal["none", "forever", "until"] = "none"
    exclusion_until: datetime | None = None
    windows: Annotated[list[WindowPayload], Field(max_length=MAX_WINDOWS)] = Field(
        default_factory=list
    )

    @model_validator(mode="after")
    def validate_exclusion(self) -> ContactPayload:
        if self.exclusion == "until":
            if self.exclusion_until is None or self.exclusion_until.tzinfo is None:
                raise ValueError("Вкажіть дату завершення з часовим поясом")
            if self.exclusion_until <= datetime.now(UTC):
                raise ValueError("Дата завершення має бути в майбутньому")
        elif self.exclusion_until is not None:
            raise ValueError("Дата потрібна лише для тимчасового виключення")
        return self


class PreviewPayload(BaseModel):
    text: Annotated[str, Field(min_length=1, max_length=2000)]
    contact_id: int | None = None


class ControlPayload(BaseModel):
    action: Literal["pause", "resume", "stop", "dry_run", "live"]
    hours: Annotated[int, Field(ge=1, le=24)] = 1
    confirmed: bool = False


@dataclass(frozen=True, slots=True)
class Principal:
    user: models.AccessUser
    connection: models.Connection


@dataclass(slots=True)
class WebApi:
    database: Database
    settings: Settings

    async def authorize(self, session: AsyncSession, request: Request) -> Principal:
        user_id: int | None = None
        raw_init_data = request.headers.get("X-Telegram-Init-Data")
        if raw_init_data:
            try:
                user_id = validate_init_data(
                    raw_init_data, bot_token=self.settings.bot_token
                ).user_id
            except WebAuthError as exc:
                raise _unauthorized() from exc
        else:
            token = request.cookies.get(SESSION_COOKIE)
            if token:
                user_id = await authenticate_session(session, token=token, now=datetime.now(UTC))
        if user_id is None:
            raise _unauthorized()

        user = await session.get(models.AccessUser, user_id)
        if user is None or user.status != "active" or user.onboarding_state != "ready":
            raise _unauthorized()
        connection = await session.scalar(
            select(models.Connection)
            .where(models.Connection.owner_user_id == user_id)
            .order_by(models.Connection.id.desc())
            .limit(1)
        )
        if connection is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="Підключення не знайдено"
            )
        return Principal(user=user, connection=connection)


def build_web_router(
    *,
    database: Database,
    settings: Settings,
    summary_channel_connector: SummaryChannelConnector | None = None,
) -> APIRouter:
    router = APIRouter()
    api = WebApi(database=database, settings=settings)

    @router.get("/api/v1/bootstrap")
    async def bootstrap(request: Request) -> dict[str, Any]:
        async with database.session() as session, session.begin():
            principal = await api.authorize(session, request)
            return await _bootstrap(session, principal)

    @router.post("/api/v1/control")
    async def control(request: Request, payload: ControlPayload) -> dict[str, Any]:
        async with database.session() as session, session.begin():
            principal = await api.authorize(session, request)
            row = principal.connection
            if payload.action == "live":
                if not payload.confirmed:
                    raise HTTPException(status_code=422, detail="Підтвердіть відповіді клієнтам")
                if not row.is_active or not (row.rights_json or {}).get("can_reply"):
                    raise HTTPException(status_code=409, detail="Перевірте підключення та права")
                row.dry_run = False
            elif payload.action == "dry_run":
                row.dry_run = True
                row.live_confirmation_until = None
                row.control_state = "main"
            elif payload.action == "pause":
                row.muted_until = datetime.now(UTC) + timedelta(hours=payload.hours)
            elif payload.action == "stop":
                row.kill_switch = True
                row.muted_until = None
            else:
                row.kill_switch = False
                row.muted_until = None
            # Pending replies are not dropped here: the worker re-checks pause,
            # kill switch and dry-run at delivery time and logs the outcome.
            await session.flush()
            return await _bootstrap(session, principal)

    @router.post("/api/v1/notifications/retry")
    async def retry_notifications(request: Request) -> dict[str, Any]:
        async with database.session() as session, session.begin():
            principal = await api.authorize(session, request)
            await retry_failed_notifications(
                session, connection_id=principal.connection.id, now=datetime.now(UTC)
            )
            return await _bootstrap(session, principal)

    @router.post("/api/v1/preview")
    async def preview(request: Request, payload: PreviewPayload) -> dict[str, Any]:
        """Dry evaluation of the saved rules, mirroring the pipeline's own checks."""
        async with database.session() as session, session.begin():
            principal = await api.authorize(session, request)
            connection = await load_connection(session, principal.connection.business_connection_id)
            assert connection is not None
            contact = (
                await load_contact_state(session, connection.id, payload.contact_id)
                if payload.contact_id is not None
                else ContactState()
            )
            if not connection.policy.is_active or not connection.rights.get("can_reply", False):
                # The pipeline refuses before the gate when the bot may not reply.
                decision_code = LogAction.SKIPPED_INACTIVE.value
            else:
                decision_code = evaluate_gate(
                    connection.policy, contact, now=datetime.now(UTC)
                ).decision.value
            forced_template = (
                await load_forced_template_code(session, connection.id, payload.contact_id)
                if payload.contact_id is not None
                else None
            )
            classifier_settings = await load_classifier_settings(session, connection.id)
            templates = await load_templates(session, connection.id)
        result = await classify(payload.text, settings=classifier_settings)
        template = (
            TemplateCode(forced_template) if forced_template else template_for(result.category)
        )
        text = render(template, overrides=templates)
        if connection.sender_identity == "bot":
            text = as_bot_reply(text)
        return {
            "decision": decision_code,
            "category": result.category.value,
            "template_code": template.value,
            "forced_template": forced_template is not None,
            "text": text,
            "dry_run": connection.dry_run,
            "timezone": connection.policy.timezone,
            "source": "keywords",
            "personal_schedule": bool(contact.windows),
        }

    @router.put("/api/v1/delivery")
    async def update_delivery(request: Request, payload: DeliveryPayload) -> dict[str, Any]:
        async with database.session() as session, session.begin():
            principal = await api.authorize(session, request)
            await set_delivery_preferences(
                session,
                principal.connection.id,
                sender_identity=payload.sender_identity,
                delay_min_seconds=payload.delay_min_seconds,
                delay_max_seconds=payload.delay_max_seconds,
                bot_delay_seconds=payload.bot_delay_seconds,
                mark_read=payload.mark_read,
                max_auto_replies_per_window=payload.max_auto_replies_per_window,
            )
            await session.refresh(principal.connection)
            return _delivery(principal.connection)

    @router.put("/api/v1/escalation")
    async def update_escalation(request: Request, payload: EscalationPayload) -> dict[str, Any]:
        async with database.session() as session, session.begin():
            principal = await api.authorize(session, request)
            connection = principal.connection
            connection.escalation_enabled = payload.enabled
            connection.escalation_price_amount = payload.price_amount
            connection.escalation_currency = payload.currency
            connection.escalation_offer_text = payload.offer_text.strip()
            connection.escalation_confirm_text = payload.confirm_text.strip()
            connection.escalation_decline_text = payload.decline_text.strip()
            await session.flush()
            return _escalation(connection)

    @router.put("/api/v1/schedule")
    async def update_schedule(request: Request, payload: SchedulePayload) -> dict[str, Any]:
        async with database.session() as session, session.begin():
            principal = await api.authorize(session, request)
            principal.connection.timezone = payload.timezone
            await session.execute(
                delete(models.Schedule).where(
                    models.Schedule.connection_id == principal.connection.id
                )
            )
            session.add_all(
                [
                    models.Schedule(
                        connection_id=principal.connection.id,
                        weekday_mask=window.weekday_mask,
                        time_from=window.time_from,
                        time_to=window.time_to,
                        is_active=window.is_active,
                    )
                    for window in payload.windows
                ]
            )
            await session.flush()
            return await _schedule(session, principal.connection)

    @router.get("/api/v1/contacts")
    async def contacts(
        request: Request,
        search: Annotated[str, Query(max_length=100)] = "",
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        async with database.session() as session, session.begin():
            principal = await api.authorize(session, request)
            items = await _contacts(session, principal.connection.id, search=search, offset=offset)
            return {"items": items[:100], "has_more": len(items) > 100, "next_offset": offset + 100}

    @router.put("/api/v1/contacts/{contact_id}")
    async def update_contact(
        request: Request,
        contact_id: int,
        payload: ContactPayload,
    ) -> dict[str, Any]:
        if contact_id < 1:
            raise HTTPException(status_code=422, detail="Невірний контакт")
        async with database.session() as session, session.begin():
            principal = await api.authorize(session, request)
            activity = await session.get(
                models.ContactActivity, (principal.connection.id, contact_id)
            )
            if activity is None:
                raise HTTPException(status_code=404, detail="Контакт не знайдено")
            await _save_contact(
                session,
                connection_id=principal.connection.id,
                contact_id=contact_id,
                contact_name=activity.contact_name,
                payload=payload,
            )
            return await _contact(session, principal.connection.id, contact_id)

    @router.put("/api/v1/templates")
    async def update_templates(request: Request, payload: TemplatesPayload) -> dict[str, Any]:
        async with database.session() as session, session.begin():
            principal = await api.authorize(session, request)
            for code in TemplateCode:
                row = await session.scalar(
                    select(models.Template).where(
                        models.Template.connection_id == principal.connection.id,
                        models.Template.code == code.value,
                    )
                )
                if row is None:
                    row = models.Template(
                        connection_id=principal.connection.id,
                        code=code.value,
                        text=getattr(payload, code.value),
                    )
                    session.add(row)
                else:
                    row.text = getattr(payload, code.value)
                    row.is_active = True
            await session.flush()
            return await _templates(session, principal.connection.id)

    @router.put("/api/v1/classifier")
    async def update_classifier(request: Request, payload: ClassifierPayload) -> dict[str, Any]:
        async with database.session() as session, session.begin():
            principal = await api.authorize(session, request)
            for direction in payload.directions:
                row = await session.scalar(
                    select(models.ClassificationDirection).where(
                        models.ClassificationDirection.connection_id == principal.connection.id,
                        models.ClassificationDirection.code == direction.code,
                    )
                )
                if row is None:
                    row = models.ClassificationDirection(
                        connection_id=principal.connection.id,
                        code=direction.code,
                        label=direction.label,
                        description=direction.description,
                    )
                    session.add(row)
                row.label = direction.label.strip()
                row.description = direction.description.strip()
                row.keywords_json = direction.keywords
                row.is_active = direction.is_active
            prompt = await session.scalar(
                select(models.Prompt).where(
                    models.Prompt.connection_id == principal.connection.id,
                    models.Prompt.code == "classifier",
                )
            )
            if prompt is None:
                prompt = models.Prompt(
                    connection_id=principal.connection.id,
                    code="classifier",
                    system_prompt=payload.system_prompt,
                )
                session.add(prompt)
            prompt.system_prompt = payload.system_prompt.strip()
            prompt.model = payload.model
            prompt.confidence_min = payload.confidence_min
            await session.flush()
            return await _classifier(session, principal.connection.id)

    @router.put("/api/v1/summary")
    async def update_summary(request: Request, payload: SummaryPayload) -> dict[str, Any]:
        async with database.session() as session, session.begin():
            principal = await api.authorize(session, request)
            if payload.message_retention_enabled and settings.message_encryption_key is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="На сервері не налаштовано ключ шифрування",
                )
            principal.connection.summary_time = payload.summary_time
            if summary_channel_connector is None:
                principal.connection.summary_channel_id = payload.summary_channel_id
                if payload.summary_channel_id is None:
                    principal.connection.summary_channel_title = None
            elif (
                payload.summary_channel_id is not None
                and payload.summary_channel_id != principal.connection.summary_channel_id
            ):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Підключіть канал через безпечний вибір у панелі",
                )
            elif payload.summary_channel_id is None:
                principal.connection.summary_channel_id = None
                principal.connection.summary_channel_title = None
            if payload.message_retention_enabled is not None:
                if not payload.message_retention_enabled:
                    await purge_retained_messages(session, connection_id=principal.connection.id)
                principal.connection.message_retention_enabled = payload.message_retention_enabled
            await session.flush()
            return await _summary(session, principal.connection)

    @router.post("/api/v1/summary/channel-request")
    async def request_summary_channel(request: Request) -> dict[str, str]:
        if summary_channel_connector is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Вибір каналу тимчасово недоступний",
            )
        async with database.session() as session, session.begin():
            principal = await api.authorize(session, request)
            try:
                prepared = await summary_channel_connector.prepare_request(
                    session,
                    connection=principal.connection,
                    owner_user_id=principal.user.user_id,
                )
            except SummaryChannelError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            return {
                "request_id": str(prepared.request_id),
                "prepared_id": prepared.prepared_id,
                "expires_in": "15m",
            }

    @router.get("/api/v1/summary/channel-request/{request_id}")
    async def summary_channel_request_status(request: Request, request_id: int) -> dict[str, Any]:
        async with database.session() as session, session.begin():
            principal = await api.authorize(session, request)
            row = await session.scalar(
                select(models.SummaryChannelRequest).where(
                    models.SummaryChannelRequest.id == request_id,
                    models.SummaryChannelRequest.connection_id == principal.connection.id,
                    models.SummaryChannelRequest.owner_user_id == principal.user.user_id,
                )
            )
            if row is None:
                raise HTTPException(status_code=404, detail="Запит не знайдено")
            request_status = row.status
            if request_status == "pending" and row.expires_at <= datetime.now(UTC):
                request_status = "error"
                row.status = "error"
                row.error_message = "Час вибору каналу минув. Спробуйте ще раз."
                row.consumed_at = datetime.now(UTC)
            return {
                "status": request_status,
                "error": row.error_message,
                "summary": await _summary(session, principal.connection),
            }

    @router.post("/api/v1/summary/channel")
    async def connect_summary_channel(
        request: Request, payload: SummaryChannelPayload
    ) -> dict[str, Any]:
        if summary_channel_connector is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Підключення каналу тимчасово недоступне",
            )
        async with database.session() as session, session.begin():
            principal = await api.authorize(session, request)
            try:
                await summary_channel_connector.connect_reference(
                    session,
                    connection=principal.connection,
                    owner_user_id=principal.user.user_id,
                    reference=payload.reference,
                )
            except SummaryChannelError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return await _summary(session, principal.connection)

    @router.delete("/api/v1/summary/channel")
    async def disconnect_summary_channel(request: Request) -> dict[str, Any]:
        async with database.session() as session, session.begin():
            principal = await api.authorize(session, request)
            principal.connection.summary_channel_id = None
            principal.connection.summary_channel_title = None
            await session.flush()
            return await _summary(session, principal.connection)

    @router.get("/api/v1/logs")
    async def logs(
        request: Request,
        contact_id: Annotated[int | None, Query(gt=0)] = None,
        action: str | None = None,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        if action is not None and action not in {
            item.value for item in LogAction if item is not LogAction.CAPTURED
        }:
            raise HTTPException(status_code=422, detail="Невідома дія")
        async with database.session() as session, session.begin():
            principal = await api.authorize(session, request)
            items = await _logs(
                session,
                principal.connection.id,
                contact_id=contact_id,
                action=action,
                offset=offset,
            )
            return {
                "items": items[:MAX_LOGS],
                "has_more": len(items) > MAX_LOGS,
                "next_offset": offset + MAX_LOGS,
            }

    @router.get("/api/v1/analytics")
    async def analytics(
        request: Request,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> dict[str, Any]:
        async with database.session() as session, session.begin():
            principal = await api.authorize(session, request)
            zone = ZoneInfo(principal.connection.timezone)
            local_today = datetime.now(zone).date()
            resolved_to = date_to or local_today
            resolved_from = date_from or (resolved_to - timedelta(days=29))
            try:
                local_period(resolved_from, resolved_to, principal.connection.timezone)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            return await build_analytics(
                session,
                connection=principal.connection,
                date_from=resolved_from,
                date_to=resolved_to,
            )

    @router.get("/api/v1/analytics/monthly.pdf")
    async def monthly_analytics_pdf(request: Request, month: str) -> Response:
        month_start, month_end = _month_bounds(month)
        async with database.session() as session, session.begin():
            principal = await api.authorize(session, request)
            report = await build_analytics(
                session,
                connection=principal.connection,
                date_from=month_start,
                date_to=month_end,
            )
        payload = render_monthly_pdf(report)
        return Response(
            content=payload,
            media_type="application/pdf",
            headers={
                "Content-Disposition": (f'attachment; filename="personal-secretary-{month}.pdf"')
            },
        )

    @router.post("/api/v1/analytics/monthly-link")
    async def monthly_analytics_link(request: Request, month: str) -> dict[str, str]:
        _month_bounds(month)
        async with database.session() as session, session.begin():
            principal = await api.authorize(session, request)
            token = secrets.token_urlsafe(32)
            session.add(
                models.PdfToken(
                    token_hash=hashlib.sha256(token.encode()).digest(),
                    user_id=principal.user.user_id,
                    month=month,
                    expires_at=datetime.now(UTC) + EXCHANGE_TTL,
                )
            )
        base = (settings.public_base_url or str(request.base_url)).rstrip("/")
        return {
            "url": f"{base}/web/analytics/{token}/{month}",
            "expires_in": "15m",
        }

    @router.post("/api/v1/auth/browser-link")
    async def browser_link(request: Request) -> dict[str, str]:
        async with database.session() as session, session.begin():
            principal = await api.authorize(session, request)
            token = await create_web_token(
                session,
                user_id=principal.user.user_id,
                kind="exchange",
                now=datetime.now(UTC),
                ttl=EXCHANGE_TTL,
            )
        base = (settings.public_base_url or str(request.base_url)).rstrip("/")
        return {"url": f"{base}/web/auth/{token}", "expires_in": "15m"}

    @router.get("/web/auth/{token}", name="exchange_browser_auth")
    async def exchange_browser_auth(token: str) -> Response:
        async with database.session() as session, session.begin():
            consumed = await consume_exchange(session, token=token, now=datetime.now(UTC))
        if consumed is None:
            raise _unauthorized()
        _, session_token = consumed
        response = RedirectResponse(url="/app/", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            SESSION_COOKIE,
            session_token,
            max_age=int(timedelta(days=30).total_seconds()),
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return response

    @router.get("/web/analytics/{token}/{month}")
    async def exchange_monthly_analytics(token: str, month: str) -> Response:
        month_start, month_end = _month_bounds(month)
        async with database.session() as session, session.begin():
            user_id = await session.scalar(
                update(models.PdfToken)
                .where(
                    models.PdfToken.token_hash == hashlib.sha256(token.encode()).digest(),
                    models.PdfToken.month == month,
                    models.PdfToken.expires_at > datetime.now(UTC),
                    models.PdfToken.consumed_at.is_(None),
                )
                .values(consumed_at=datetime.now(UTC))
                .returning(models.PdfToken.user_id)
            )
            user = None if user_id is None else await session.get(models.AccessUser, user_id)
            if user is None or user.status != "active" or user.onboarding_state != "ready":
                raise _unauthorized()
            connection = await session.scalar(
                select(models.Connection)
                .where(models.Connection.owner_user_id == user_id)
                .order_by(models.Connection.id.desc())
                .limit(1)
            )
            if connection is None:
                raise _unauthorized()
            report = await build_analytics(
                session, connection=connection, date_from=month_start, date_to=month_end
            )
        return Response(
            content=render_monthly_pdf(report),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="personal-secretary-{month}.pdf"',
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
            },
        )

    @router.post("/api/v1/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(request: Request) -> Response:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            async with database.session() as session, session.begin():
                await revoke_session(session, token=token)
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True)
        return response

    return router


def _month_bounds(month: str) -> tuple[date, date]:
    try:
        month_start = datetime.strptime(month, "%Y-%m").date().replace(day=1)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Місяць має формат РРРР-ММ") from exc
    if month_start.strftime("%Y-%m") != month:
        raise HTTPException(status_code=422, detail="Місяць має формат РРРР-ММ")
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return month_start, next_month - timedelta(days=1)


async def _bootstrap(session: AsyncSession, principal: Principal) -> dict[str, Any]:
    return {
        "user": {
            "id": principal.user.user_id,
            "username": principal.user.username,
            "role": principal.user.role,
        },
        "connection": {
            "id": principal.connection.id,
            "is_active": principal.connection.is_active,
            "dry_run": principal.connection.dry_run,
            "kill_switch": principal.connection.kill_switch,
            "muted_until": _iso(principal.connection.muted_until),
            "rights": dict(principal.connection.rights_json or {}),
        },
        "status": await operating_status(session, principal.connection),
        "delivery": _delivery(principal.connection),
        "escalation": _escalation(principal.connection),
        "schedule": await _schedule(session, principal.connection),
        "templates": await _templates(session, principal.connection.id),
        "classifier": await _classifier(session, principal.connection.id),
        "summary": await _summary(session, principal.connection),
    }


def _delivery(connection: models.Connection) -> dict[str, Any]:
    return {
        "sender_identity": connection.sender_identity,
        "delay_min_seconds": connection.delay_min_seconds,
        "delay_max_seconds": connection.delay_max_seconds,
        "bot_delay_seconds": connection.bot_delay_seconds,
        "mark_read": connection.mark_read,
        "max_auto_replies_per_window": connection.max_auto_replies_per_window,
    }


def _escalation(connection: models.Connection) -> dict[str, Any]:
    return {
        "enabled": connection.escalation_enabled,
        "price_amount": str(connection.escalation_price_amount),
        "currency": connection.escalation_currency,
        "offer_text": connection.escalation_offer_text,
        "confirm_text": connection.escalation_confirm_text,
        "decline_text": connection.escalation_decline_text,
    }


async def _schedule(session: AsyncSession, connection: models.Connection) -> dict[str, Any]:
    rows = await session.scalars(
        select(models.Schedule)
        .where(models.Schedule.connection_id == connection.id)
        .order_by(models.Schedule.id)
    )
    return {
        "timezone": connection.timezone,
        "windows": [_window(row) for row in rows],
    }


async def _templates(session: AsyncSession, connection_id: int) -> dict[str, str]:
    rows = await session.scalars(
        select(models.Template).where(
            models.Template.connection_id == connection_id,
            models.Template.is_active.is_(True),
        )
    )
    overrides = {row.code: row.text for row in rows}
    return {code.value: overrides.get(code.value, DEFAULT_TEMPLATES[code]) for code in TemplateCode}


async def _classifier(session: AsyncSession, connection_id: int) -> dict[str, Any]:
    rows = await session.scalars(
        select(models.ClassificationDirection)
        .where(models.ClassificationDirection.connection_id == connection_id)
        .order_by(models.ClassificationDirection.code)
    )
    stored = {row.code: row for row in rows}
    directions = []
    for code in ("general", "money"):
        row = stored.get(code)
        fallback = DEFAULT_DIRECTIONS[code]
        directions.append(
            {
                "code": code,
                "label": fallback["label"] if row is None else row.label,
                "description": fallback["description"] if row is None else row.description,
                "keywords": fallback["keywords"] if row is None else list(row.keywords_json or []),
                "is_active": fallback["is_active"] if row is None else row.is_active,
            }
        )
    prompt = await session.scalar(
        select(models.Prompt).where(
            models.Prompt.connection_id == connection_id,
            models.Prompt.code == "classifier",
        )
    )
    return {
        "directions": directions,
        "system_prompt": DEFAULT_SYSTEM_PROMPT if prompt is None else prompt.system_prompt,
        "model": DEFAULT_MODEL if prompt is None else prompt.model,
        "confidence_min": str(DEFAULT_CONFIDENCE_MIN if prompt is None else prompt.confidence_min),
    }


async def _summary(session: AsyncSession, connection: models.Connection) -> dict[str, Any]:
    retained = (
        await session.execute(
            select(
                func.count(models.MessageLog.id),
                func.coalesce(func.sum(func.length(models.MessageLog.body_encrypted)), 0),
                func.min(models.MessageLog.retention_until),
            ).where(
                models.MessageLog.connection_id == connection.id,
                models.MessageLog.action == LogAction.CAPTURED.value,
            )
        )
    ).one()
    return {
        "summary_time": connection.summary_time.isoformat(timespec="minutes"),
        "summary_channel_id": connection.summary_channel_id,
        "summary_channel_title": connection.summary_channel_title,
        "message_retention_enabled": connection.message_retention_enabled,
        "retention_hours": 48,
        "retained_message_count": retained[0],
        "retained_bytes": retained[1],
        "next_deletion_at": _iso(retained[2]),
    }


async def _contacts(
    session: AsyncSession, connection_id: int, *, search: str, offset: int = 0
) -> list[dict[str, Any]]:
    query = select(models.ContactActivity).where(
        models.ContactActivity.connection_id == connection_id
    )
    needle = search.strip().lstrip("@")
    if needle:
        query = query.where(
            or_(
                models.ContactActivity.contact_name.icontains(needle, autoescape=True),
                models.ContactActivity.contact_username.icontains(needle, autoescape=True),
            )
        )
    rows = list(
        await session.scalars(
            query.order_by(
                models.ContactActivity.last_incoming_at.desc(), models.ContactActivity.contact_id
            )
            .offset(offset)
            .limit(101)
        )
    )
    return await _contact_rows(session, connection_id, rows)


async def _contact(session: AsyncSession, connection_id: int, contact_id: int) -> dict[str, Any]:
    activity = await session.get(models.ContactActivity, (connection_id, contact_id))
    if activity is None:
        raise HTTPException(status_code=404, detail="Контакт не знайдено")
    return (await _contact_rows(session, connection_id, [activity]))[0]


async def _contact_rows(
    session: AsyncSession, connection_id: int, rows: list
) -> list[dict[str, Any]]:
    if not rows:
        return []
    ids = [row.contact_id for row in rows]
    now = datetime.now(UTC)
    exclusions = {
        row.contact_id: row
        for row in await session.scalars(
            select(models.Exclusion).where(
                models.Exclusion.connection_id == connection_id,
                models.Exclusion.contact_id.in_(ids),
            )
        )
    }
    windows: dict[int, list] = {}
    for row in await session.scalars(
        select(models.ContactWindow)
        .where(
            models.ContactWindow.connection_id == connection_id,
            models.ContactWindow.contact_id.in_(ids),
        )
        .order_by(models.ContactWindow.id)
    ):
        windows.setdefault(row.contact_id, []).append(_window(row))
    counts: dict[tuple[int, str], int] = {}
    for contact_id, action, count in (
        await session.execute(
            select(models.MessageLog.contact_id, models.MessageLog.action, func.count())
            .where(
                models.MessageLog.connection_id == connection_id,
                models.MessageLog.contact_id.in_(ids),
                models.MessageLog.action.in_(["replied", "dry_run"]),
                models.MessageLog.occurred_at >= now - LOG_RETENTION,
            )
            .group_by(models.MessageLog.contact_id, models.MessageLog.action)
        )
    ).all():
        counts[contact_id, action] = count
    result = []
    for activity in rows:
        contact_id = activity.contact_id
        exclusion = exclusions.get(contact_id)
        if exclusion is not None and exclusion.until is not None and exclusion.until <= now:
            exclusion = None
        result.append(
            {
                "contact_id": contact_id,
                "contact_name": activity.contact_name,
                "contact_username": activity.contact_username,
                "contact_label": contact_label(activity.contact_name, activity.contact_username),
                "last_incoming_at": _iso(activity.last_incoming_at),
                "last_auto_reply_at": _iso(activity.last_auto_reply_at),
                "auto_reply_count": counts.get((contact_id, "replied"), 0),
                "preview_count": counts.get((contact_id, "dry_run"), 0),
                "reply_period_days": 30,
                "off_hours_request_count": activity.off_hours_request_count,
                "paid_escalation_count": activity.paid_escalation_count,
                "exclusion": "none"
                if exclusion is None
                else "forever"
                if exclusion.until is None
                else "until",
                "exclusion_until": None if exclusion is None else _iso(exclusion.until),
                "windows": windows.get(contact_id, []),
            }
        )
    return result


async def _save_contact(
    session: AsyncSession,
    *,
    connection_id: int,
    contact_id: int,
    contact_name: str | None,
    payload: ContactPayload,
) -> None:
    await session.execute(
        delete(models.Exclusion).where(
            models.Exclusion.connection_id == connection_id,
            models.Exclusion.contact_id == contact_id,
        )
    )
    if payload.exclusion != "none":
        session.add(
            models.Exclusion(
                connection_id=connection_id,
                contact_id=contact_id,
                contact_name=contact_name,
                until=payload.exclusion_until if payload.exclusion == "until" else None,
                reason="web_settings",
            )
        )
    await session.execute(
        delete(models.ContactWindow).where(
            models.ContactWindow.connection_id == connection_id,
            models.ContactWindow.contact_id == contact_id,
        )
    )
    session.add_all(
        [
            models.ContactWindow(
                connection_id=connection_id,
                contact_id=contact_id,
                weekday_mask=window.weekday_mask,
                time_from=window.time_from,
                time_to=window.time_to,
                is_active=window.is_active,
            )
            for window in payload.windows
        ]
    )
    await session.flush()


async def _logs(
    session: AsyncSession,
    connection_id: int,
    *,
    contact_id: int | None,
    action: str | None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    query = (
        select(
            models.MessageLog,
            models.ContactActivity.contact_name,
            models.ContactActivity.contact_username,
        )
        .outerjoin(
            models.ContactActivity,
            (models.ContactActivity.connection_id == models.MessageLog.connection_id)
            & (models.ContactActivity.contact_id == models.MessageLog.contact_id),
        )
        .where(
            models.MessageLog.connection_id == connection_id,
            models.MessageLog.occurred_at >= datetime.now(UTC) - LOG_RETENTION,
            models.MessageLog.action != LogAction.CAPTURED.value,
        )
    )
    if contact_id is not None:
        query = query.where(models.MessageLog.contact_id == contact_id)
    if action is not None:
        query = query.where(models.MessageLog.action == action)
    rows = (
        await session.execute(
            query.order_by(models.MessageLog.occurred_at.desc(), models.MessageLog.id.desc())
            .offset(offset)
            .limit(MAX_LOGS + 1)
        )
    ).all()
    return [
        {
            "id": row.id,
            "contact_id": row.contact_id,
            "contact_name": contact_name,
            "contact_username": contact_username,
            "contact_label": contact_label(contact_name, contact_username),
            "occurred_at": _iso(row.occurred_at),
            "direction": row.direction,
            "action": row.action,
            "category": row.category,
            "confidence": None if row.confidence is None else str(row.confidence),
            "template_code": row.template_code,
            "error_code": row.error_code,
        }
        for row, contact_name, contact_username in rows
    ]


def _window(row: models.Schedule | models.ContactWindow) -> dict[str, Any]:
    return {
        "id": row.id,
        "weekday_mask": row.weekday_mask,
        "time_from": row.time_from.isoformat(timespec="minutes"),
        "time_to": row.time_to.isoformat(timespec="minutes"),
        "is_active": row.is_active,
    }


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.astimezone(UTC).isoformat()


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Потрібна авторизація")
