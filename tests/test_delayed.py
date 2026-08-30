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
    template_code="money_priority",
    category="money",
    incoming_at=NOW.isoformat(),
    sender_identity="owner",
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
        "template_code",
        "category",
        "incoming_at",
        "sender_identity",
        "confidence",
        "window_key",
        "contact_name",
        "delivery_attempts",
    }
    assert TASK.incoming_moment == NOW


def test_legacy_task_keeps_the_preference_used_before_stage_1_7() -> None:
    values = json.loads(TASK.to_json())
    values.pop("sender_identity")
    values.pop("delivery_attempts")

    restored = ReplyTask.from_json(json.dumps(values))
    assert restored.sender_identity == "owner"
    assert restored.delivery_attempts == 0


def test_delay_uses_connection_specific_bounds() -> None:
    delay = reply_delay(min_seconds=17, max_seconds=17, rng=random.Random(0))

    assert delay == timedelta(seconds=17)


@pytest.mark.parametrize("bounds", [(-1, 5), (10, 5)])
def test_invalid_delay_bounds_are_rejected(bounds: tuple[int, int]) -> None:
    with pytest.raises(ValueError, match="0 <= min <= max"):
        reply_delay(min_seconds=bounds[0], max_seconds=bounds[1])


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


@pytest.mark.asyncio
async def test_revoking_one_connection_only_cancels_its_tasks() -> None:
    queue = DelayedReplyQueue(client=FakeSortedSet())
    same_owner_later = replace(TASK, message_id=8)
    other_owner = replace(
        TASK,
        connection_id=2,
        business_connection_id="connection-2",
        message_id=9,
    )
    for task in (TASK, same_owner_later, other_owner):
        await queue.schedule(task, due_at=NOW + timedelta(minutes=5))

    assert await queue.cancel_connection(1) == 2
    assert await queue.pending() == 1
    assert await queue.pop_due(now=NOW + timedelta(minutes=10)) == [other_owner]
