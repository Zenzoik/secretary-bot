from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from aiogram.types import Update
from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


class DeduplicationUnavailable(RuntimeError):
    """Raised when an update cannot be safely claimed or released."""


class IngestQueueFull(RuntimeError):
    """Raised when the bounded in-memory worker queue has no capacity."""


class Deduplicator(Protocol):
    async def claim(self, key: str) -> bool: ...

    async def release(self, key: str) -> None: ...

    async def aclose(self) -> None: ...


class RedisClient(Protocol):
    async def set(self, name: str, value: str, *, ex: int, nx: bool) -> Any: ...

    async def get(self, name: str) -> Any: ...

    async def delete(self, *names: str) -> int: ...

    async def aclose(self) -> None: ...


@dataclass(slots=True)
class RedisDeduplicator:
    client: RedisClient
    ttl_seconds: int
    namespace: str = "secretary:dedup"

    @classmethod
    def from_url(cls, url: str, *, ttl_seconds: int) -> RedisDeduplicator:
        client = Redis.from_url(url, decode_responses=True)
        return cls(client=client, ttl_seconds=ttl_seconds)

    async def is_completed(self, key: str) -> bool:
        return await self.client.get(self._qualified(key)) == "done"

    async def complete(self, key: str) -> None:
        await self.client.set(self._qualified(key), "done", ex=self.ttl_seconds, nx=False)

    async def claim(self, key: str) -> bool:
        try:
            result = await self.client.set(self._qualified(key), "processing", ex=45, nx=True)
        except RedisError as exc:
            raise DeduplicationUnavailable("could not claim update") from exc
        return bool(result)

    async def release(self, key: str) -> None:
        try:
            await self.client.delete(self._qualified(key))
        except RedisError as exc:
            raise DeduplicationUnavailable("could not release update") from exc

    async def aclose(self) -> None:
        await self.client.aclose()

    def _qualified(self, key: str) -> str:
        return f"{self.namespace}:{key}"


class IngestResult(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"


@dataclass(slots=True)
class UpdateIngestor:
    queue: asyncio.Queue[Update]
    deduplicator: Deduplicator
    processor: Callable[[Update], Awaitable[None]] | None = None
    concurrency: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(8))
    accepted_updates: int = 0
    duplicate_updates: int = 0

    async def enqueue(self, update: Update) -> IngestResult:
        key = deduplication_key(update)
        if not await self.deduplicator.claim(key):
            completed = getattr(self.deduplicator, "is_completed", None)
            if self.processor is not None and completed is not None and not await completed(key):
                raise IngestQueueFull("update is still processing; retry")
            self.duplicate_updates += 1
            return IngestResult.DUPLICATE

        if self.processor is not None:
            try:
                async with asyncio.timeout(30):
                    async with self.concurrency:
                        await self.processor(update)
                    complete = getattr(self.deduplicator, "complete", None)
                    if complete is not None:
                        await complete(key)
            except BaseException:
                await asyncio.shield(self.deduplicator.release(key))
                raise
            self.accepted_updates += 1
            return IngestResult.ACCEPTED

        try:
            self.queue.put_nowait(update)
        except asyncio.QueueFull as exc:
            try:
                await self.deduplicator.release(key)
            except DeduplicationUnavailable as release_error:
                logger.error(
                    "failed to release dedup key after queue overflow",
                    extra={"error_type": type(release_error.__cause__).__name__},
                )
            raise IngestQueueFull("incoming update queue is full") from exc

        self.accepted_updates += 1
        return IngestResult.ACCEPTED


def deduplication_key(update: Update) -> str:
    message = update.business_message
    if message is not None and message.business_connection_id is not None:
        return (
            f"business-message:{message.business_connection_id}:"
            f"{message.chat.id}:{message.message_id}"
        )
    return f"update:{update.update_id}"
