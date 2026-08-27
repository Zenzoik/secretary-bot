# Telegram Secretary Bot

Персональный автоответчик для Telegram Business. Сейчас в репозитории реализован
безопасный PoC Этапа 0: защищённый webhook принимает Business-апдейты, фиксирует
права подключения без тел сообщений и умеет отправить тестовое эхо от имени
владельца.

## Локальный запуск PoC

Требования: Python 3.12 и `uv`.

```bash
cp .env.example .env
# Заполнить BOT_TOKEN, WEBHOOK_SECRET и PUBLIC_BASE_URL в .env
set -a
source .env
set +a
uv sync --dev
uv run uvicorn secretary_bot.application:create_app --factory --host 0.0.0.0 --port 8000
```

Проверка процесса:

```bash
curl --fail http://127.0.0.1:8000/healthz
```

Для Telegram нужен публичный HTTPS URL, проксирующий запросы на порт 8000. После
его настройки зарегистрировать webhook:

```bash
uv run secretary-set-webhook
```

### Временный публичный URL для PoC

На macOS установить `cloudflared`:

```bash
brew install cloudflared
```

Оставить приложение запущенным на порту 8000, открыть второй терминал и выполнить:

```bash
cloudflared tunnel --url http://127.0.0.1:8000
```

Команда напечатает адрес вида `https://random-words.trycloudflare.com`. Записать
его в `.env` без завершающего `/`:

```dotenv
PUBLIC_BASE_URL=https://random-words.trycloudflare.com
```

Загрузить обновлённое окружение и зарегистрировать webhook:

```bash
set -a
source .env
set +a
uv run secretary-set-webhook
```

Оба процесса — Uvicorn и `cloudflared` — должны продолжать работать. Quick Tunnel
предназначен только для PoC: после перезапуска `cloudflared` URL изменится, и
понадобится снова обновить `PUBLIC_BASE_URL` и webhook.

Эхо намеренно отключено по умолчанию. Включать его следует только на время
контролируемого теста и только вместе с серверным allowlist числовых Telegram ID:

```dotenv
POC_ECHO_ENABLED=true
POC_ALLOWED_CHAT_IDS=123456789
```

Приложение откажется запускаться с включённым эхо и пустым allowlist. Область
чатов в Telegram остаётся первым уровнем защиты, но не заменяет эту проверку на
сервере.

Пошаговая ручная проверка и журнал результатов находятся в
[`docs/stage-0-poc.md`](docs/stage-0-poc.md).

## Автоматические проверки

```bash
uv run ruff check .
uv run pytest
```

## Схема базы данных

Модели SQLAlchemy находятся в `src/secretary_bot/models.py`, миграции — в
`migrations/`. Для применения миграций указать PostgreSQL URL с asyncpg в
`DATABASE_URL` и выполнить:

```bash
uv run alembic upgrade head
```

Проверить SQL миграции без подключения к базе:

```bash
uv run alembic upgrade head --sql
```

Секреты хранятся только в `.env`, который исключён из Git. Код не пишет тела
сообщений в логи или файлы.
