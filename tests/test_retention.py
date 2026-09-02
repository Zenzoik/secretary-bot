from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from secretary_bot import models
from secretary_bot.actions import LogAction
from secretary_bot.retention import MESSAGE_RETENTION, MessageCipher, MessageContext
from secretary_bot.storage import (
    capture_message,
    delete_expired_messages,
    load_retained_dialogues,
    log_decision,
    purge_retained_messages,
)

NOW = datetime(2026, 9, 2, 12, tzinfo=UTC)


def context(*, message_id: int = 10) -> MessageContext:
    return MessageContext(
        connection_id=1,
        contact_id=200,
        tg_message_id=message_id,
        direction="in",
    )


def test_cipher_round_trip_never_contains_plaintext() -> None:
    cipher = MessageCipher.from_encoded_key(MessageCipher.generate_encoded_key())

    encrypted = cipher.encrypt("Оплата завтра", context=context())

    assert "Оплата завтра".encode() not in encrypted
    assert cipher.decrypt(encrypted, context=context()) == "Оплата завтра"


def test_cipher_authenticates_row_identity_and_payload() -> None:
    cipher = MessageCipher.from_encoded_key(MessageCipher.generate_encoded_key())
    encrypted = cipher.encrypt("confidential", context=context())

    with pytest.raises(ValueError, match="authentication"):
        cipher.decrypt(encrypted, context=context(message_id=11))

    tampered = encrypted[:-1] + bytes([encrypted[-1] ^ 1])
    with pytest.raises(ValueError, match="authentication"):
        cipher.decrypt(tampered, context=context())


@pytest.mark.parametrize("encoded", ["", "dG9vLXNob3J0", "not base64!"])
def test_cipher_rejects_invalid_keys(encoded: str) -> None:
    with pytest.raises(ValueError):
        MessageCipher.from_encoded_key(encoded)


async def add_connection(session) -> int:
    connection = models.Connection(
        business_connection_id="retention-test",
        owner_user_id=42,
        message_retention_enabled=True,
    )
    session.add(connection)
    await session.flush()
    return connection.id


@pytest.mark.asyncio
async def test_cleanup_deletes_only_expired_captured_rows(session) -> None:
    connection_id = await add_connection(session)
    cipher = MessageCipher.from_encoded_key(MessageCipher.generate_encoded_key())
    encrypted = cipher.encrypt("text", context=context())
    await capture_message(
        session,
        connection_id=connection_id,
        contact_id=200,
        tg_message_id=10,
        direction="in",
        occurred_at=NOW - MESSAGE_RETENTION,
        body_encrypted=encrypted,
        retention_until=NOW,
    )
    await capture_message(
        session,
        connection_id=connection_id,
        contact_id=200,
        tg_message_id=11,
        direction="in",
        occurred_at=NOW,
        body_encrypted=encrypted,
        retention_until=NOW + MESSAGE_RETENTION,
    )
    await log_decision(
        session,
        connection_id=connection_id,
        contact_id=200,
        action=LogAction.REPLIED,
        occurred_at=NOW - timedelta(days=10),
    )

    assert await delete_expired_messages(session, now=NOW) == 1
    rows = list(await session.scalars(select(models.MessageLog).order_by(models.MessageLog.id)))

    assert [row.action for row in rows] == [LogAction.CAPTURED.value, LogAction.REPLIED.value]
    assert rows[0].retention_until == NOW + MESSAGE_RETENTION
    assert rows[1].body_encrypted is None


@pytest.mark.asyncio
async def test_cleanup_is_bounded_and_purge_is_connection_scoped(session) -> None:
    connection_id = await add_connection(session)
    other = models.Connection(business_connection_id="other", owner_user_id=43)
    session.add(other)
    await session.flush()
    for message_id in range(3):
        session.add(
            models.MessageLog(
                connection_id=connection_id,
                contact_id=200,
                tg_message_id=message_id,
                direction="in",
                action=LogAction.CAPTURED.value,
                body_encrypted=b"encrypted",
                retention_until=NOW - timedelta(seconds=1),
            )
        )
    session.add(
        models.MessageLog(
            connection_id=other.id,
            contact_id=201,
            direction="in",
            action=LogAction.CAPTURED.value,
            body_encrypted=b"encrypted",
            retention_until=NOW + MESSAGE_RETENTION,
        )
    )
    await session.flush()

    assert await delete_expired_messages(session, now=NOW, batch_size=2) == 2
    assert await purge_retained_messages(session, connection_id=connection_id) == 1
    remaining = await session.scalar(select(func.count()).select_from(models.MessageLog))

    assert remaining == 1


@pytest.mark.asyncio
async def test_retained_dialogues_only_include_active_rows_inside_period(session) -> None:
    connection_id = await add_connection(session)
    cipher = MessageCipher.from_encoded_key(MessageCipher.generate_encoded_key())
    session.add(
        models.ContactActivity(
            connection_id=connection_id,
            contact_id=200,
            contact_name="Контакт",
        )
    )
    for message_id, direction, text, occurred_at, retention_until in (
        (10, "in", "Питання", NOW - timedelta(hours=2), NOW + timedelta(hours=46)),
        (11, "out", "Відповідь", NOW - timedelta(hours=1), NOW + timedelta(hours=47)),
        (12, "in", "Застаріле", NOW - timedelta(days=2), NOW - timedelta(seconds=1)),
    ):
        encrypted = cipher.encrypt(
            text,
            context=MessageContext(connection_id, 200, message_id, direction),
        )
        await capture_message(
            session,
            connection_id=connection_id,
            contact_id=200,
            tg_message_id=message_id,
            direction=direction,
            occurred_at=occurred_at,
            body_encrypted=encrypted,
            retention_until=retention_until,
        )

    dialogues = await load_retained_dialogues(
        session,
        connection_id=connection_id,
        period_start=NOW - timedelta(days=1),
        period_end=NOW,
        now=NOW,
        cipher=cipher,
    )

    assert len(dialogues) == 1
    assert dialogues[0].contact_name == "Контакт"
    assert [message.text for message in dialogues[0].messages] == ["Питання", "Відповідь"]
    assert dialogues[0].last_incoming_message_id == 10
