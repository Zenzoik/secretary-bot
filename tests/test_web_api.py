from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, time, timedelta
from urllib.parse import urlencode, urlparse

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from secretary_bot import models
from secretary_bot.actions import LogAction
from secretary_bot.config import Settings
from secretary_bot.gate import GateDecision, evaluate_gate
from secretary_bot.retention import MessageCipher
from secretary_bot.storage import (
    ConnectionSnapshot,
    Database,
    ensure_master,
    load_contact_state,
    load_owner_connection,
    log_decision,
    record_incoming,
    upsert_connection,
)
from secretary_bot.web_api import build_web_router

TOKEN = "123456:TEST_TOKEN"
NOW = datetime.now(UTC)


def signed_init_data(user_id: int, *, token: str = TOKEN) -> str:
    values = {
        "auth_date": str(int(NOW.timestamp())),
        "query_id": "AAEAAAE",
        "user": json.dumps({"id": user_id, "first_name": "Owner"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def headers(user_id: int = 42) -> dict[str, str]:
    return {"X-Telegram-Init-Data": signed_init_data(user_id)}


def web_app(database: Database, *, message_encryption_key: str | None = None) -> FastAPI:
    app = FastAPI()
    settings = Settings(
        bot_token=TOKEN,
        webhook_secret="secret",
        master_user_id=42,
        bot_username="secretary_test_bot",
        public_base_url="https://testserver",
        message_encryption_key=message_encryption_key,
    )
    app.include_router(build_web_router(database=database, settings=settings))
    return app


async def seed_owner(database: Database, *, user_id: int = 42, name: str = "Owner") -> int:
    async with database.session() as session, session.begin():
        if user_id == 42:
            await ensure_master(session, user_id, username=name)
        else:
            session.add(
                models.AccessUser(
                    user_id=user_id,
                    username=name,
                    status="active",
                    onboarding_state="ready",
                )
            )
        connection = await upsert_connection(
            session,
            ConnectionSnapshot(
                business_connection_id=f"connection-{user_id}",
                owner_user_id=user_id,
                owner_chat_id=user_id,
                rights={"can_reply": True},
            ),
        )
        session.add(
            models.Schedule(
                connection_id=connection.id,
                weekday_mask=127,
                time_from=time(22, 0),
                time_to=time(8, 0),
            )
        )
        return connection.id


@pytest.mark.asyncio
async def test_bootstrap_requires_valid_active_telegram_owner(database: Database) -> None:
    await seed_owner(database)
    transport = ASGITransport(app=web_app(database))
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        missing = await client.get("/api/v1/bootstrap")
        stranger = await client.get("/api/v1/bootstrap", headers=headers(99))
        tampered = await client.get(
            "/api/v1/bootstrap",
            headers={"X-Telegram-Init-Data": signed_init_data(42) + "x"},
        )
        owner = await client.get("/api/v1/bootstrap", headers=headers())

    assert missing.status_code == stranger.status_code == tampered.status_code == 401
    assert owner.status_code == 200
    assert owner.json()["user"]["id"] == 42
    assert owner.json()["schedule"]["timezone"] == "Europe/Kyiv"
    assert "business_connection_id" not in owner.text


@pytest.mark.asyncio
async def test_delivery_schedule_templates_classifier_and_summary_apply_immediately(
    database: Database,
) -> None:
    await seed_owner(database)
    transport = ASGITransport(app=web_app(database))
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        delivery = await client.put(
            "/api/v1/delivery",
            headers=headers(),
            json={
                "sender_identity": "owner",
                "delay_min_seconds": 12,
                "delay_max_seconds": 44,
                "bot_delay_seconds": 7,
                "mark_read": True,
                "max_auto_replies_per_window": 3,
            },
        )
        escalation = await client.put(
            "/api/v1/escalation",
            headers=headers(),
            json={
                "enabled": True,
                "price_amount": "250.00",
                "currency": "UAH",
                "offer_text": "Потрібна термінова відповідь?",
                "confirm_text": "Платне звернення підтверджено.",
                "decline_text": "Зараз терміново відповісти не вийде.",
            },
        )
        schedule = await client.put(
            "/api/v1/schedule",
            headers=headers(),
            json={
                "timezone": "Europe/Prague",
                "windows": [
                    {
                        "weekday_mask": 31,
                        "time_from": "18:00",
                        "time_to": "09:00",
                        "is_active": True,
                    }
                ],
            },
        )
        templates = await client.put(
            "/api/v1/templates",
            headers=headers(),
            json={"off_hours_default": "Напишу вранці", "money_priority": "Оплату побачив"},
        )
        classifier = await client.put(
            "/api/v1/classifier",
            headers=headers(),
            json={
                "directions": [
                    {
                        "code": "general",
                        "label": "Інше",
                        "description": "Звичайні звернення",
                        "keywords": [],
                        "is_active": True,
                    },
                    {
                        "code": "money",
                        "label": "Оплата",
                        "description": "Усе про гроші",
                        "keywords": ["гонорар"],
                        "is_active": True,
                    },
                ],
                "system_prompt": "Класифікуй повідомлення обережно і повертай лише JSON.",
                "model": "claude-sonnet-4-6",
                "confidence_min": "0.82",
            },
        )
        summary = await client.put(
            "/api/v1/summary",
            headers=headers(),
            json={"summary_time": "08:30", "summary_channel_id": -1001234567890},
        )

    assert delivery.json()["sender_identity"] == "owner"
    assert delivery.json()["max_auto_replies_per_window"] == 3
    assert escalation.json() == {
        "enabled": True,
        "price_amount": "250.00",
        "currency": "UAH",
        "offer_text": "Потрібна термінова відповідь?",
        "confirm_text": "Платне звернення підтверджено.",
        "decline_text": "Зараз терміново відповісти не вийде.",
    }
    assert schedule.json()["windows"][0]["weekday_mask"] == 31
    assert templates.json()["money_priority"] == "Оплату побачив"
    assert classifier.json()["directions"][1]["keywords"] == ["гонорар"]
    assert summary.json()["summary_channel_id"] == -1001234567890
    assert summary.json()["message_retention_enabled"] is False
    async with database.session() as session:
        connection = await load_owner_connection(session, 42)
        assert connection is not None
        assert connection.sender_identity == "owner"
        assert connection.policy.timezone == "Europe/Prague"
        assert connection.policy.windows[0].weekday_mask == 31


@pytest.mark.asyncio
async def test_paid_escalation_requires_positive_price_when_enabled(database: Database) -> None:
    await seed_owner(database)
    transport = ASGITransport(app=web_app(database))
    payload = {
        "enabled": True,
        "price_amount": "0",
        "currency": "UAH",
        "offer_text": "Пропозиція",
        "confirm_text": "Підтверджено",
        "decline_text": "Відмовлено",
    }
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        response = await client.put(
            "/api/v1/escalation", headers=headers(), json=payload
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_summary_retention_requires_key_reports_usage_and_purges_on_disable(
    database: Database,
) -> None:
    connection_id = await seed_owner(database)
    payload = {
        "summary_time": "09:00",
        "summary_channel_id": -1001234567890,
        "message_retention_enabled": True,
    }
    transport = ASGITransport(app=web_app(database))
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        missing_key = await client.put("/api/v1/summary", headers=headers(), json=payload)

    assert missing_key.status_code == 409
    assert "ключ" in missing_key.json()["detail"]

    key = MessageCipher.generate_encoded_key()
    transport = ASGITransport(app=web_app(database, message_encryption_key=key))
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        enabled = await client.put("/api/v1/summary", headers=headers(), json=payload)
        async with database.session() as session, session.begin():
            session.add(
                models.MessageLog(
                    connection_id=connection_id,
                    contact_id=100,
                    tg_message_id=7,
                    direction="in",
                    action=LogAction.CAPTURED.value,
                    body_encrypted=b"encrypted-body",
                    retention_until=NOW + timedelta(hours=48),
                )
            )
        usage = await client.get("/api/v1/bootstrap", headers=headers())
        disabled = await client.put(
            "/api/v1/summary",
            headers=headers(),
            json={**payload, "message_retention_enabled": False},
        )

    assert enabled.status_code == 200
    assert enabled.json()["message_retention_enabled"] is True
    assert enabled.json()["retention_hours"] == 48
    assert usage.json()["summary"]["retained_message_count"] == 1
    assert usage.json()["summary"]["retained_bytes"] == len(b"encrypted-body")
    assert disabled.json()["message_retention_enabled"] is False
    assert disabled.json()["retained_message_count"] == 0
    async with database.session() as session:
        retained = await session.scalar(
            select(func.count())
            .select_from(models.MessageLog)
            .where(models.MessageLog.action == LogAction.CAPTURED.value)
        )
    assert retained == 0


@pytest.mark.asyncio
async def test_contacts_support_exclusions_personal_windows_and_owner_isolation(
    database: Database,
) -> None:
    first_id = await seed_owner(database)
    second_id = await seed_owner(database, user_id=99, name="Other")
    async with database.session() as session, session.begin():
        await record_incoming(session, first_id, 100, at=NOW, contact_name="Test Contact")
        await record_incoming(session, second_id, 100, at=NOW, contact_name="Foreign Contact")

    transport = ASGITransport(app=web_app(database))
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        saved = await client.put(
            "/api/v1/contacts/100",
            headers=headers(),
            json={
                "exclusion": "until",
                "exclusion_until": (NOW + timedelta(hours=2)).isoformat(),
                "windows": [
                    {
                        "weekday_mask": 127,
                        "time_from": "10:00",
                        "time_to": "12:00",
                        "is_active": True,
                    }
                ],
            },
        )
        contacts = await client.get("/api/v1/contacts?search=Test", headers=headers())

    assert saved.status_code == 200
    assert saved.json()["contact_name"] == "Test Contact"
    assert contacts.json()["items"][0]["contact_name"] == "Test Contact"
    assert "Foreign Contact" not in contacts.text
    async with database.session() as session:
        connection = await load_owner_connection(session, 42)
        state = await load_contact_state(session, first_id, 100)
        assert connection is not None
        assert state.exclusion is not None
        assert state.windows
        assert evaluate_gate(connection.policy, state, now=NOW).decision is (
            GateDecision.SKIPPED_EXCLUDED
        )


@pytest.mark.asyncio
async def test_log_is_limited_to_30_days_and_filters_without_message_bodies(
    database: Database,
) -> None:
    connection_id = await seed_owner(database)
    async with database.session() as session, session.begin():
        await log_decision(
            session,
            connection_id=connection_id,
            contact_id=100,
            action=LogAction.REPLIED,
            category="general",
            occurred_at=NOW,
        )
        await log_decision(
            session,
            connection_id=connection_id,
            contact_id=200,
            action=LogAction.ERROR,
            occurred_at=NOW - timedelta(days=31),
        )
        session.add(
            models.MessageLog(
                connection_id=connection_id,
                contact_id=100,
                direction="in",
                action=LogAction.CAPTURED.value,
                body_encrypted=b"ciphertext",
                retention_until=NOW + timedelta(hours=48),
            )
        )

    transport = ASGITransport(app=web_app(database))
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        response = await client.get("/api/v1/logs?contact_id=100&action=replied", headers=headers())
        unfiltered = await client.get("/api/v1/logs", headers=headers())
        captured = await client.get("/api/v1/logs?action=captured", headers=headers())

    assert response.status_code == 200
    assert len(response.json()["items"]) == 1
    assert response.json()["items"][0]["contact_id"] == 100
    assert "body" not in response.text
    assert [item["action"] for item in unfiltered.json()["items"]] == ["replied"]
    assert captured.status_code == 422


@pytest.mark.asyncio
async def test_browser_link_is_one_time_and_creates_an_http_only_session(
    database: Database,
) -> None:
    await seed_owner(database)
    transport = ASGITransport(app=web_app(database))
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        link = await client.post("/api/v1/auth/browser-link", headers=headers())
        path = urlparse(link.json()["url"]).path
        exchange = await client.get(path, follow_redirects=False)
        browser_bootstrap = await client.get("/api/v1/bootstrap")
        replay = await client.get(path, follow_redirects=False)

    assert link.status_code == 200
    assert exchange.status_code == 303
    assert "HttpOnly" in exchange.headers["set-cookie"]
    assert "Secure" in exchange.headers["set-cookie"]
    assert browser_bootstrap.status_code == 200
    assert replay.status_code == 401


@pytest.mark.asyncio
async def test_updates_cannot_mutate_another_owners_connection(database: Database) -> None:
    first_id = await seed_owner(database)
    second_id = await seed_owner(database, user_id=99, name="Other")
    transport = ASGITransport(app=web_app(database))
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        response = await client.put(
            "/api/v1/delivery",
            headers=headers(99),
            json={
                "sender_identity": "owner",
                "delay_min_seconds": 20,
                "delay_max_seconds": 30,
                "bot_delay_seconds": 5,
                "mark_read": False,
            },
        )

    assert response.status_code == 200
    async with database.session() as session:
        first = await session.get(models.Connection, first_id)
        second = await session.get(models.Connection, second_id)
        assert first is not None and first.sender_identity == "bot"
        assert second is not None and second.sender_identity == "owner"
