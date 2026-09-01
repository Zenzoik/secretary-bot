from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretary_bot import models

INIT_DATA_MAX_AGE = timedelta(hours=24)
EXCHANGE_TTL = timedelta(minutes=15)
SESSION_TTL = timedelta(days=30)
SESSION_COOKIE = "secretary_session"


class WebAuthError(ValueError):
    """Authentication data is invalid, expired or no longer authorized."""


@dataclass(frozen=True, slots=True)
class TelegramIdentity:
    user_id: int
    username: str | None
    auth_date: datetime


def validate_init_data(
    raw: str,
    *,
    bot_token: str,
    now: datetime | None = None,
    max_age: timedelta = INIT_DATA_MAX_AGE,
) -> TelegramIdentity:
    """Validate Telegram Mini App initData exactly as defined by Telegram."""
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise WebAuthError("invalid initData") from exc
    values = dict(pairs)
    received_hash = values.pop("hash", "")
    if len(received_hash) != 64 or len(values) != len(pairs) - 1:
        raise WebAuthError("invalid initData")

    data_check_string = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(received_hash, expected_hash):
        raise WebAuthError("invalid initData")

    moment = now or datetime.now(UTC)
    try:
        auth_date = datetime.fromtimestamp(int(values["auth_date"]), tz=UTC)
        user = json.loads(values["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError) as exc:
        raise WebAuthError("invalid initData") from exc
    if user_id < 1 or auth_date > moment + timedelta(minutes=5) or moment - auth_date > max_age:
        raise WebAuthError("expired initData")
    username = user.get("username")
    return TelegramIdentity(
        user_id=user_id,
        username=username[:32] if isinstance(username, str) and username else None,
        auth_date=auth_date,
    )


async def create_web_token(
    session: AsyncSession,
    *,
    user_id: int,
    kind: str,
    now: datetime,
    ttl: timedelta,
) -> str:
    if kind not in {"exchange", "session"}:
        raise ValueError("unsupported web token kind")
    token = secrets.token_urlsafe(32)
    session.add(
        models.WebSession(
            token_hash=_token_hash(token),
            user_id=user_id,
            kind=kind,
            expires_at=now + ttl,
        )
    )
    await session.flush()
    return token


async def consume_exchange(
    session: AsyncSession, *, token: str, now: datetime
) -> tuple[int, str] | None:
    result = await session.execute(
        update(models.WebSession)
        .where(
            models.WebSession.token_hash == _token_hash(token),
            models.WebSession.kind == "exchange",
            models.WebSession.consumed_at.is_(None),
            models.WebSession.expires_at > now,
        )
        .values(consumed_at=now)
        .returning(models.WebSession.user_id)
    )
    user_id = result.scalar_one_or_none()
    if user_id is None or not await _active_user(session, user_id):
        return None
    session_token = await create_web_token(
        session, user_id=user_id, kind="session", now=now, ttl=SESSION_TTL
    )
    return user_id, session_token


async def authenticate_session(session: AsyncSession, *, token: str, now: datetime) -> int | None:
    row = await session.scalar(
        select(models.WebSession).where(
            models.WebSession.token_hash == _token_hash(token),
            models.WebSession.kind == "session",
            models.WebSession.consumed_at.is_(None),
            models.WebSession.expires_at > now,
        )
    )
    if row is None or not await _active_user(session, row.user_id):
        return None
    row.last_used_at = now
    await session.flush()
    return row.user_id


async def revoke_session(session: AsyncSession, *, token: str) -> None:
    await session.execute(
        delete(models.WebSession).where(models.WebSession.token_hash == _token_hash(token))
    )


async def _active_user(session: AsyncSession, user_id: int) -> bool:
    return bool(
        await session.scalar(
            select(models.AccessUser.user_id).where(
                models.AccessUser.user_id == user_id,
                models.AccessUser.status == "active",
                models.AccessUser.onboarding_state == "ready",
            )
        )
    )


def _token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()
