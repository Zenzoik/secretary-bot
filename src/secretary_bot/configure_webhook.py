from __future__ import annotations

import asyncio

from aiogram import Bot

from secretary_bot.config import Settings

_ALLOWED_UPDATES = [
    # Owner control commands arrive as ordinary private messages with the bot.
    "message",
    "business_connection",
    "business_message",
    "edited_business_message",
    "deleted_business_messages",
    # Verdict buttons under the dry-run preview arrive as callback queries;
    # without this Telegram silently never delivers them.
    "callback_query",
]


async def configure() -> None:
    settings = Settings.from_env(require_public_url=True)
    async with Bot(token=settings.bot_token) as bot:
        await bot.set_webhook(
            url=settings.webhook_url,
            secret_token=settings.webhook_secret,
            allowed_updates=_ALLOWED_UPDATES,
            drop_pending_updates=False,
        )
        info = await bot.get_webhook_info()
        print(f"Webhook configured: {info.url}")
        print(f"Pending updates: {info.pending_update_count}")
        if info.last_error_message:
            print(f"Last Telegram error: {info.last_error_message}")


def main() -> None:
    asyncio.run(configure())


if __name__ == "__main__":
    main()
