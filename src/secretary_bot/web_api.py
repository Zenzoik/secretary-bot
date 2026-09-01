from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretary_bot import models
from secretary_bot.actions import LogAction
from secretary_bot.classifier import (
    DEFAULT_CONFIDENCE_MIN,
    DEFAULT_MODEL,
    DEFAULT_SYSTEM_PROMPT,
    MONEY_KEYWORDS,
)
from secretary_bot.config import Settings
from secretary_bot.storage import Database, set_delivery_preferences
from secretary_bot.templates import DEFAULT_TEMPLATES, TemplateCode
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
MAX_CONTACTS = 500
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

    @model_validator(mode="after")
    def validate_bounds(self) -> DeliveryPayload:
        if self.delay_min_seconds > self.delay_max_seconds:
            raise ValueError("Мінімальна затримка не може бути більшою за максимальну")
        if self.bot_delay_seconds > min(self.delay_max_seconds, 60):
            raise ValueError("Затримка бота виходить за дозволений діапазон")
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

    @field_validator("summary_channel_id")
    @classmethod
    def validate_channel_id(cls, value: int | None) -> int | None:
        if value == 0:
            raise ValueError("ID каналу не може дорівнювати нулю")
        return value


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
        elif self.exclusion_until is not None:
            raise ValueError("Дата потрібна лише для тимчасового виключення")
        return self


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


def build_web_router(*, database: Database, settings: Settings) -> APIRouter:
    router = APIRouter()
    api = WebApi(database=database, settings=settings)

    @router.get("/api/v1/bootstrap")
    async def bootstrap(request: Request) -> dict[str, Any]:
        async with database.session() as session, session.begin():
            principal = await api.authorize(session, request)
            return await _bootstrap(session, principal)

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
            )
            await session.refresh(principal.connection)
            return _delivery(principal.connection)

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
    ) -> dict[str, Any]:
        async with database.session() as session, session.begin():
            principal = await api.authorize(session, request)
            items = await _contacts(session, principal.connection.id, search=search)
            return {"items": items}

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
            principal.connection.summary_time = payload.summary_time
            principal.connection.summary_channel_id = payload.summary_channel_id
            await session.flush()
            return _summary(principal.connection)

    @router.get("/api/v1/logs")
    async def logs(
        request: Request,
        contact_id: Annotated[int | None, Query(gt=0)] = None,
        action: str | None = None,
    ) -> dict[str, Any]:
        if action is not None and action not in {item.value for item in LogAction}:
            raise HTTPException(status_code=422, detail="Невідома дія")
        async with database.session() as session, session.begin():
            principal = await api.authorize(session, request)
            return {
                "items": await _logs(
                    session,
                    principal.connection.id,
                    contact_id=contact_id,
                    action=action,
                )
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
            "rights": dict(principal.connection.rights_json or {}),
        },
        "delivery": _delivery(principal.connection),
        "schedule": await _schedule(session, principal.connection),
        "templates": await _templates(session, principal.connection.id),
        "classifier": await _classifier(session, principal.connection.id),
        "summary": _summary(principal.connection),
    }


def _delivery(connection: models.Connection) -> dict[str, Any]:
    return {
        "sender_identity": connection.sender_identity,
        "delay_min_seconds": connection.delay_min_seconds,
        "delay_max_seconds": connection.delay_max_seconds,
        "bot_delay_seconds": connection.bot_delay_seconds,
        "mark_read": connection.mark_read,
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


def _summary(connection: models.Connection) -> dict[str, Any]:
    return {
        "summary_time": connection.summary_time.isoformat(timespec="minutes"),
        "summary_channel_id": connection.summary_channel_id,
    }


async def _contacts(
    session: AsyncSession, connection_id: int, *, search: str
) -> list[dict[str, Any]]:
    query = (
        select(models.ContactActivity)
        .where(models.ContactActivity.connection_id == connection_id)
        .order_by(models.ContactActivity.last_incoming_at.desc())
        .limit(MAX_CONTACTS)
    )
    rows = list(await session.scalars(query))
    needle = search.strip().casefold()
    if needle:
        rows = [
            row
            for row in rows
            if needle in (row.contact_name or "").casefold() or needle in str(row.contact_id)
        ]
    return [await _contact(session, connection_id, row.contact_id) for row in rows]


async def _contact(session: AsyncSession, connection_id: int, contact_id: int) -> dict[str, Any]:
    activity = await session.get(models.ContactActivity, (connection_id, contact_id))
    if activity is None:
        raise HTTPException(status_code=404, detail="Контакт не знайдено")
    exclusion = await session.scalar(
        select(models.Exclusion).where(
            models.Exclusion.connection_id == connection_id,
            models.Exclusion.contact_id == contact_id,
        )
    )
    windows = await session.scalars(
        select(models.ContactWindow)
        .where(
            models.ContactWindow.connection_id == connection_id,
            models.ContactWindow.contact_id == contact_id,
        )
        .order_by(models.ContactWindow.id)
    )
    replies = await session.scalar(
        select(func.count())
        .select_from(models.MessageLog)
        .where(
            models.MessageLog.connection_id == connection_id,
            models.MessageLog.contact_id == contact_id,
            models.MessageLog.action.in_([LogAction.REPLIED.value, LogAction.DRY_RUN.value]),
            models.MessageLog.occurred_at >= datetime.now(UTC) - LOG_RETENTION,
        )
    )
    return {
        "contact_id": contact_id,
        "contact_name": activity.contact_name,
        "last_incoming_at": _iso(activity.last_incoming_at),
        "last_auto_reply_at": _iso(activity.last_auto_reply_at),
        "auto_reply_count": replies or 0,
        "exclusion": "none"
        if exclusion is None
        else "forever"
        if exclusion.until is None
        else "until",
        "exclusion_until": None if exclusion is None else _iso(exclusion.until),
        "windows": [_window(row) for row in windows],
    }


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
) -> list[dict[str, Any]]:
    query = select(models.MessageLog).where(
        models.MessageLog.connection_id == connection_id,
        models.MessageLog.occurred_at >= datetime.now(UTC) - LOG_RETENTION,
    )
    if contact_id is not None:
        query = query.where(models.MessageLog.contact_id == contact_id)
    if action is not None:
        query = query.where(models.MessageLog.action == action)
    rows = await session.scalars(
        query.order_by(models.MessageLog.occurred_at.desc()).limit(MAX_LOGS)
    )
    return [
        {
            "id": row.id,
            "contact_id": row.contact_id,
            "occurred_at": _iso(row.occurred_at),
            "direction": row.direction,
            "action": row.action,
            "category": row.category,
            "confidence": None if row.confidence is None else str(row.confidence),
            "template_code": row.template_code,
            "error_code": row.error_code,
        }
        for row in rows
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
