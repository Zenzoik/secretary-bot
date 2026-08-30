from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from secretary_bot.delayed import DelayedReplyQueue, ReplyTask
from secretary_bot.workers import DELIVERY_RETRY_SECONDS, deliver_due_once
from tests.test_delayed import NOW, TASK, FakeSortedSet


class FakePipeline:
    def __init__(self, *failures: BaseException | None) -> None:
        self.failures = list(failures)
        self.calls: list[ReplyTask] = []

    async def deliver(self, task: ReplyTask, *, now=None) -> None:
        self.calls.append(task)
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure


@pytest.mark.asyncio
async def test_failed_delivery_is_retried_and_eventually_succeeds() -> None:
    queue = DelayedReplyQueue(client=FakeSortedSet())
    pipeline = FakePipeline(RuntimeError("temporary"), None)
    await queue.schedule(TASK, due_at=NOW)

    await deliver_due_once(pipeline, queue, now=NOW)

    assert await queue.pop_due(now=NOW + timedelta(seconds=DELIVERY_RETRY_SECONDS - 1)) == []
    assert await queue.pending() == 1
    await deliver_due_once(
        pipeline,
        queue,
        now=NOW + timedelta(seconds=DELIVERY_RETRY_SECONDS),
    )
    assert await queue.pending() == 0
    assert [task.delivery_attempts for task in pipeline.calls] == [0, 1]


@pytest.mark.asyncio
async def test_delivery_is_dropped_after_three_failed_attempts() -> None:
    queue = DelayedReplyQueue(client=FakeSortedSet())
    pipeline = FakePipeline(*(RuntimeError("offline") for _ in range(3)))
    await queue.schedule(TASK, due_at=NOW)

    for attempt in range(3):
        await deliver_due_once(
            pipeline,
            queue,
            now=NOW + timedelta(seconds=DELIVERY_RETRY_SECONDS * attempt),
        )

    assert await queue.pending() == 0
    assert [task.delivery_attempts for task in pipeline.calls] == [0, 1, 2]


@pytest.mark.asyncio
async def test_shutdown_returns_the_entire_claimed_batch_to_redis() -> None:
    queue = DelayedReplyQueue(client=FakeSortedSet())
    other = replace(TASK, message_id=8)
    pipeline = FakePipeline(asyncio.CancelledError())
    await queue.schedule(TASK, due_at=NOW)
    await queue.schedule(other, due_at=NOW)

    with pytest.raises(asyncio.CancelledError):
        await deliver_due_once(pipeline, queue, now=NOW)

    assert await queue.pending() == 2
