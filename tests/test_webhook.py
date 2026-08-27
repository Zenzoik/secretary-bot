from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from aiogram.types import BusinessConnection
from fastapi.testclient import TestClient

from secretary_bot.application import create_app
from secretary_bot.config import Settings

SECRET = "test_webhook_secret"
CONNECTION_UPDATE = {
    "update_id": 1,
    "business_connection": {
        "id": "connection-1",
        "user": {"id": 42, "is_bot": False, "first_name": "Owner"},
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


@dataclass
class SentMessage:
    message_id: int = 11


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.read: list[dict[str, Any]] = []
        self.fail_read = False
        self.connections: dict[str, BusinessConnection] = {}

    async def get_business_connection(self, connection_id: str) -> BusinessConnection:
        return self.connections[connection_id]

    async def send_message(self, **kwargs: Any) -> SentMessage:
        self.sent.append(kwargs)
        return SentMessage()

    async def read_business_message(
        self, business_connection_id: str, chat_id: int, message_id: int
    ) -> bool:
        self.read.append(
            {
                "business_connection_id": business_connection_id,
                "chat_id": chat_id,
                "message_id": message_id,
            }
        )
        if self.fail_read:
            raise RuntimeError("simulated read failure")
        return True


def make_client(*, echo_enabled: bool = False) -> tuple[TestClient, FakeBot]:
    bot = FakeBot()
    settings = Settings(
        bot_token="123456:TEST_TOKEN",
        webhook_secret=SECRET,
        echo_enabled=echo_enabled,
        allowed_chat_ids=frozenset({100}),
    )
    return TestClient(create_app(settings=settings, bot=bot)), bot


def post_update(client: TestClient, update: dict[str, Any]) -> Any:
    return client.post(
        "/telegram/webhook",
        json=update,
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
    )


def wait_for(predicate: Any, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("background worker did not process the update")


def test_health_does_not_expose_secrets() -> None:
    client, _ = make_client()

    with client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "echo_enabled": False,
        "allowed_chat_count": 1,
        "queue_depth": 0,
        "processed_updates": 0,
    }
    assert SECRET not in response.text


def test_webhook_rejects_missing_or_wrong_secret() -> None:
    client, _ = make_client()

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


def test_webhook_rejects_invalid_update() -> None:
    client, _ = make_client()

    with client:
        response = post_update(client, {"unexpected": "payload"})

    assert response.status_code == 400


def test_echo_is_sent_with_business_connection_id() -> None:
    client, bot = make_client(echo_enabled=True)

    with client:
        assert post_update(client, CONNECTION_UPDATE).status_code == 200
        assert post_update(client, INCOMING_UPDATE).status_code == 200
        wait_for(lambda: len(bot.sent) == 1)

    assert bot.sent == [
        {
            "business_connection_id": "connection-1",
            "chat_id": 100,
            "text": "sensitive test body",
        }
    ]
    assert bot.read == []


def test_incoming_message_is_marked_read_after_echo_when_allowed() -> None:
    client, bot = make_client(echo_enabled=True)
    connection_update = {
        **CONNECTION_UPDATE,
        "business_connection": {
            **CONNECTION_UPDATE["business_connection"],
            "rights": {"can_reply": True, "can_read_messages": True},
        },
    }

    with client:
        assert post_update(client, connection_update).status_code == 200
        assert post_update(client, INCOMING_UPDATE).status_code == 200
        wait_for(lambda: len(bot.read) == 1)

    assert bot.read == [
        {
            "business_connection_id": "connection-1",
            "chat_id": 100,
            "message_id": 10,
        }
    ]


def test_read_failure_does_not_undo_successful_echo(caplog: Any) -> None:
    client, bot = make_client(echo_enabled=True)
    bot.fail_read = True
    connection_update = {
        **CONNECTION_UPDATE,
        "business_connection": {
            **CONNECTION_UPDATE["business_connection"],
            "rights": {"can_reply": True, "can_read_messages": True},
        },
    }

    with client, caplog.at_level("WARNING"):
        assert post_update(client, connection_update).status_code == 200
        assert post_update(client, INCOMING_UPDATE).status_code == 200
        wait_for(lambda: client.app.state.runtime.processed_updates == 2)

    assert len(bot.sent) == 1
    assert '"event": "message_read_failed"' in caplog.text
    assert "sensitive test body" not in caplog.text


def test_echo_is_not_sent_when_disabled() -> None:
    client, bot = make_client(echo_enabled=False)

    with client:
        assert post_update(client, CONNECTION_UPDATE).status_code == 200
        assert post_update(client, INCOMING_UPDATE).status_code == 200
        wait_for(lambda: client.app.state.runtime.processed_updates == 2)

    assert bot.sent == []


def test_echo_is_not_sent_to_chat_outside_server_allowlist() -> None:
    client, bot = make_client(echo_enabled=True)
    other_chat_update = {
        **INCOMING_UPDATE,
        "business_message": {
            **INCOMING_UPDATE["business_message"],
            "chat": {"id": 101, "type": "private", "first_name": "Other"},
            "from": {"id": 101, "is_bot": False, "first_name": "Other"},
        },
    }

    with client:
        assert post_update(client, CONNECTION_UPDATE).status_code == 200
        assert post_update(client, other_chat_update).status_code == 200
        wait_for(lambda: client.app.state.runtime.processed_updates == 2)

    assert bot.sent == []
    assert bot.read == []


def test_message_body_is_not_logged(caplog: Any) -> None:
    client, _ = make_client(echo_enabled=False)

    with client, caplog.at_level("INFO"):
        assert post_update(client, CONNECTION_UPDATE).status_code == 200
        assert post_update(client, INCOMING_UPDATE).status_code == 200
        wait_for(lambda: client.app.state.runtime.processed_updates == 2)

    assert "sensitive test body" not in caplog.text
