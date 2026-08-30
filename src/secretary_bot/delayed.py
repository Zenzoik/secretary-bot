from __future__ import annotations

import json
import random
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

MIN_DELAY_SECONDS = 10
MAX_DELAY_SECONDS = 60


def reply_delay(
    *,
    min_seconds: int = MIN_DELAY_SECONDS,
    max_seconds: int = MAX_DELAY_SECONDS,
    rng: random.Random | None = None,
) -> timedelta:
    """Choose an inclusive delay inside the connection's configured range."""
    if not 0 <= min_seconds <= max_seconds:
        raise ValueError("delay must satisfy 0 <= min <= max")
    generator = rng or random
    return timedelta(seconds=generator.randint(min_seconds, max_seconds))


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
    template_code: str
    category: str
    incoming_at: str
    sender_identity: str = "owner"
    confidence: str | None = None
    window_key: str | None = None
    contact_name: str | None = None
    delivery_attempts: int = 0

    @property
    def incoming_moment(self) -> datetime:
        return datetime.fromisoformat(self.incoming_at)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, payload: str) -> ReplyTask:
        values = json.loads(payload)
        # Tasks created before Stage 1.7 were always sent as the owner.
        values.setdefault("sender_identity", "owner")
        values.setdefault("delivery_attempts", 0)
        return cls(**values)


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

    async def cancel_connection(self, connection_id: int) -> int:
        """Remove every pending task owned by one revoked connection."""
        members = await self.client.zrangebyscore(self.key, float("-inf"), float("inf"))
        cancelled = 0
        for member in members:
            task = ReplyTask.from_json(member)
            if task.connection_id == connection_id:
                cancelled += await self.client.zrem(self.key, member)
        return cancelled
