from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from secretary_bot import models
from secretary_bot.morning import MorningDigest, is_delivery_time
from secretary_bot.storage import ConnectionSnapshot, Database, enqueue_morning, upsert_connection
from tests.test_pipeline import FakeNotifier

# 08:05 in Kyiv (UTC+3 in August).
MORNING = datetime(2026, 8, 24, 5, 5, tzinfo=UTC)
NIGHT = datetime(2026, 8, 24, 0, 14, tzinfo=UTC)


async def seed(database: Database, *, owner_chat_id: int | None = 42, items: int = 1) -> int:
    async with database.session() as session, session.begin():
        record = await upsert_connection(
            session,
            ConnectionSnapshot(
                business_connection_id="connection-1",
                owner_user_id=42,
                owner_chat_id=owner_chat_id,
            ),
        )
        for index in range(items):
            await enqueue_morning(
                session,
                connection_id=record.id,
                contact_id=100 + index,
                contact_name=f"Контакт {index}",
                occurred_at=NIGHT,
            )
        return record.id


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 8, 24, 8, 0), True),
        (datetime(2026, 8, 24, 8, 29), True),
        (datetime(2026, 8, 24, 7, 59), False),
        (datetime(2026, 8, 24, 8, 30), False),
        (datetime(2026, 8, 24, 20, 0), False),
    ],
)
def test_delivery_window_opens_at_eight(moment: datetime, expected: bool) -> None:
    assert is_delivery_time(moment) is expected


@pytest.mark.asyncio
async def test_the_list_arrives_in_the_morning(database: Database) -> None:
    await seed(database, items=2)
    notifier = FakeNotifier()

    delivered = await MorningDigest(database=database, notifier=notifier).run_once(now=MORNING)

    assert delivered == 2
    chat_id, text = notifier.alerts[0]
    assert chat_id == 42
    assert "Контакт 0" in text
    assert "03:14" in text, "times are shown in the owner's timezone"


@pytest.mark.asyncio
async def test_nothing_is_sent_at_night(database: Database) -> None:
    await seed(database)
    notifier = FakeNotifier()

    delivered = await MorningDigest(database=database, notifier=notifier).run_once(now=NIGHT)

    assert delivered == 0
    assert notifier.alerts == []


@pytest.mark.asyncio
async def test_the_same_list_is_not_delivered_twice(database: Database) -> None:
    await seed(database)
    digest = MorningDigest(database=database, notifier=FakeNotifier())

    first = await digest.run_once(now=MORNING)
    second = await digest.run_once(now=MORNING)

    assert (first, second) == (1, 0)
    async with database.session() as session:
        row = await session.scalar(select(models.MorningQueue))
    assert row is not None and row.is_delivered is True


@pytest.mark.asyncio
async def test_an_empty_queue_produces_no_message(database: Database) -> None:
    await seed(database, items=0)
    notifier = FakeNotifier()

    await MorningDigest(database=database, notifier=notifier).run_once(now=MORNING)

    assert notifier.alerts == []


@pytest.mark.asyncio
async def test_a_connection_without_a_known_owner_chat_is_skipped(database: Database) -> None:
    await seed(database, owner_chat_id=None)
    notifier = FakeNotifier()

    assert await MorningDigest(database=database, notifier=notifier).run_once(now=MORNING) == 0
