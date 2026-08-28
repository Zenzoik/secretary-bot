# Telegram Secretary Bot

Пользовательские команды, сценарии и настройки описаны в
[`README-USER.md`](README-USER.md).

Персональный автоответчик для Telegram Business. В нерабочее время бот отвечает
одним коротким сообщением от имени владельца, а денежные вопросы кладёт в
утренний список. Тела сообщений не сохраняются нигде.

Реализованы ядро Этапа 1 и control plane Этапа 1.5: webhook с дедупликацией,
hard filter, gate (расписание, исключения, один ответ на окно), классификатор со
словарным fallback, отложенная отправка с проверкой «владелец ответил сам»,
dry-run с превью владельцу, утренний список и управление из личного чата с
ботом.

Новое подключение всегда стартует в `dry_run = true`: контакту не уходит ничего,
пока владелец не переключит режим.

Команды владельца:

| Команда | Действие |
|---|---|
| `/status` | Состояние, режим и временная пауза |
| `/off` / `/on` | Немедленно остановить или вернуть работу |
| `/mute N` | Пауза на N часов с автоматическим окончанием |
| `/today` | Агрегаты действий за локальные сутки |
| `/live` | Запросить переход из dry-run с отдельным подтверждением |

Кнопка Telegram Manage Bot открывает карточку текущего контакта. Подробности и
журнал живой проверки: [`docs/stage-1.5-control-plane.md`](docs/stage-1.5-control-plane.md).

## Локальный запуск

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

Нужны Redis по адресу `REDIS_URL` и PostgreSQL по `DATABASE_URL`. Redis хранит
дедуп-ключи и отложенные отправки — только идентификаторы и код шаблона, без тел
сообщений; поэтому перезапуск контейнера не теряет запланированные ответы.

Без `ANTHROPIC_API_KEY` классификатор работает на словаре ключевых слов: бот не
падает и не молчит, просто теряет точность.

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

Область действия по умолчанию — «все чаты, кроме исключений» (FR-2). На время
отладки её можно сузить серверным allowlist числовых Telegram ID:

```dotenv
ALLOWED_CHAT_IDS=123456789
```

Пустое значение снимает ограничение. Область чатов в настройках Telegram
остаётся первым уровнем защиты.

Пошаговая ручная проверка и журнал результатов находятся в
[`docs/stage-0-poc.md`](docs/stage-0-poc.md).

## Запуск в Docker

```bash
cp .env.example .env
# Обязательны: BOT_TOKEN, WEBHOOK_SECRET, POSTGRES_PASSWORD, PUBLIC_BASE_URL
docker compose up -d --build
docker compose logs -f app
```

`POSTGRES_PASSWORD` читается из `.env` самим Compose: он создаёт с ним базу и
подставляет его в `DATABASE_URL` приложения. Значения `DATABASE_URL` и
`REDIS_URL` из `.env` при запуске в Compose не используются — они нужны только
для локального запуска без Docker.

Compose поднимает четыре сервиса: PostgreSQL, Redis с включённым AOF, разовую
задачу `migrate` (`alembic upgrade head`) и приложение. Порт 8000 слушает только
127.0.0.1 — публичный HTTPS отдаёт реверс-прокси на хосте, он же держит
сертификат и передаёт запросы на webhook.

После первого запуска зарегистрировать webhook:

```bash
docker compose exec app secretary-set-webhook
```

Обновление на VPS:

```bash
git pull
docker compose up -d --build
```

`migrate` выполняется до старта приложения, тома `postgres-data` и `redis-data`
переживают пересборку: отложенные ответы не теряются.

## Автоматические проверки

```bash
uv run ruff check .
uv run python -m pytest
uv run python -m pytest --cov=secretary_bot.gate --cov-branch \
  --cov-report=term-missing tests/test_gate.py
```

Последняя команда держит требование Этапа 1: покрытие ветвлений gate — 100%.

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
