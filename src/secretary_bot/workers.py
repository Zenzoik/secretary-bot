from __future__ import annotations

import asyncio
import logging

from secretary_bot.delayed import DelayedReplyQueue
from secretary_bot.morning import MorningDigest
from secretary_bot.pipeline import Pipeline

logger = logging.getLogger(__name__)

DELAYED_POLL_SECONDS = 1.0
MORNING_POLL_SECONDS = 60.0


async def run_delayed_replies(
    pipeline: Pipeline, queue: DelayedReplyQueue, *, interval: float = DELAYED_POLL_SECONDS
) -> None:
    """Deliver replies whose delay has elapsed, including ones left by a restart."""
    while True:
        try:
            for task in await queue.pop_due():
                await pipeline.deliver(task)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("delayed reply worker failed: %s", type(exc).__name__)
        await asyncio.sleep(interval)


async def run_morning_digest(
    digest: MorningDigest, *, interval: float = MORNING_POLL_SECONDS
) -> None:
    while True:
        try:
            await digest.run_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("morning digest worker failed: %s", type(exc).__name__)
        await asyncio.sleep(interval)
