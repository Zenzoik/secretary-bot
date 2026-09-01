from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

BUTTON_STATUS = "📊 Статус"
BUTTON_TODAY = "🗓 Сьогодні"
BUTTON_OFF = "⛔ Вимкнути"
BUTTON_ON = "▶️ Увімкнути"
BUTTON_MUTE = "⏸ Пауза"
BUTTON_LIVE = "⚠️ Увімкнути live"
BUTTON_LIVE_ACTIVE = "🔴 Live увімкнено"
BUTTON_BACK = "↩️ Назад"
BUTTON_LIVE_CONFIRM = "⚠️ Підтверджую live"
BUTTON_CANCEL = "Скасувати"
BUTTON_USERS = "👥 Користувачі"
BUTTON_INVITE = "➕ Запросити"
BUTTON_USERS_REFRESH = "🔄 Оновити список"
BUTTON_ADMIN_BACK = "↩️ Головне меню"
BUTTON_SCOPE_CONFIRMED = "✅ Only Selected Chats налаштовано"
BUTTON_RECHECK_CONNECTION = "🔄 Перевірити підключення"
MENU_SETTINGS = "Налаштування"

TIMEZONE_LABELS = {
    "🇺🇦 Київ": "Europe/Kyiv",
    "🇨🇿 Прага": "Europe/Prague",
    "🇵🇱 Варшава": "Europe/Warsaw",
    "🌐 UTC": "UTC",
}
SCHEDULE_LABELS = (
    "🌙 Щодня 22–08",
    "💼 Будні 18–09",
    "🧪 Тестовий 24/7",
)
MUTE_LABELS = {"1 година": 1, "3 години": 3, "8 годин": 8, "24 години": 24}

INVITE_INVALID = "Посилання-запрошення недійсне, уже використане або протерміноване."
INVITE_REQUESTED = "✅ Запит надіслано майстру. Дочекайтеся підтвердження доступу."
MASTER_ACCESS_REQUEST = "👤 Отримано новий запит доступу. Відкрийте «Користувачі» для перевірки."
ACCESS_ALREADY_ACTIVE = "Доступ уже активний. Надішліть /start."
ACCESS_PENDING = "Запит очікує підтвердження майстром."
ACCESS_REVOKED = "Доступ до бота відкликано. Зверніться до майстра."
CONNECT_BOT = "Підключіть бота в Telegram Chat Automation, потім натисніть перевірку."
CONNECTION_OFF = "Підключення вимкнено. Увімкніть бота в Chat Automation і перевірте ще раз."
ONBOARDING_TIMEZONE = "Крок 1/3. Виберіть часовий пояс."
ONBOARDING_TIMEZONE_BUTTON = "Крок 1/3. Виберіть часовий пояс кнопкою."
ONBOARDING_BACK_TIMEZONE = "Повернулися до вибору часового поясу."
ONBOARDING_SCHEDULE = "Крок 2/3. Виберіть безпечний шаблон розкладу."
ONBOARDING_SCOPE = (
    "Крок 3/3. У Chat Automation виберіть Only Selected Chats і додайте один тестовий контакт."
)
ONBOARDING_BACK_SCHEDULE = "Повернулися до вибору розкладу."
ONBOARDING_SCOPE_CONFIRM = "Підтвердьте Only Selected Chats лише після налаштування в Telegram."
ONBOARDING_CONNECTION_CHANGED = "Права або підключення змінилися. Перевірте Chat Automation ще раз."
ONBOARDING_DONE = (
    "✅ Налаштування завершено. Режим dry-run; live доступний лише після ручного підтвердження."
)
ONBOARDING_ALREADY_DONE = "Налаштування вже завершено."
PANEL_OPEN = "Панель керування відкрита. Для картки контакту використовуйте Manage Bot."
FORBIDDEN = "Недостатньо прав."
MUTE_SELECT = "Виберіть тривалість паузи кнопкою."
LIVE_SELECT = "Підтвердьте live або скасуйте перехід кнопкою."
MAIN_MENU = "Головне меню."
SECRETARY_OFF = "⛔ Секретаря вимкнено. Уже заплановані відповіді також зупинено."
SECRETARY_ON = "✅ Секретаря увімкнено. Тимчасову паузу знято."
MUTE_QUESTION = "На скільки годин поставити паузу?"
LIVE_ALREADY = "Live-режим уже увімкнено."
LIVE_PROMPT = "⚠️ Увімкнути live? Після підтвердження бот зможе відповідати контактам."
LIVE_ENABLED = "⚠️ Live-режим увімкнено. Бот може відповідати контактам."
LIVE_EXPIRED = "Підтвердження протерміновано. Режим не змінено."
LIVE_EXPIRED_RETRY = "Підтвердження протерміновано. Повторіть увімкнення live."
DRY_RUN_SAVED = "Dry-run збережено. Live-режим не увімкнено."
CONTACT_EXCLUDED = "🚫 Контакт виключено назавжди."
CONTACT_TEMPLATE_PROMPT = "Виберіть шаблон для цього контакту:"
USER_APPROVED = "✅ Користувача підтверджено. Йому надіслано інструкцію підключення."
USER_STATE_CHANGED = "Стан користувача вже змінено."
USER_APPROVED_NOTICE = (
    "✅ Доступ підтверджено. Підключіть бота в Chat Automation, потім надішліть /start."
)
USER_REVOKED = "⛔ Доступ користувача відкликано."
USER_NOT_REVOCABLE = "Користувача не знайдено або його захищено від відкликання."
USER_REVOKED_NOTICE = "⛔ Доступ до секретаря відкликано майстром. Автоматичні дії зупинено."

CONTACT_EXCLUDE_BUTTON = "🚫 Виключити назавжди"
CONTACT_TODAY_BUTTON = "😴 Не турбувати сьогодні"
CONTACT_TEMPLATE_BUTTON = "✏️ Власний шаблон"
CONTACT_OK_BUTTON = "◀️ Усе гаразд"
TEMPLATE_GENERAL_BUTTON = "Звичайний"
TEMPLATE_MONEY_BUTTON = "Грошовий"
ACCESS_REVOKE_BUTTON = "⛔ Відкликати"
RIGHT_REPLY = "відповіді"

PLACEHOLDER_CONNECTION = "Підключіть бота в Chat Automation"
PLACEHOLDER_TIMEZONE = "Виберіть часовий пояс"
PLACEHOLDER_SCHEDULE = "Виберіть розклад"
PLACEHOLDER_SCOPE = "Підтвердьте область чатів"
PLACEHOLDER_MAIN = "Керування секретарем"
PLACEHOLDER_MUTE = "Виберіть тривалість паузи"
PLACEHOLDER_LIVE = "Підтвердьте або скасуйте live"

FEEDBACK_BUTTONS = (
    ("ok", "✅ Норм"),
    ("wrong", "❌ Не треба було"),
    ("exclude", "🚫 Виключити"),
)

FEEDBACK_RESULTS = {
    "ok": ("✅ Оброблено: оцінка «Норм»", "Записано: Норм"),
    "wrong": ("✅ Оброблено: оцінка «Не треба було»", "Оцінку записано"),
    "exclude": ("✅ Оброблено: кандидат на виключення", "Оцінку записано"),
}

OFF_HOURS_TEMPLATE = "Зараз неробочий час, відповім пізніше"
MONEY_PRIORITY_TEMPLATE = (
    "Зараз неробочий час. Питання щодо оплати побачив, відповім насамперед уранці."
)
BOT_IDENTITY_SUFFIX = "— 🤖 Секретар"

CONNECTION_DISABLED_ALERT = (
    "⚠️ Telegram Business відключено. Чергу відповідей очищено, автовідповіді зупинено."
)
REPLY_PERMISSION_LOST_ALERT = (
    "⚠️ Бот більше не має права відповідати. Чергу відповідей очищено, автовідповіді зупинено."
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
    """Add attribution that is visible in every Telegram client."""
    return f"{text}\n\n{BOT_IDENTITY_SUFFIX}"


def missing_rights(rights: list[str]) -> str:
    return (
        "Бракує прав Telegram: "
        + ", ".join(rights)
        + ". Дозвольте відповіді, потім натисніть перевірку."
    )


def timezone_selected(timezone: str) -> str:
    return f"Часовий пояс: {timezone}.\nКрок 2/3. Виберіть розклад."


def invite_link(link: str) -> str:
    return f"➕ Запрошення діє 24 години й лише один раз:\n{link}"


def invalid_mute_hours(max_hours: int) -> str:
    return f"Виберіть кнопку або вкажіть від 1 до {max_hours} годин."


def muted_until(local_until: datetime, hours: int) -> str:
    return f"⏸ Пауза до {local_until:%d.%m %H:%M} ({hours} год.)."


def contact_excluded_until(until: datetime) -> str:
    return f"😴 Контакт виключено до {until:%d.%m %H:%M}."


def contact_template_selected(code: str) -> str:
    return f"✏️ Для контакту вибрано шаблон: {code}."


def callback_feedback(
    contact: tuple[int, str, str | None] | None,
    live_action: str | None,
    access_action: tuple[str, int] | None,
) -> tuple[str, str]:
    if access_action is not None:
        action, _ = access_action
        return (
            ("✅ Оброблено: доступ підтверджено", "Доступ підтверджено")
            if action == "approve"
            else ("✅ Оброблено: доступ відкликано", "Доступ відкликано")
        )
    if live_action == "confirm":
        return "✅ Оброблено: live увімкнено", "Live увімкнено"
    if live_action == "cancel":
        return "✅ Оброблено: dry-run збережено", "Скасовано"
    assert contact is not None
    _, action, argument = contact
    if action == "exclude":
        return "✅ Оброблено: контакт виключено назавжди", "Контакт виключено"
    if action == "today":
        return "✅ Оброблено: контакт не турбуємо сьогодні", "Виключено до опівночі"
    if action == "templates":
        return "✅ Оброблено: вибір персонального шаблону", "Виберіть шаблон"
    if action == "template":
        return f"✅ Оброблено: вибрано шаблон {argument}", "Шаблон вибрано"
    return "✅ Оброблено: без змін", "Без змін"


def render_status(connection: Any, *, now: datetime) -> str:
    muted = connection.policy.muted_until
    if connection.policy.kill_switch:
        state, pause = "вимкнено", "немає"
    elif muted is not None and now < muted:
        local_until = muted.astimezone(ZoneInfo(connection.policy.timezone))
        state, pause = "пауза", f"до {local_until:%d.%m %H:%M}"
    else:
        state, pause = "увімкнено", "немає"
    mode = "dry-run" if connection.dry_run else "live"
    return (
        f"🤖 Секретар: {state}\n"
        f"Режим: {mode}\n"
        f"Пауза: {pause}\n"
        f"Часовий пояс: {connection.policy.timezone}"
    )


def render_today(counts: list[tuple[str, str | None, int]], *, local_date: str) -> str:
    if not counts:
        return f"📊 {local_date}: дій поки немає."
    lines = [f"📊 {local_date}:"]
    for action, category, count in counts:
        label = action if category is None else f"{action}/{category}"
        lines.append(f"• {label}: {count}")
    return "\n".join(lines)


def render_access_users(users: list[Any]) -> str:
    lines = ["👥 Користувачі:"]
    labels = {"pending": "очікує", "active": "активний", "revoked": "відкликаний"}
    for user in users:
        identity = f"@{user.username}" if user.username else f"ID {user.user_id}"
        role = "майстер" if user.role == "master" else labels[user.status]
        state = "" if user.role == "master" else f" · {user.onboarding_state}"
        lines.append(f"• {identity} — {role}{state}")
    return "\n".join(lines)


def render_contact_card(card: Any, *, timezone: str) -> str:
    zone = ZoneInfo(timezone)
    name = card.contact_name or f"Контакт {card.contact_id}"
    last = (
        "немає"
        if card.last_auto_reply_at is None
        else card.last_auto_reply_at.astimezone(zone).strftime("%d.%m %H:%M")
    )
    if card.permanently_excluded:
        exclusion = "назавжди"
    elif card.exclusion_until is not None:
        exclusion = f"до {card.exclusion_until.astimezone(zone):%d.%m %H:%M}"
    else:
        exclusion = "немає"
    forced = card.forced_template_code or "автоматично"
    return (
        f"👤 {name}\n"
        f"Автовідповідей за 30 днів: {card.auto_reply_count}\n"
        f"Остання: {last}\n"
        f"Виключення: {exclusion}\n"
        f"Шаблон: {forced}"
    )


def render_preview(
    *,
    occurred_at: datetime,
    contact_id: int,
    contact_name: str | None,
    category: str,
    confidence: str | None,
    reply_text: str,
) -> str:
    who = contact_name or f"id {contact_id}"
    shown_category = category if confidence is None else f"{category} ({confidence})"
    return (
        f"🌙 {occurred_at:%H:%M} · {who}\nКатегорія: {shown_category}\nЯ б відповів: «{reply_text}»"
    )


def render_morning_digest(rows: list[Any], *, timezone: str) -> str:
    zone = ZoneInfo(timezone)
    lines = ["☀️ Уранці обіцяли відповісти:"]
    for row in rows:
        who = row.contact_name or f"id {row.contact_id}"
        detail = row.summary or "питання про гроші"
        lines.append(f"• {row.occurred_at.astimezone(zone):%H:%M} · {who} — {detail}")
    return "\n".join(lines)
