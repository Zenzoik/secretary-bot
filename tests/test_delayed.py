from __future__ import annotations

import json
import random
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from secretary_bot.delayed import (
    MAX_DELAY_SECONDS,
    MIN_DELAY_SECONDS,
    DelayedReplyQueue,
    ReplyTask,
    reply_delay,
)

NOW = datetime(2026, 8, 24, 0, 14, tzinfo=UTC)

TASK = ReplyTask(
    connection_id=1,
    business_connection_id="connection-1",
    contact_id=100,
    chat_id=100,
    message_id=7,
    log_id=11,
    template_code="money_priority",
    category="money",
    incoming_at=NOW.isoformat(),
    confidence="0.91",
    window_key="2026-08-23:1",
    contact_name="Вася",
)


class FakeSortedSet:
    """The slice of Redis the queue uses, kept in memory."""

    def __init__(self) -> None:
        self.scores: dict[str, float] = {}

    async def zadd(self, name: str, mapping: dict[str, float]) -> int:
        added = sum(member not in self.scores for member in mapping)
        self.scores.update(mapping)
        return added

    async def zrangebyscore(self, name: str, min: float, max: float) -> list[str]:
        return sorted(
            (member for member, score in self.scores.items() if min <= score <= max),
            key=lambda member: self.scores[member],
        )

    async def zrem(self, name: str, *values: str) -> int:
        return sum(self.scores.pop(value, None) is not None for value in values)

    async def zcard(self, name: str) -> int:
        return len(self.scores)


def test_delay_stays_inside_the_specified_window() -> None:
    rng = random.Random(0)

    delays = [reply_delay(rng=rng).total_seconds() for _ in range(200)]

    assert min(delays) >= MIN_DELAY_SECONDS
    assert max(delays) <= MAX_DELAY_SECONDS
    assert len(set(delays)) > 1, "a constant delay would be recognisable as a bot"


def test_task_round_trips_without_the_message_body() -> None:
    payload = TASK.to_json()

    assert ReplyTask.from_json(payload) == TASK
    assert set(json.loads(payload)) == {
        "connection_id",
        "business_connection_id",
        "contact_id",
        "chat_id",
        "message_id",
        "log_id",
        "template_code",
        "category",
        "incoming_at",
        "confidence",
        "window_key",
        "contact_name",
    }
    assert TASK.incoming_moment == NOW


@pytest.mark.asyncio
async def test_a_task_stays_put_until_its_delay_elapses() -> None:
    queue = DelayedReplyQueue(client=FakeSortedSet())
    due_at = NOW + timedelta(seconds=120)

    await queue.schedule(TASK, due_at=due_at)

    assert await queue.pop_due(now=NOW) == []
    assert await queue.pending() == 1
    assert await queue.pop_due(now=due_at) == [TASK]
    assert await queue.pending() == 0


@pytest.mark.asyncio
async def test_due_tasks_come_back_in_the_order_they_were_due() -> None:
    queue = DelayedReplyQueue(client=FakeSortedSet())
    later = replace(TASK, message_id=8)

    await queue.schedule(later, due_at=NOW + timedelta(seconds=200))
    await queue.schedule(TASK, due_at=NOW + timedelta(seconds=100))

    assert await queue.pop_due(now=NOW + timedelta(seconds=300)) == [TASK, later]


@pytest.mark.asyncio
async def test_a_task_is_claimed_only_once() -> None:
    client = FakeSortedSet()
    first = DelayedReplyQueue(client=client)
    second = DelayedReplyQueue(client=client)
    await first.schedule(TASK, due_at=NOW)

    claimed = await first.pop_due(now=NOW)
    also_claimed = await second.pop_due(now=NOW)

    assert claimed == [TASK]
    assert also_claimed == []


@pytest.mark.asyncio
async def test_tasks_left_by_a_stopped_process_are_picked_up_on_restart() -> None:
    client = FakeSortedSet()
    await DelayedReplyQueue(client=client).schedule(TASK, due_at=NOW + timedelta(seconds=90))

    restarted = DelayedReplyQueue(client=client)

    assert await restarted.pop_due(now=NOW + timedelta(minutes=30)) == [TASK]
