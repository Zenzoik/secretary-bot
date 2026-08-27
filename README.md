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

Эхо намеренно отключено по умолчанию. Включать его следует только на время
контролируемого теста:

```dotenv
POC_ECHO_ENABLED=true
```

Пошаговая ручная проверка и журнал результатов находятся в
[`docs/stage-0-poc.md`](docs/stage-0-poc.md).

## Автоматические проверки

```bash
uv run ruff check .
uv run pytest
```

Секреты хранятся только в `.env`, который исключён из Git. Код не пишет тела
сообщений в логи или файлы.
