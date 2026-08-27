from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiogram.types import Update
from redis.exceptions import ConnectionError

from secretary_bot.ingest import (
    DeduplicationUnavailable,
    IngestQueueFull,
    IngestResult,
    RedisDeduplicator,
    UpdateIngestor,
    deduplication_key,
)


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.set_calls: list[dict[str, Any]] = []
        self.fail = False

    async def set(self, name: str, value: str, *, ex: int, nx: bool) -> bool | None:
        if self.fail:
            raise ConnectionError("simulated connection failure")
        self.set_calls.append({"name": name, "value": value, "ex": ex, "nx": nx})
        if nx and name in self.values:
            return None
        self.values[name] = value
        return True

    async def delete(self, *names: str) -> int:
        if self.fail:
            raise ConnectionError("simulated connection failure")
        deleted = 0
        for name in names:
            deleted += self.values.pop(name, None) is not None
        return deleted

    async def aclose(self) -> None:
        return None


def make_update(*, update_id: int = 2, message_id: int = 10) -> Update:
    return Update.model_validate(
        {
            "update_id": update_id,
            "business_message": {
                "message_id": message_id,
                "date": 1_700_000_001,
                "business_connection_id": "connection-1",
                "chat": {"id": 100, "type": "private", "first_name": "Contact"},
                "from": {"id": 100, "is_bot": False, "first_name": "Contact"},
                "text": "must never enter the dedup key",
            },
        }
    )


def test_business_message_key_uses_stable_ids_without_body() -> None:
    key = deduplication_key(make_update())

    assert key == "business-message:connection-1:100:10"
    assert "must never" not in key


@pytest.mark.asyncio
async def test_redis_claim_is_atomic_and_has_24_hour_ttl() -> None:
    redis = FakeRedis()
    deduplicator = RedisDeduplicator(client=redis, ttl_seconds=86400)

    assert await deduplicator.claim("update:1") is True
    assert await deduplicator.claim("update:1") is False
    assert redis.set_calls[0] == {
        "name": "secretary:dedup:update:1",
        "value": "1",
        "ex": 86400,
        "nx": True,
    }


@pytest.mark.asyncio
async def test_redis_error_fails_closed() -> None:
    redis = FakeRedis()
    redis.fail = True
    deduplicator = RedisDeduplicator(client=redis, ttl_seconds=86400)

    with pytest.raises(DeduplicationUnavailable):
        await deduplicator.claim("update:1")


@pytest.mark.asyncio
async def test_queue_overflow_releases_claim_for_telegram_retry() -> None:
    redis = FakeRedis()
    deduplicator = RedisDeduplicator(client=redis, ttl_seconds=86400)
    queue: asyncio.Queue[Update] = asyncio.Queue(maxsize=1)
    queue.put_nowait(make_update(update_id=1, message_id=1))
    ingestor = UpdateIngestor(queue=queue, deduplicator=deduplicator)
    update = make_update()

    with pytest.raises(IngestQueueFull):
        await ingestor.enqueue(update)

    assert "secretary:dedup:business-message:connection-1:100:10" not in redis.values
    queue.get_nowait()
    assert await ingestor.enqueue(update) is IngestResult.ACCEPTED
