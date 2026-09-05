"""Opt-in tests against isolated PostgreSQL/Redis; never use production URLs."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from sqlalchemy import text

from secretary_bot import models
from secretary_bot.delayed import DelayedReplyQueue
from secretary_bot.delivery import send_once
from secretary_bot.sender import BusinessReplySender
from secretary_bot.storage import Database
from tests.test_delayed import NOW, TASK
from tests.test_pipeline import FakeBot

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_AUDIT_DATABASE_URL") or not os.environ.get("TEST_AUDIT_REDIS_URL"),
    reason="explicit isolated infrastructure URLs required",
)


@pytest_asyncio.fixture
async def infrastructure():
    url = os.environ["TEST_AUDIT_DATABASE_URL"]
    schema = "audit_" + uuid.uuid4().hex
    admin = Database.from_url(url)
    async with admin.engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    db = Database.from_url(url, connect_args={"server_settings": {"search_path": schema}})
    async with db.engine.begin() as connection:
        await connection.run_sync(models.Base.metadata.create_all)
    async with db.session() as session, session.begin():
        session.add(models.Connection(id=1, business_connection_id="test", owner_user_id=42))
    redis = Redis.from_url(os.environ["TEST_AUDIT_REDIS_URL"], decode_responses=True)
    queue = DelayedReplyQueue(redis, key=schema)
    try:
        yield db, queue
    finally:
        await redis.delete(schema)
        await redis.aclose()
        await db.aclose()
        async with admin.engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.aclose()


@pytest.mark.asyncio
async def test_two_real_redis_consumers_cannot_claim_the_same_lease(infrastructure):
    _, queue = infrastructure
    await queue.schedule(TASK, due_at=NOW)
    first, second = await asyncio.gather(queue.pop_due(now=NOW), queue.pop_due(now=NOW))
    assert sorted(map(len, [first, second])) == [0, 1]
    assert await queue.pending() == 1
    # A worker disappeared without ack; a new worker recovers after lease expiration.
    assert await queue.pop_due(now=NOW + timedelta(seconds=121)) == [TASK]
    await queue.acknowledge(TASK)
    assert await queue.pending() == 0


@pytest.mark.asyncio
async def test_postgres_receipt_serializes_concurrent_business_sends(infrastructure):
    db, _ = infrastructure
    bot = FakeBot()
    sender = BusinessReplySender(bot)

    async def attempt():
        return await send_once(
            db,
            sender,
            key="same-send",
            connection_id=1,
            business_connection_id="test",
            chat_id=100,
            text="test",
        )

    results = await asyncio.gather(attempt(), attempt())
    assert any(result.is_sent for result in results)
    assert len(bot.sent) == 1
    assert (await attempt()).is_sent
    assert len(bot.sent) == 1


@pytest.mark.asyncio
async def test_task_survives_sigkill_after_redis_claim(infrastructure):
    import sys

    _, queue = infrastructure
    await queue.schedule(TASK, due_at=NOW)
    code = """
import asyncio, os, signal
from redis.asyncio import Redis
from secretary_bot.delayed import DelayedReplyQueue
from tests.test_delayed import NOW
async def main():
    client = Redis.from_url(os.environ['TEST_AUDIT_REDIS_URL'], decode_responses=True)
    queue = DelayedReplyQueue(client, key=os.environ['TEST_AUDIT_QUEUE_KEY'])
    assert len(await queue.pop_due(now=NOW)) == 1
    os.kill(os.getpid(), signal.SIGKILL)
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        env={**os.environ, "TEST_AUDIT_QUEUE_KEY": queue.key},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, error = await asyncio.wait_for(process.communicate(), timeout=15)
    assert process.returncode == -9, error.decode()
    assert await queue.pending() == 1
    assert await queue.pop_due(now=NOW + timedelta(seconds=121)) == [TASK]
    await queue.acknowledge(TASK)
