from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import pytest
from aiogram.types import BusinessConnection
from fastapi.testclient import TestClient
from sqlalchemy import select

from secretary_bot import models
from secretary_bot.application import create_app
from secretary_bot.config import Settings
from secretary_bot.delayed import DelayedReplyQueue
from secretary_bot.ingest import DeduplicationUnavailable
from secretary_bot.storage import Database
from tests.test_delayed import FakeSortedSet
from tests.test_pipeline import FakeNotifier

SECRET = "test_webhook_secret"
CONNECTION_UPDATE = {
    "update_id": 1,
    "business_connection": {
        "id": "connection-1",
        "user": {"id": 42, "is_bot": False, "first_name": "Owner", "username": "owner"},
        "user_chat_id": 42,
        "date": 1_700_000_000,
        "rights": {"can_reply": True},
        "is_enabled": True,
    },
}
INCOMING_UPDATE = {
    "update_id": 2,
    "business_message": {
        "message_id": 10,
        "date": 1_700_000_001,
        "business_connection_id": "connection-1",
        "chat": {"id": 100, "type": "private", "first_name": "Contact"},
        "from": {"id": 100, "is_bot": False, "first_name": "Contact"},
        "text": "sensitive test body",
    },
}
FEEDBACK_UPDATE = {
    "update_id": 3,
    "callback_query": {
        "id": "callback-1",
        "from": {"id": 42, "is_bot": False, "first_name": "Owner"},
        "chat_instance": "instance",
        "data": "feedback:1:ok",
        "message": {
            "message_id": 20,
            "date": 1_700_000_002,
            "chat": {"id": 42, "type": "private", "first_name": "Owner"},
            "text": "Dry-run preview",
        },
    },
}
CONTROL_UPDATE = {
    "update_id": 4,
    "message": {
        "message_id": 12,
        "date": 1_700_000_002,
        "chat": {"id": 42, "type": "private", "first_name": "Owner"},
        "from": {"id": 42, "is_bot": False, "first_name": "Owner"},
        "text": "/status",
    },
}
CONTACT_CALLBACK_UPDATE = {
    "update_id": 5,
    "callback_query": {
        "id": "contact-callback-1",
        "from": {"id": 42, "is_bot": False, "first_name": "Owner"},
        "chat_instance": "instance",
        "data": "contact:100:exclude",
    },
}


@dataclass
class SentMessage:
    message_id: int = 11


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.answered: list[str] = []
        self.edited: list[dict[str, Any]] = []
        self.connections: dict[str, BusinessConnection] = {}

    async def get_business_connection(self, connection_id: str) -> BusinessConnection:
        return self.connections[connection_id]

    async def send_message(self, **kwargs: Any) -> SentMessage:
        self.sent.append(kwargs)
        return SentMessage()

    async def answer_callback_query(self, callback_query_id: str, **kwargs: Any) -> bool:
        self.answered.append(callback_query_id)
        return True

    async def edit_message_text(self, **kwargs: Any) -> bool:
        self.edited.append(kwargs)
        return True

    async def edit_message_reply_markup(self, **kwargs: Any) -> bool:
        self.edited.append(kwargs)
        return True


class FakeDeduplicator:
    def __init__(self) -> None:
        self.keys: set[str] = set()

    async def claim(self, key: str) -> bool:
        if key in self.keys:
            return False
        self.keys.add(key)
        return True

    async def release(self, key: str) -> None:
        self.keys.discard(key)

    async def aclose(self) -> None:
        return None


class FailingDeduplicator(FakeDeduplicator):
    async def claim(self, key: str) -> bool:
        raise DeduplicationUnavailable("simulated Redis failure")


@pytest.fixture
def world(database: Database):
    bot = FakeBot()
    notifier = FakeNotifier()
    queue = DelayedReplyQueue(client=FakeSortedSet())
    settings = Settings(
        bot_token="123456:TEST_TOKEN",
        webhook_secret=SECRET,
        master_user_id=42,
        bot_username="secretary_test_bot",
        database_url="sqlite+aiosqlite:///:memory:",
    )
    app = create_app(
        settings=settings,
        bot=bot,
        deduplicator=FakeDeduplicator(),
        database=database,
        delayed_queue=queue,
        notifier=notifier,
    )
    return TestClient(app), bot, queue, database


def post_update(client: TestClient, update: dict[str, Any]) -> Any:
    return client.post(
        "/telegram/webhook",
        json=update,
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
    )


def wait_for(predicate: Any, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("background worker did not process the update")


def test_health_does_not_expose_secrets(world) -> None:
    client, *_ = world

    with client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "classifier": "keywords",
        "allowed_chat_count": 0,
        "queue_depth": 0,
        "processed_updates": 0,
        "accepted_updates": 0,
        "duplicate_updates": 0,
    }
    assert SECRET not in response.text


def test_webhook_rejects_missing_or_wrong_secret(world) -> None:
    client, *_ = world

    with client:
        assert client.post("/telegram/webhook", json=CONNECTION_UPDATE).status_code == 403
        assert (
            client.post(
                "/telegram/webhook",
                json=CONNECTION_UPDATE,
                headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
            ).status_code
            == 403
        )


def test_webhook_rejects_invalid_update(world) -> None:
    client, *_ = world

    with client:
        assert post_update(client, {"unexpected": "payload"}).status_code == 400


def test_connection_update_is_stored(world) -> None:
    client, _, _, database = world

    with client:
        assert post_update(client, CONNECTION_UPDATE).status_code == 200
        wait_for(lambda: client.app.state.runtime.processed_updates == 1)

    row = asyncio.run(_first(database, models.Connection))
    assert row is not None
    assert row.owner_chat_id == 42
    assert row.rights_json == {"can_reply": True}
    assert row.dry_run is True, "a new connection must not answer anyone yet"


def test_unknown_owner_connection_is_denied_fail_closed(world) -> None:
    client, _, _, database = world
    unauthorized = {
        **CONNECTION_UPDATE,
        "update_id": 99,
        "business_connection": {
            **CONNECTION_UPDATE["business_connection"],
            "id": "unauthorized-connection",
            "user": {"id": 99, "is_bot": False, "first_name": "Unknown"},
            "user_chat_id": 99,
        },
    }

    with client:
        assert post_update(client, unauthorized).status_code == 200
        wait_for(lambda: client.app.state.runtime.processed_updates == 1)

    assert asyncio.run(_first(database, models.Connection)) is None


def test_incoming_message_is_gated_and_never_echoed(world) -> None:
    client, bot, queue, database = world

    with client:
        assert post_update(client, CONNECTION_UPDATE).status_code == 200
        assert post_update(client, INCOMING_UPDATE).status_code == 200
        wait_for(lambda: client.app.state.runtime.processed_updates == 2)

    # No schedule exists yet, so the gate refuses and nothing is sent.
    assert bot.sent == []
    row = asyncio.run(_first(database, models.MessageLog))
    assert row is not None and row.action == "skipped_schedule"


def test_message_body_is_not_logged(world, caplog: Any) -> None:
    client, *_ = world

    with client, caplog.at_level("INFO"):
        assert post_update(client, CONNECTION_UPDATE).status_code == 200
        assert post_update(client, INCOMING_UPDATE).status_code == 200
        wait_for(lambda: client.app.state.runtime.processed_updates == 2)

    assert "sensitive test body" not in caplog.text


def test_feedback_button_is_recorded(world) -> None:
    client, bot, _, database = world

    with client:
        assert post_update(client, CONNECTION_UPDATE).status_code == 200
        assert post_update(client, INCOMING_UPDATE).status_code == 200
        wait_for(lambda: client.app.state.runtime.processed_updates == 2)
        assert post_update(client, FEEDBACK_UPDATE).status_code == 200
        wait_for(lambda: client.app.state.runtime.processed_updates == 3)

    row = asyncio.run(_first(database, models.ShadowFeedback))
    assert row is not None and row.verdict == "ok"
    assert bot.answered == ["callback-1"]
    assert bot.edited[-1]["reply_markup"] is None
    assert "Обработано" in bot.edited[-1]["text"]


def test_owner_control_command_is_handled(world) -> None:
    client, bot, _, _ = world

    with client:
        assert post_update(client, CONNECTION_UPDATE).status_code == 200
        assert post_update(client, CONTROL_UPDATE).status_code == 200
        wait_for(lambda: client.app.state.runtime.processed_updates == 2)

    assert len(bot.sent) == 1
    assert "Секретарь: включён" in bot.sent[0]["text"]


def test_contact_card_callback_is_routed_before_feedback(world) -> None:
    client, bot, _, database = world

    with client:
        assert post_update(client, CONNECTION_UPDATE).status_code == 200
        assert post_update(client, CONTACT_CALLBACK_UPDATE).status_code == 200
        wait_for(lambda: client.app.state.runtime.processed_updates == 2)

    exclusion = asyncio.run(_first(database, models.Exclusion))
    assert exclusion is not None and exclusion.contact_id == 100
    assert bot.answered == ["contact-callback-1"]


def test_duplicate_business_message_is_processed_once(world) -> None:
    client, _, _, database = world
    same_message_with_new_update_id = {**INCOMING_UPDATE, "update_id": 99}

    with client:
        assert post_update(client, CONNECTION_UPDATE).status_code == 200
        assert post_update(client, INCOMING_UPDATE).status_code == 200
        assert post_update(client, same_message_with_new_update_id).status_code == 200
        wait_for(lambda: client.app.state.runtime.processed_updates == 2)
        health = client.get("/healthz").json()

    assert health["accepted_updates"] == 2
    assert health["duplicate_updates"] == 1


def test_redis_failure_returns_retryable_status(database: Database) -> None:
    settings = Settings(
        bot_token="123456:TEST_TOKEN",
        webhook_secret=SECRET,
        master_user_id=42,
        bot_username="secretary_test_bot",
        database_url="sqlite+aiosqlite:///:memory:",
    )
    client = TestClient(
        create_app(
            settings=settings,
            bot=FakeBot(),
            deduplicator=FailingDeduplicator(),
            database=database,
            delayed_queue=DelayedReplyQueue(client=FakeSortedSet()),
            notifier=FakeNotifier(),
        )
    )

    with client:
        response = post_update(client, CONNECTION_UPDATE)

    assert response.status_code == 503
    assert response.json() == {"detail": "ingest unavailable"}
    assert client.app.state.runtime.processed_updates == 0


async def _first(database: Database, model: Any) -> Any:
    async with database.session() as session:
        return await session.scalar(select(model))
