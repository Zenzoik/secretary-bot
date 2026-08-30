from __future__ import annotations

BOT_IDENTITY_PREFIX = "🤖 Відповідає секретар:"

CONNECTION_DISABLED_ALERT = (
    "⚠️ Telegram Business відключено. Чергу відповідей очищено, "
    "автовідповіді зупинено."
)
REPLY_PERMISSION_LOST_ALERT = (
    "⚠️ Бот більше не має права відповідати. Чергу відповідей очищено, "
    "автовідповіді зупинено."
)
READ_PERMISSION_LOST_ALERT = (
    "⚠️ Бот більше не має права позначати повідомлення прочитаними. "
    "Отримання повідомлень і відповіді продовжують працювати."
)
CONNECTION_LOST_ALERT = (
    "⚠️ Telegram відхилив відправлення: з'єднання недійсне. "
    "Чергу відповідей очищено, автовідповіді зупинено."
)


def as_bot_reply(text: str) -> str:
    return f"{BOT_IDENTITY_PREFIX}\n{text}"
