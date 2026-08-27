# Telegram Secretary Bot — Архитектура

Версия: 1.0
Дата: 27.08.2026
Статус: черновик под реализацию

---

## 1. Что это

Персональный бот-автоответчик, подключаемый к личному Telegram-аккаунту владельца через **Secretary Mode** (Business Connections API). Бот читает входящие сообщения в приватных чатах и в нерабочее время отвечает от имени владельца.

Получатель не видит пометки «отправлено ботом» — сообщение приходит от имени и аватара владельца. Это накладывает требования к честности формулировок (см. §9).

Два сценария ответа:

| Условие | Ответ |
|---|---|
| Обычное сообщение в нерабочее время | «Сейчас нерабочее время, отвечу позже» |
| Сообщение с денежной тематикой | «Отвечу первым делом утром» + запись в утренний список владельца |

---

## 2. Ограничения платформы

Определяют дизайн, обойти нельзя.

### 2.1 Окно 24 часа

Бот может отправлять сообщения только в приватные чаты, где было входящее сообщение за последние 24 часа. Попытка написать в «остывший» чат → ошибка `BUSINESS_CHAT_INACTIVE`.

**Следствие:** холодная инициация переписки невозможна. Любые отложенные ответы должны укладываться внутрь окна. Обещание «отвечу утром» бот выполнить не может — выполняет человек.

### 2.2 Только приватные чаты

Группы, каналы, комментарии — вне зоны действия `can_reply`. Групп в продукте не будет.

### 2.3 Управление доступом к чатам живёт в Telegram

Владелец сам в `Настройки → Telegram Business → Чат-боты` указывает область действия бота. Наш веб-интерфейс этим управлять **не может**.

Выбранная политика: **«все чаты, кроме исключений»**. Новые контакты автоматически попадают под действие бота.

**Следствие:** через сервер проходит весь входящий личный поток владельца. См. §9.

### 2.4 Права бота (BusinessBotRights)

Выдаются владельцем при подключении, приходят в поле `rights` объекта `BusinessConnection`. Нужны:

- `can_reply` — отправка и редактирование сообщений (обязательно)
- `can_read_messages` — отметка входящего прочитанным после успешного автоответа

Права могут быть изменены владельцем в любой момент — приходит апдейт `business_connection`. Перед каждой отправкой проверяем актуальные права из БД.

### 2.5 Результаты проверки платформы на Этапе 0

1. **Telegram Premium и интерфейс подключения.** Полный профиль и набор функций `Telegram Business` требуют Premium. Отдельный пункт `Chat Automation`, достаточный для подключения secretary-бота, по официальной документации на 27.08.2026 доступен без Premium. Текущий PoC проверен на Premium-аккаунте; путь подключения через `Chat Automation` на аккаунте без подписки требует отдельной практической проверки клиента и раскатки функции.
2. **Исходящие сообщения самого владельца приходят** как `business_message`. Проверено 27.08.2026 на реальном Business Connection: обработчик надёжно распознаёт владельца по `from.id` и фиксирует `message_skipped_owner` (§6.4).

---

## 3. Стек

| Слой | Выбор | Почему |
|---|---|---|
| Язык | Python 3.12 | скорость разработки, совпадает с остальными проектами |
| Bot framework | aiogram 3.x | business mode из коробки: `business_message`, `BusinessConnection`, `business_connection_id` |
| Web/API | FastAPI | webhook + REST для Mini App в одном процессе |
| Очередь / кэш | Redis | дедуп, отложенные задачи, rate-limit |
| БД | PostgreSQL 16 | |
| Миграции | Alembic | |
| LLM | Claude (Anthropic API) | классификация + генерация |
| Планировщик | APScheduler в процессе воркера | cron-задачи, дайджест позже |
| Frontend | Telegram Mini App (React + Vite) | Этап 3 |
| Деплой | Docker Compose на VPS €5–10 | тот же хост, что и остальные боты |
| Логи / алерты | structlog в stdout + алерты в Telegram владельцу | |

Альтернатива для JS-стека — grammY, business mode тоже поддержан. Выбор Python обусловлен переиспользованием кода в соседних проектах.

---

## 4. Компоненты

```
┌──────────────────────────────────────────────────────────┐
│  Telegram Bot API                                        │
└────────────────┬─────────────────────────────────────────┘
                 │ webhook (HTTPS, secret_token)
                 ▼
┌──────────────────────────────────────────────────────────┐
│  INGEST (FastAPI)                                        │
│  • валидация secret_token                                │
│  • дедуп по (chat_id, message_id) в Redis, TTL 24h       │
│  • быстрый 200 OK, вся работа — в очередь                │
└────────────────┬─────────────────────────────────────────┘
                 ▼
          Redis: queue:incoming
                 │
                 ▼
┌──────────────────────────────────────────────────────────┐
│  WORKER                                                  │
│                                                          │
│  1. HARD FILTER      боты, сервисные аккаунты, свои msg  │
│  2. GATE             расписание, исключения, лимит окна  │
│  3. CLASSIFY         LLM → {category, confidence}        │
│  4. DELAY            рандом 60–240 сек                   │
│  5. SELF-REPLY CHECK владелец ответил сам?               │
│  6. DECIDE           выбор шаблона                       │
│  7. SEND             sendMessage(business_connection_id) │
│     └─ если dry_run → отправить превью владельцу         │
│  8. LOG              запись без тела сообщения           │
│  9. FLAG             если money → в утренний список      │
└────────────────┬─────────────────────────────────────────┘
                 ▼
┌──────────────────────────────────────────────────────────┐
│  PostgreSQL                                              │
└────────────────┬─────────────────────────────────────────┘
                 ▲
                 │ REST
┌────────────────┴─────────────────────────────────────────┐
│  CONTROL PLANE                                           │
│  • команды в личке бота: /off /on /mute /status /today   │
│  • deep-link «Manage Bot» → карточка контакта            │
│  • Mini App (Этап 3): расписание, исключения, шаблоны    │
└──────────────────────────────────────────────────────────┘
```

Ingest и Worker живут в одном контейнере, но как разные процессы. Ingest обязан отвечать Telegram быстро; вся логика, включая задержку и вызов LLM, — асинхронно в воркере.

---

## 5. Обрабатываемые апдейты

| Update | Что делаем |
|---|---|
| `business_connection` | создаём/обновляем запись в `connections`: `is_enabled`, `rights_json`. При `is_enabled=false` — помечаем соединение мёртвым, отправку останавливаем |
| `business_message` | основной поток, см. §6 |
| `edited_business_message` | игнорируем в MVP |
| `deleted_business_messages` | помечаем соответствующие записи лога как удалённые |
| `message` (личка с ботом) | команды управления, deep-link `/start bizChat<user_chat_id>` |
| `callback_query` | кнопки в карточке контакта |

---

## 6. Пайплайн обработки входящего

### 6.1 Hard filter

Жёсткие правила в коде, не настраиваются:

- `from.is_bot == true` → drop
- `from.id == 777000` (Telegram Notifications) → drop
- `from.id == connection.owner_user_id` → это исходящее самого владельца → не отвечаем, но **регистрируем факт активности** (см. §6.4)
- отсутствие текста (стикер, гс, файл без подписи) → в MVP drop, в лог с пометкой `unsupported_content`

### 6.2 Gate

Проверки по порядку, первая сработавшая останавливает обработку:

1. `connections.is_active == false` или `dry_run` глобально выключен вместе с отправкой → drop
2. `kill_switch == true` (команда `/off`) → drop
3. контакт в `exclusions` и `until` не истёк → drop
4. текущее время **не** попадает в quiet-окно из `schedules` → drop
5. на этот контакт уже был автоответ в текущем quiet-окне → drop

Пункт 5 важнее обычного cooldown в часах: десять сообщений подряд ночью дают **один** ответ, а не десять.

### 6.3 Классификация

Один вызов LLM, строгий JSON на выходе:

```json
{
  "category": "money" | "general",
  "confidence": 0.0-1.0,
  "reason": "короткое пояснение для лога"
}
```

Правила устойчивости:

- в промпт уходит **только текущее сообщение**, без истории переписки
- таймаут 8 сек → fallback
- fallback: словарь ключевых слов (`счёт, оплата, платіж, инвойс, реквизиты, аванс, предоплата, долг, гривн, євро, доллар, payment, invoice`)
- `confidence < 0.7` → трактуем как `general` (общий шаблон безопаснее)
- любая ошибка LLM → `general`

На Этапе 1.5 (shadow) классификатор работает с первого дня и ничем не рискует — за неделю накапливается материал для калибровки порога.

### 6.4 Задержка и проверка «ответил сам»

Задача ставится в Redis с задержкой `random(60, 240)` секунд. Мгновенный ответ в 03:14 читается как автомат; задержка решает и это, и даёт окно живому человеку.

Перед отправкой проверяем `contact_activity.owner_last_reply_at`:
если владелец написал в этот чат после нашего входящего — задачу отменяем, в лог `skipped_owner_replied`.

**Проверено на Этапе 0:** исходящие владельца приходят в `business_message`.
Механика отмены может надёжно сравнивать время ручного ответа с исходным
входящим сообщением; деградированная эвристика «владелец был онлайн» не нужна.

### 6.5 Отправка

```python
await bot.send_message(
    business_connection_id=conn.business_connection_id,
    chat_id=chat_id,
    text=template.text,
)
```

Обработка ошибок:

| Ошибка | Действие |
|---|---|
| `BUSINESS_CHAT_INACTIVE` | лог, не ретраим — окно 24h закрылось |
| `FLOOD_WAIT_x` | отложить на x+5 сек, максимум 3 попытки |
| `BUSINESS_CONNECTION_INVALID` | пометить соединение мёртвым, алерт владельцу |
| прочее | 3 ретрая с экспонентой, потом лог + алерт |

### 6.6 Dry run

Флаг `connections.dry_run`. Проверяется **в самой последней точке**, непосредственно перед вызовом `send_message`. Вместо отправки контакту — сообщение владельцу в личку бота:

```
🌙 03:14 · Вася Петров
«Слушай, а когда оплата пройдёт?»

Категория: money (0.91)
Я бы ответил: «Сейчас нерабочее время. Вопрос по оплате увидел,
отвечу первым делом утром.»

[✅ Норм]  [❌ Не надо было]  [🚫 Исключить контакт]
```

Реакции пишутся в `shadow_feedback` — это материал для калибровки перед включением боевого режима.

### 6.7 Money-флаг

Если `category == money` и ответ отправлен — запись в `morning_queue`. Утром в 08:00 владелец получает список: кто писал, во сколько, о чём.

Без этого бот обещает от имени владельца то, чего владелец не знает — то есть врёт его голосом. Три строчки кода, но без них фича вредная.

---

## 7. Модель данных

```sql
-- Подключения Secretary Mode
CREATE TABLE connections (
    id                      BIGSERIAL PRIMARY KEY,
    business_connection_id  TEXT UNIQUE NOT NULL,
    owner_user_id           BIGINT NOT NULL,
    owner_username          TEXT,
    rights_json             JSONB NOT NULL DEFAULT '{}',
    is_active               BOOLEAN NOT NULL DEFAULT true,
    dry_run                 BOOLEAN NOT NULL DEFAULT true,
    kill_switch             BOOLEAN NOT NULL DEFAULT false,
    timezone                TEXT NOT NULL DEFAULT 'Europe/Kyiv',
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Quiet-окна: когда бот работает
CREATE TABLE schedules (
    id             BIGSERIAL PRIMARY KEY,
    connection_id  BIGINT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    weekday_mask   SMALLINT NOT NULL,     -- битовая маска, bit0 = Пн
    time_from      TIME NOT NULL,
    time_to        TIME NOT NULL,         -- если time_to < time_from → окно через полночь
    is_active      BOOLEAN NOT NULL DEFAULT true
);

-- Исключения: контакты, которых бот не трогает
CREATE TABLE exclusions (
    id             BIGSERIAL PRIMARY KEY,
    connection_id  BIGINT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    contact_id     BIGINT NOT NULL,
    contact_name   TEXT,
    until          TIMESTAMPTZ,           -- NULL = навсегда
    reason         TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (connection_id, contact_id)
);

-- Персональные переопределения
CREATE TABLE overrides (
    id             BIGSERIAL PRIMARY KEY,
    connection_id  BIGINT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    contact_id     BIGINT NOT NULL,
    mode           TEXT NOT NULL,         -- 'always_silent' | 'always_reply' | 'force_template'
    template_id    BIGINT REFERENCES templates(id),
    UNIQUE (connection_id, contact_id)
);

-- Шаблоны ответов
CREATE TABLE templates (
    id             BIGSERIAL PRIMARY KEY,
    connection_id  BIGINT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    code           TEXT NOT NULL,         -- 'off_hours_default' | 'money_priority'
    text           TEXT NOT NULL,
    is_active      BOOLEAN NOT NULL DEFAULT true,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (connection_id, code)
);

-- Промпт классификатора, редактируемый из UI
CREATE TABLE prompts (
    id             BIGSERIAL PRIMARY KEY,
    connection_id  BIGINT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    code           TEXT NOT NULL,         -- 'classifier'
    system_prompt  TEXT NOT NULL,
    model          TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
    confidence_min NUMERIC(3,2) NOT NULL DEFAULT 0.70,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (connection_id, code)
);

-- Активность в чате: нужна для проверки «владелец ответил сам»
CREATE TABLE contact_activity (
    connection_id        BIGINT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    contact_id           BIGINT NOT NULL,
    last_incoming_at     TIMESTAMPTZ,
    owner_last_reply_at  TIMESTAMPTZ,
    last_auto_reply_at   TIMESTAMPTZ,
    quiet_window_key     TEXT,            -- 'YYYY-MM-DD:N' — окно последнего автоответа
    PRIMARY KEY (connection_id, contact_id)
);

-- Лог. В MVP БЕЗ тел сообщений
CREATE TABLE message_log (
    id                BIGSERIAL PRIMARY KEY,
    connection_id     BIGINT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    contact_id        BIGINT NOT NULL,
    tg_message_id     BIGINT,
    direction         TEXT NOT NULL,      -- 'in' | 'out'
    occurred_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    action            TEXT NOT NULL,      -- 'replied' | 'dry_run' | 'skipped_schedule'
                                          -- | 'skipped_excluded' | 'skipped_owner_replied'
                                          -- | 'skipped_window_limit' | 'error'
    category          TEXT,               -- 'money' | 'general'
    confidence        NUMERIC(3,2),
    template_code     TEXT,
    error_code        TEXT,

    -- зарезервировано под дайджест (Этап 4), в MVP всегда NULL
    body_encrypted    BYTEA,
    retention_until   TIMESTAMPTZ,

    deleted_by_user   BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX ON message_log (connection_id, occurred_at DESC);
CREATE INDEX ON message_log (connection_id, contact_id, occurred_at DESC);

-- Утренний список по денежным сообщениям
CREATE TABLE morning_queue (
    id             BIGSERIAL PRIMARY KEY,
    connection_id  BIGINT NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
    contact_id     BIGINT NOT NULL,
    contact_name   TEXT,
    occurred_at    TIMESTAMPTZ NOT NULL,
    summary        TEXT,                  -- одна строка от LLM, не полный текст
    is_delivered   BOOLEAN NOT NULL DEFAULT false,
    is_done        BOOLEAN NOT NULL DEFAULT false
);

-- Обратная связь из shadow-режима
CREATE TABLE shadow_feedback (
    id             BIGSERIAL PRIMARY KEY,
    log_id         BIGINT NOT NULL REFERENCES message_log(id) ON DELETE CASCADE,
    verdict        TEXT NOT NULL,         -- 'ok' | 'wrong' | 'exclude'
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### Заметки по схеме

- **`body_encrypted` и `retention_until` заведены заранее** и в MVP не заполняются. Это чтобы на Этапе 4 не мигрировать боевую таблицу.
- **`quiet_window_key`** — строковый идентификатор конкретного ночного окна. Позволяет реализовать «один ответ на окно» без арифметики с часами.
- **Мультиарендность заложена**: всё привязано к `connection_id`. Один инстанс обслуживает нескольких владельцев без переделки, если продукт пойдёт дальше личного использования.
- Данные разных `connection_id` изолированы на уровне запросов; при выходе за пределы личного использования — вынести тела сообщений в отдельную БД.

---

## 8. Control plane

### 8.1 Команды в личке с ботом

| Команда | Действие |
|---|---|
| `/status` | активно ли соединение, режим (dry/live), сколько ответов за сутки |
| `/off` | kill switch, отправка останавливается до `/on` |
| `/on` | снять kill switch |
| `/mute <часы>` | пауза на N часов |
| `/today` | что бот наотвечал за сегодня |
| `/live` | выйти из dry-run (требует подтверждения кнопкой) |

Kill switch должен срабатывать в два тапа с телефона. Разрыв Business Connection в настройках Telegram тоже останавливает бота, но дольше и теряет конфиг.

### 8.2 Deep-link из чата

В каждом управляемом чате Telegram показывает панель с кнопкой **«Manage Bot»**, которая ведёт в бота с диплинком `/start bizChat<user_chat_id>`.

Бот распознаёт префикс `bizChat`, достаёт контакт и показывает карточку:

```
Вася Петров
Автоответов за 30 дней: 4 · Последний: вчера 02:40

[🚫 Исключить навсегда]
[😴 Не трогать сегодня]
[✏️ Свой шаблон]
[◀️ Всё в порядке]
```

Это удобнее любого веб-интерфейса и делается за полдня.

### 8.3 Mini App (Этап 3)

React + Vite внутри Telegram, авторизация через `initData`. Разделы: расписание, исключения, шаблоны, промпт классификатора, лог. Веб-версия получается тем же кодом на отдельном домене.

---

## 9. Приватность и безопасность

Политика «все чаты, кроме исключений» означает, что весь входящий личный поток владельца проходит через сервер и (для классификации) через LLM-провайдера. Владелец это принимает осознанно, но техника должна минимизировать ущерб.

**Хранение**
- В MVP тела сообщений **не сохраняются вообще**. Текст живёт в памяти воркера секунды.
- В лог пишутся только: контакт, время, категория, действие, код шаблона.
- `morning_queue.summary` — одна строка-выжимка, не оригинал.
- На Этапе 4 хранение включается с TTL и шифрованием на уровне приложения (AES-GCM, ключ в переменной окружения, не в БД).

**Передача в LLM**
- Только текущее сообщение, без истории и без имени контакта.
- Отключён retention на стороне провайдера, где это доступно.

**Списки исключений до первого запуска**
- Все боты и сервисные аккаунты — жёстко в коде.
- Близкие, для кого автоответ будет выглядеть странно.
- Активные рабочие переписки, где ответ ждут в моменте.

**Инфраструктура**
- `secret_token` на webhook, проверка заголовка `X-Telegram-Bot-Api-Secret-Token`.
- Токен бота и API-ключ — в переменных окружения, не в репозитории.
- Postgres не смотрит наружу, доступ только из docker-сети.
- Ежедневный бэкап БД, но **без** таблиц с телами сообщений, когда те появятся.

**Этика формулировок**
Получатель не знает, что отвечает бот. Значит текст шаблона должен быть правдой: «сейчас нерабочее время» — правда. Имитация живого диалога — нет. Бот отвечает одним сообщением и замолкает, в переписку не вступает.

**Правовое**
Работа ботов регулируется Telegram Bot Developer ToS, в частности разделом 5.4 про Telegram Business. Для личного использования достаточно соблюдения; при выходе на сторонних пользователей понадобится публичная privacy policy.

---

## 10. Что осознанно НЕ делаем в MVP

| Не делаем | Почему |
|---|---|
| Диалог с контактом | одно сообщение и молчание — предсказуемо и безопасно |
| Память переписки | приватность + сложность |
| Группы | платформа не даёт |
| Инициацию переписки | платформа не даёт (окно 24h) |
| Голосовые и файлы | отдельный объём работы, ценность низкая |
| Дайджест | вынесен в Этап 4 по решению владельца |
| Действия (файл → Drive, email) | Этап 5, проектировать рано |

---

## 11. Стоимость эксплуатации

| Статья | В месяц |
|---|---|
| VPS | €5–10 |
| LLM (классификация, ~30 сообщений/сутки) | $3–8 |
| Telegram Premium | $0: продукт использует отдельный `Chat Automation`; полный профиль `Telegram Business` не требуется |
| **Итого** | **~$8–18** |

Классификатор вызывается только для сообщений, прошедших gate, — то есть в quiet-окне и не от исключённых контактов. Реальный объём вызовов заметно ниже общего потока.
