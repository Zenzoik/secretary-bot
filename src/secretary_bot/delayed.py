from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

MIN_DELAY_SECONDS = 60
MAX_DELAY_SECONDS = 240


def reply_delay(*, rng: random.Random | None = None) -> timedelta:
    """FR-8: 60–240 seconds. An instant answer at 03:14 reads as a machine."""
    generator = rng or random
    return timedelta(seconds=generator.randint(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS))


@dataclass(frozen=True, slots=True)
class ReplyTask:
    """A decision waiting out its delay.

    Carries identifiers and the chosen template only — the message body
    never reaches Redis (NFR-2).
    """

    connection_id: int
    business_connection_id: str
    contact_id: int
    chat_id: int
    message_id: int
    log_id: int
    template_code: str
    category: str
    incoming_at: str
    confidence: str | None = None
    window_key: str | None = None
    contact_name: str | None = None

    @property
    def incoming_moment(self) -> datetime:
        return datetime.fromisoformat(self.incoming_at)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, payload: str) -> ReplyTask:
        return cls(**json.loads(payload))


class SortedSetClient(Protocol):
    async def zadd(self, name: str, mapping: dict[str, float]) -> Any: ...

    async def zrangebyscore(self, name: str, min: float, max: float) -> list[str]: ...

    async def zrem(self, name: str, *values: str) -> int: ...

    async def zcard(self, name: str) -> int: ...


@dataclass(slots=True)
class DelayedReplyQueue:
    """Pending replies in a Redis sorted set, scored by their due time.

    Redis outlives the container, so a restart resumes the queue instead of
    dropping the replies it was holding.
    """

    client: SortedSetClient
    key: str = "secretary:delayed"

    async def schedule(self, task: ReplyTask, *, due_at: datetime) -> None:
        await self.client.zadd(self.key, {task.to_json(): due_at.timestamp()})

    async def pop_due(self, *, now: datetime | None = None) -> list[ReplyTask]:
        """Claim every task whose delay has elapsed.

        The ZREM result is the claim: whoever removes a member owns it, so a
        second worker cannot send the same reply twice.
        """
        moment = now or datetime.now(UTC)
        members = await self.client.zrangebyscore(self.key, 0, moment.timestamp())
        claimed = []
        for member in members:
            if await self.client.zrem(self.key, member):
                claimed.append(ReplyTask.from_json(member))
        return claimed

    async def pending(self) -> int:
        return await self.client.zcard(self.key)
