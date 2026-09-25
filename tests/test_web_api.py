from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from io import BytesIO
from urllib.parse import urlencode, urlparse

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pypdf import PdfReader
from sqlalchemy import func, select, update

from secretary_bot import models
from secretary_bot.actions import LogAction
from secretary_bot.config import Settings
from secretary_bot.gate import GateDecision, evaluate_gate
from secretary_bot.retention import MessageCipher
from secretary_bot.storage import (
    ConnectionSnapshot,
    Database,
    consume_access_invite,
    ensure_master,
    load_access_user,
    load_contact_state,
    load_owner_connection,
    log_decision,
    record_incoming,
    record_owner_reply,
    upsert_connection,
)
from secretary_bot.templates import DEFAULT_TEMPLATES, TemplateCode
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


def web_app(
    database: Database,
    *,
    message_encryption_key: str | None = None,
    bot: object | None = None,
    require_contact_setup: bool = True,
) -> FastAPI:
    app = FastAPI()
    settings = Settings(
        bot_token=TOKEN,
        webhook_secret="secret",
        master_user_id=42,
        bot_username="secretary_test_bot",
        public_base_url="https://testserver",
        message_encryption_key=message_encryption_key,
        require_contact_setup=require_contact_setup,
    )
    app.include_router(build_web_router(database=database, settings=settings, bot=bot))
    return app


@pytest.mark.asyncio
async def test_master_manages_invites_and_approval_in_mini_app(database: Database) -> None:
    await seed_owner(database)
    await seed_owner(database, user_id=77)

    class AccessBot:
        def __init__(self) -> None:
            self.menu: list[dict] = []
            self.messages: list[dict] = []

        async def set_chat_menu_button(self, **kwargs: object) -> None:
            self.menu.append(kwargs)

        async def send_message(self, **kwargs: object) -> None:
            self.messages.append(kwargs)

    bot = AccessBot()
    async with AsyncClient(
        transport=ASGITransport(app=web_app(database, bot=bot)), base_url="https://testserver"
    ) as client:
        invited = await client.post("/api/v1/access/invites", headers=headers())
        assert invited.status_code == 200
        assert invited.json()["url"].startswith("https://t.me/secretary_test_bot?start=invite_")
        token = invited.json()["url"].split("invite_", 1)[1]
        async with database.session() as session, session.begin():
            await consume_access_invite(
                session, token=token, user_id=99, username="candidate", now=datetime.now(UTC)
            )

        denied = await client.post("/api/v1/access/users/99/approve", headers=headers(99))
        assert denied.status_code == 401
        assert (await client.get("/api/v1/access/users", headers=headers(77))).status_code == 403
        users = await client.get("/api/v1/access/users", headers=headers())
        assert users.status_code == 200
        assert any(
            user["user_id"] == 99 and user["status"] == "pending"
            for user in users.json()["users"]
        )
        approved = await client.post("/api/v1/access/users/99/approve", headers=headers())
        assert approved.json() == {"approved": True, "notified": True}
        assert (await client.get("/api/v1/bootstrap", headers=headers(99))).status_code == 409
        assert bot.menu[0]["chat_id"] == 99
        assert bot.messages[0]["reply_markup"].inline_keyboard[0][0].web_app.url == "https://testserver/app/"
        repeated = await client.post("/api/v1/access/users/99/approve", headers=headers())
        assert repeated.status_code == 409
        revoked = await client.post("/api/v1/access/users/99/revoke", headers=headers())
        assert revoked.json() == {"revoked": True}
    async with database.session() as session:
        user = await load_access_user(session, 99)
        assert user is not None and user.status == "revoked"


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
async def test_three_day_analytics_and_monthly_pdf_include_all_contacts(
    database: Database,
) -> None:
    connection_id = await seed_owner(database)
    period_start = datetime(2026, 8, 31, 21, 0, tzinfo=UTC)  # 01.09 00:00 Kyiv
    async with database.session() as session, session.begin():
        session.add_all(
            [
                models.ContactActivity(
                    connection_id=connection_id,
                    contact_id=101,
                    contact_name="Олена Клієнт",
                    contact_username="olena",
                ),
                models.ContactActivity(
                    connection_id=connection_id,
                    contact_id=202,
                    contact_name="Тарас Замовник",
                ),
                models.ContactActivity(
                    connection_id=connection_id,
                    contact_id=303,
                    contact_name="Контакт без подій",
                ),
            ]
        )
        session.add_all(
            [
                models.MessageLog(
                    connection_id=connection_id,
                    contact_id=101,
                    tg_message_id=1,
                    direction="in",
                    occurred_at=period_start + timedelta(hours=1),
                    action=LogAction.REPLIED.value,
                    category="general",
                ),
                models.MessageLog(
                    connection_id=connection_id,
                    contact_id=101,
                    tg_message_id=2,
                    direction="out",
                    occurred_at=period_start + timedelta(days=1, hours=1),
                    action=LogAction.REPLIED.value,
                    category="money",
                ),
                models.MessageLog(
                    connection_id=connection_id,
                    contact_id=202,
                    tg_message_id=3,
                    direction="in",
                    occurred_at=period_start + timedelta(days=2, hours=1),
                    action=LogAction.DRY_RUN.value,
                    category="money",
                ),
                models.MessageLog(
                    connection_id=connection_id,
                    contact_id=101,
                    tg_message_id=4,
                    direction="in",
                    occurred_at=period_start + timedelta(hours=2),
                    action=LogAction.CAPTURED.value,
                    body_encrypted=b"not-an-analytics-event",
                ),
                models.MessageLog(
                    connection_id=connection_id,
                    contact_id=101,
                    tg_message_id=5,
                    direction="in",
                    occurred_at=period_start - timedelta(seconds=1),
                    action=LogAction.REPLIED.value,
                    category="general",
                ),
            ]
        )
        request_deadline = period_start + timedelta(days=10)
        session.add_all(
            [
                models.ContactRequest(
                    connection_id=connection_id,
                    contact_id=101,
                    tg_message_id=11,
                    category="general",
                    occurred_at=period_start + timedelta(hours=1),
                    status="normal",
                    offer_expires_at=request_deadline,
                ),
                models.ContactRequest(
                    connection_id=connection_id,
                    contact_id=101,
                    tg_message_id=12,
                    category="money",
                    occurred_at=period_start + timedelta(days=1, hours=1),
                    status="paid",
                    price_amount=Decimal("1250.50"),
                    currency="UAH",
                    offer_expires_at=request_deadline,
                ),
                models.ContactRequest(
                    connection_id=connection_id,
                    contact_id=202,
                    tg_message_id=13,
                    category="money",
                    occurred_at=period_start + timedelta(days=2, hours=1),
                    status="offered",
                    offer_expires_at=request_deadline,
                ),
            ]
        )
        for index in range(3):
            run = models.SummaryRun(
                connection_id=connection_id,
                period_start=period_start + timedelta(days=index),
                period_end=period_start + timedelta(days=index + 1),
                status="delivered",
            )
            session.add(run)
            await session.flush()
            session.add(
                models.SummaryItem(
                    run_id=run.id,
                    contact_id=101,
                    contact_name="Олена Клієнт",
                    contact_username="olena",
                    topic="Оплата",
                    questions_asked=index + 1,
                    questions_closed=index,
                )
            )

    transport = ASGITransport(app=web_app(database))
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        response = await client.get(
            "/api/v1/analytics?date_from=2026-09-01&date_to=2026-09-03",
            headers=headers(),
        )
        pdf_response = await client.get(
            "/api/v1/analytics/monthly.pdf?month=2026-09",
            headers=headers(),
        )
        link_response = await client.post(
            "/api/v1/analytics/monthly-link?month=2026-09",
            headers=headers(),
        )
        browser_pdf = await client.get(link_response.json()["url"], follow_redirects=True)
        reused_link = await client.get(link_response.json()["url"], follow_redirects=False)

    assert response.status_code == 200
    payload = response.json()
    assert payload["period"]["started_at"] == "2026-08-31T21:00:00+00:00"
    assert payload["totals"] == {
        "contacts": 3,
        "messages": 3,
        "ordinary_requests": 2,
        "paid_requests": 1,
        "questions_asked": 6,
        "questions_closed": 3,
        "paid_amounts": {"UAH": "1250.50"},
    }
    by_id = {item["contact_id"]: item for item in payload["items"]}
    assert by_id[101]["messages"] == 2
    assert by_id[101]["contact_label"] == "Олена Клієнт · @olena"
    assert by_id[101]["message_directions"] == {"in": 1, "out": 1}
    assert by_id[101]["ordinary_requests"] == 1
    assert by_id[101]["paid_requests"] == 1
    assert by_id[101]["request_categories"] == {"general": 1, "money": 1}
    assert by_id[101]["questions_asked"] == 6
    assert by_id[101]["questions_closed"] == 3
    assert by_id[202]["ordinary_requests"] == 1
    assert by_id[303]["messages"] == by_id[303]["requests_total"] == 0

    assert pdf_response.status_code == 200
    assert pdf_response.headers["content-type"] == "application/pdf"
    assert "personal-secretary-2026-09.pdf" in pdf_response.headers["content-disposition"]
    pdf_reader = PdfReader(BytesIO(pdf_response.content))
    pdf_text = "\n".join(page.extract_text() or "" for page in pdf_reader.pages)
    assert "Олена Клієнт" in pdf_text
    assert "Тарас Замовник" in pdf_text
    assert "Контакт без подій" in pdf_text
    assert "1250.50 UAH" in pdf_text
    assert link_response.status_code == 200
    assert browser_pdf.status_code == 200
    assert browser_pdf.headers["content-type"] == "application/pdf"
    assert browser_pdf.content.startswith(b"%PDF-")
    assert reused_link.status_code == 401


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
        response = await client.put("/api/v1/escalation", headers=headers(), json=payload)

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
        await record_incoming(
            session,
            first_id,
            100,
            at=NOW,
            contact_name=".",
            contact_username="test_contact",
        )
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
        contacts = await client.get("/api/v1/contacts?search=@test_contact", headers=headers())

    assert saved.status_code == 200
    assert saved.json()["contact_label"] == "@test_contact"
    assert contacts.json()["items"][0]["contact_label"] == "@test_contact"
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
async def test_saving_a_new_contact_lets_the_bot_answer_it(database: Database) -> None:
    connection_id = await seed_owner(database)
    async with database.session() as session, session.begin():
        await record_incoming(session, connection_id, 100, at=NOW, contact_name="Reviewed")
        await session.execute(update(models.ContactActivity).values(configured_at=NOW))
        await record_incoming(
            session, connection_id, 101, at=NOW - timedelta(days=1), contact_name="Newcomer"
        )
        await record_owner_reply(session, connection_id, 102, at=NOW, contact_name="Silent")

    transport = ASGITransport(app=web_app(database))
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        before = (await client.get("/api/v1/contacts", headers=headers())).json()["items"]
        saved = await client.put(
            "/api/v1/contacts/101", headers=headers(), json={"exclusion": "none", "windows": []}
        )
        after = (await client.get("/api/v1/contacts", headers=headers())).json()["items"]

    # Contacts waiting for setup come first; one that never wrote sorts after one that did.
    assert [(item["contact_id"], item["configured"]) for item in before] == [
        (101, False),
        (102, False),
        (100, True),
    ]
    assert saved.status_code == 200 and saved.json()["configured"] is True
    assert [item["contact_id"] for item in after] == [102, 100, 101]
    async with database.session() as session:
        state = await load_contact_state(session, connection_id, 101)
    assert state.configured


@pytest.mark.asyncio
async def test_disabled_setup_rule_shows_every_contact_as_ready(database: Database) -> None:
    connection_id = await seed_owner(database)
    async with database.session() as session, session.begin():
        await record_incoming(session, connection_id, 101, at=NOW, contact_name="Newcomer")

    transport = ASGITransport(app=web_app(database, require_contact_setup=False))
    async with AsyncClient(transport=transport, base_url="https://testserver") as client:
        contacts = await client.get("/api/v1/contacts", headers=headers())
        preview = await client.post(
            "/api/v1/preview", headers=headers(), json={"text": "привіт", "contact_id": 101}
        )

    assert contacts.json()["items"][0]["configured"] is True
    assert preview.json()["decision"] != "skipped_unconfigured"


@pytest.mark.asyncio
async def test_log_is_limited_to_30_days_and_filters_without_message_bodies(
    database: Database,
) -> None:
    connection_id = await seed_owner(database)
    async with database.session() as session, session.begin():
        await record_incoming(
            session,
            connection_id,
            100,
            at=NOW,
            contact_name=".",
            contact_username="journal_contact",
        )
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
    assert response.json()["items"][0]["contact_label"] == "@journal_contact"
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


@pytest.mark.asyncio
async def test_custom_direction_roundtrip_and_tenant_isolation(database):
    from secretary_bot.storage import load_classifier_settings, load_templates

    owner = await seed_owner(database)
    other = await seed_owner(database, user_id=43)
    async with AsyncClient(
        transport=ASGITransport(app=web_app(database)), base_url="https://testserver"
    ) as client:
        payload = (await client.get("/api/v1/bootstrap", headers=headers())).json()["classifier"]
        payload["directions"].append(
            {
                "code": "support",
                "label": "Підтримка",
                "description": "Питання про помилки",
                "reply_template": "Перевірю проблему",
                "priority": "high",
                "keywords": [],
            }
        )
        result = await client.put("/api/v1/classifier", headers=headers(), json=payload)
        assert result.status_code == 200
        assert result.json()["directions"][-1]["reply_template"] == "Перевірю проблему"
        async with database.session() as session:
            assert "support" in (await load_classifier_settings(session, owner)).active_categories
            assert (
                "support" not in (await load_classifier_settings(session, other)).active_categories
            )
            assert (await load_templates(session, owner))[
                "direction_support"
            ] == "Перевірю проблему"
        payload["directions"][-1]["is_active"] = False
        assert (
            await client.put("/api/v1/classifier", headers=headers(), json=payload)
        ).status_code == 200
        async with database.session() as session:
            assert (
                "support" not in (await load_classifier_settings(session, owner)).active_categories
            )
        payload["directions"].pop()
        assert (
            await client.put("/api/v1/classifier", headers=headers(), json=payload)
        ).status_code == 200
        async with database.session() as session:
            assert (
                await session.scalar(
                    select(models.ClassificationDirection).where(
                        models.ClassificationDirection.code == "support"
                    )
                )
                is None
            )


@pytest.mark.asyncio
async def test_custom_direction_requires_reply_template(database):
    await seed_owner(database)
    async with AsyncClient(
        transport=ASGITransport(app=web_app(database)), base_url="https://testserver"
    ) as client:
        payload = (await client.get("/api/v1/bootstrap", headers=headers())).json()["classifier"]
        payload["directions"].append(
            {
                "code": "support",
                "label": "Підтримка",
                "description": "Проблеми зі входом",
                "reply_template": "   ",
                "priority": "normal",
                "keywords": [],
            }
        )

        save = await client.put("/api/v1/classifier", headers=headers(), json=payload)
        expand = await client.post(
            "/api/v1/classifier/expand", headers=headers(), json=payload
        )

    assert save.status_code == 422
    assert expand.status_code == 422
    assert "відповідь клієнту" in save.text


@pytest.mark.asyncio
async def test_built_in_type_reply_is_the_shared_template(database):
    from secretary_bot.storage import load_templates

    owner = await seed_owner(database)
    async with database.session() as session, session.begin():
        # A per-type text saved by the old editor is what the bot sends today.
        session.add(
            models.ClassificationDirection(
                connection_id=owner,
                code="money",
                label="Гроші",
                description="Оплата",
                reply_template="Старий текст про оплату",
                priority="high",
            )
        )
    async with AsyncClient(
        transport=ASGITransport(app=web_app(database)), base_url="https://testserver"
    ) as client:
        payload = (await client.get("/api/v1/bootstrap", headers=headers())).json()["classifier"]
        replies = {d["code"]: d["reply_template"] for d in payload["directions"]}
        assert replies == {
            "general": DEFAULT_TEMPLATES[TemplateCode.OFF_HOURS_DEFAULT],
            "money": "Старий текст про оплату",
        }

        for direction in payload["directions"]:
            direction.pop("priority")
            if direction["code"] == "general":
                direction["reply_template"] = "  Відповім зранку  "
        saved = await client.put("/api/v1/classifier", headers=headers(), json=payload)
        assert saved.status_code == 200
        assert {d["code"]: d["reply_template"] for d in saved.json()["directions"]} == {
            "general": "Відповім зранку",
            "money": "Старий текст про оплату",
        }
        async with database.session() as session:
            templates = await load_templates(session, owner)
            money = await session.scalar(
                select(models.ClassificationDirection).where(
                    models.ClassificationDirection.code == "money"
                )
            )
        # One source per reply: the templates the contact menu in the bot also uses.
        assert templates["off_hours_default"] == "Відповім зранку"
        assert templates["money_priority"] == "Старий текст про оплату"
        assert "direction_general" not in templates and "direction_money" not in templates
        assert money is not None and money.reply_template == "" and money.priority == "high"

        payload["directions"][0]["reply_template"] = ""
        assert (
            await client.put("/api/v1/classifier", headers=headers(), json=payload)
        ).status_code == 200
        async with database.session() as session:
            assert (await load_templates(session, owner))["off_hours_default"] == "Відповім зранку"

        # Priority is hidden in the panel, so a save without it keeps what is stored.
        async with database.session() as session, session.begin():
            money = await session.scalar(
                select(models.ClassificationDirection).where(
                    models.ClassificationDirection.code == "money"
                )
            )
            assert money is not None
            money.priority = "normal"
        payload["directions"].append(
            {
                "code": "support",
                "label": "Підтримка",
                "description": "Помилки",
                "reply_template": "Перевірю",
                "keywords": [],
            }
        )
        assert (
            await client.put("/api/v1/classifier", headers=headers(), json=payload)
        ).status_code == 200
        async with database.session() as session:
            priorities = dict(
                (
                    await session.execute(
                        select(
                            models.ClassificationDirection.code,
                            models.ClassificationDirection.priority,
                        )
                    )
                ).all()
            )
        assert priorities == {"general": "normal", "money": "normal", "support": "normal"}


@pytest.mark.asyncio
async def test_contact_stats_count_each_contact_once(database):
    owner = await seed_owner(database)
    other = await seed_owner(database, user_id=43)
    now = datetime.now(UTC)
    async with database.session() as session, session.begin():
        session.add_all(
            [
                models.ContactActivity(connection_id=owner, contact_id=1, configured_at=now),
                models.ContactActivity(connection_id=owner, contact_id=2, configured_at=now),
                models.ContactActivity(connection_id=owner, contact_id=3, configured_at=now),
                models.ContactActivity(connection_id=owner, contact_id=4, configured_at=now),
                models.ContactActivity(connection_id=owner, contact_id=5),
                models.ContactActivity(connection_id=other, contact_id=9, configured_at=now),
                models.Exclusion(connection_id=owner, contact_id=2, until=None),
                models.Exclusion(
                    connection_id=owner, contact_id=3, until=now + timedelta(days=1)
                ),
                # An expired pause counts as answered again.
                models.Exclusion(
                    connection_id=owner, contact_id=4, until=now - timedelta(minutes=1)
                ),
            ]
        )
    async with AsyncClient(
        transport=ASGITransport(app=web_app(database)), base_url="https://testserver"
    ) as client:
        stats = await client.get("/api/v1/contacts/stats", headers=headers())

    assert stats.status_code == 200
    assert stats.json() == {"active": 2, "paused": 1, "never": 1, "new": 1}


@pytest.mark.asyncio
async def test_expansion_is_authenticated_preview_and_errors_preserve_prompt(database):
    await seed_owner(database)

    class Generator:
        calls = 0
        output = json.dumps(
            {
                "system_prompt": "Класифікуй: general — інше, money — оплата.",
                "directions": [
                    {"code": "general", "keywords": []},
                    {"code": "money", "keywords": ["Оплата", "рахунок", "оплата"]},
                ],
            }
        )

        async def expand_classifier(self, text, *, model):
            self.calls += 1
            return self.output

    generator = Generator()
    app = FastAPI()
    app.include_router(
        build_web_router(
            database=database,
            settings=Settings(bot_token=TOKEN, webhook_secret="secret", master_user_id=42),
            language_model=generator,
        )
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        payload = (await client.get("/api/v1/bootstrap", headers=headers())).json()["classifier"]
        assert (await client.post("/api/v1/classifier/expand", json=payload)).status_code == 401
        assert generator.calls == 0
        result = await client.post("/api/v1/classifier/expand", headers=headers(), json=payload)
        assert result.status_code == 200
        saved = (await client.get("/api/v1/bootstrap", headers=headers())).json()["classifier"]
        assert saved["system_prompt"] == payload["system_prompt"]
        assert result.json()["directions"][1]["keywords"] == ["оплата", "рахунок"]
        assert saved["directions"] == payload["directions"]
        payload["system_prompt"] = result.json()["system_prompt"]
        for direction, expanded in zip(
            payload["directions"], result.json()["directions"], strict=True
        ):
            direction["keywords"] = expanded["keywords"]
        applied = await client.put("/api/v1/classifier", headers=headers(), json=payload)
        assert applied.status_code == 200
        assert applied.json()["directions"][1]["keywords"] == ["оплата", "рахунок"]
        generator.output = '{"system_prompt":"Missing category definitions here"}'
        assert (
            await client.post("/api/v1/classifier/expand", headers=headers(), json=payload)
        ).status_code == 502


@pytest.mark.asyncio
async def test_expansion_retries_without_old_prompt_when_model_keeps_deleted_type(database):
    await seed_owner(database)
    stale_code = "type_deadbeef"

    class Generator:
        calls: list[dict[str, object]] = []

        async def expand_classifier(self, text, *, model):
            request = json.loads(text)
            self.calls.append(request)
            suffix = f", {stale_code} — видалений тип" if len(self.calls) == 1 else ""
            return json.dumps(
                {
                    "system_prompt": f"Класифікуй лише general і money{suffix}.",
                    "directions": [
                        {"code": "general", "keywords": []},
                        {"code": "money", "keywords": ["оплата"]},
                    ],
                }
            )

    generator = Generator()
    app = FastAPI()
    app.include_router(
        build_web_router(
            database=database,
            settings=Settings(bot_token=TOKEN, webhook_secret="secret", master_user_id=42),
            language_model=generator,
        )
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        payload = (await client.get("/api/v1/bootstrap", headers=headers())).json()["classifier"]
        payload["system_prompt"] += f"\n- {stale_code} — старий тип"
        result = await client.post("/api/v1/classifier/expand", headers=headers(), json=payload)

    assert result.status_code == 200
    assert stale_code not in result.json()["system_prompt"]
    assert len(generator.calls) == 2
    assert generator.calls[0]["current_prompt"] == payload["system_prompt"]
    assert generator.calls[0]["regenerate_from_scratch"] is False
    assert generator.calls[1]["current_prompt"] == ""
    assert generator.calls[1]["regenerate_from_scratch"] is True
