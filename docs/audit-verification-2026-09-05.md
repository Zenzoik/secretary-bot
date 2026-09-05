# Проверка аудита от 05.09.2026

Проверен HEAD `2148b49`, тот же, что и в [`audit-2026-09-05.md`](audit-2026-09-05.md).
Метод: чтение всех модулей `src/secretary_bot`, Mini App, планов и stage-документов;
повторный прогон `ruff` и `pytest --cov`; пять воспроизведений на фикстурах проекта
(SQLite + fake Telegram, код в приложении A); проверка сдвига даты в Node.
Живой Telegram, production PostgreSQL/Redis и браузерный рендеринг не проверялись.

Вывод: аудит точный. Все семь пунктов приоритета 1 подтверждены, пять из них
воспроизведены. Приоритет 2 подтверждён по коду, кроме продуктовых предложений.
Ссылки на строки, число тестов и покрытие совпадают. Дополнительно найдено восемь
пропущенных проблем, две из них существенные (раздел «Что аудит пропустил»).

## Проверки

| Проверка | Аудит | Повтор 05.09 |
|---|---|---|
| `ruff check .` | чисто | чисто |
| `pytest` | 301 passed | 301 passed |
| покрытие строк | 86 % | 86 %, помодульно совпадает |
| escalation / web_api / workers | 69 / 62 / 68 % | 69 / 62 / 68 % |

## Приоритет 1

### 1. Исключение не останавливает запланированный ответ — подтверждено, воспроизведено

`_blocked` в `pipeline.py:489` проверяет только `is_active`, `can_reply`,
`kill_switch` и `muted_until`. Воспроизведено на фикстуре `world`: входящее →
постановка → `set_contact_exclusion(until=None)` → `deliver` возвращает `replied`,
fake Telegram получил 1 сообщение.

Дополнительно воспроизведён второй вариант: окно расписания изменено так, что оно
больше не покрывает момент доставки. Результат тот же — `replied`, 1 отправка.

Оговорка аудита о лимите верна: `claim_window` инкрементирует счётчик при постановке
в очередь, поэтому повторный полный `evaluate_gate` при доставке отклонил бы
собственную задачу.

### 2. Платное обращение без уведомления владельца — подтверждено, воспроизведено

`escalation.py:175`. Статус `paid`, `paid_at` и инкремент `paid_escalation_count`
коммитятся в первой транзакции. Затем без обработки ошибок идут отправка контакту
и `bot.send_message` владельцу. Воспроизведено: исключение на отправке владельцу →
`status=paid`, `owner_notification_message_id=None`, `paid_count=1`; повторный
confirm отвечает «Платне звернення вже підтверджено», попыток уведомления
по-прежнему 1.

Попутно: результат `sender.send` контакту на `escalation.py:239` игнорируется.
При сбое контакт не узнаёт, что обращение стало платным, а в счёт оно попадёт.

### 3. Потеря принятых входящих — подтверждено по коду

`ingest.py:83` возвращает `ACCEPTED` после `put_nowait` в `asyncio.Queue`; webhook
отдаёт 200. `runtime.py:76` логирует исключение обработчика и вызывает `task_done`.

Хуже, чем описано: штатная остановка тоже теряет очередь. `application.py:151`
отменяет `telegram-update-worker` без `queue.join()` или drain. Telegram уже получил
200 и повторно не доставит; dedup-ключ живёт в Redis 24 часа.

### 4. Очередь отложенных ответов — подтверждено, дубль воспроизведён

`delayed.py:92`: `ZREM` до доставки, SIGKILL внутри `deliver_due_once` теряет
захваченный пакет. Возврат при `CancelledError` (`workers.py:36`) защищает только
штатную остановку.

Обратный случай воспроизведён: `record_auto_reply` подменён на падающий при первом
вызове. Telegram принял сообщение, транзакция упала, worker вернул задачу в Redis,
вторая доставка прошла. Контакт получил 2 сообщения на одно входящее.

### 5. Сдвиг временного исключения — подтверждено в Node

`app.js:322` кладёт UTC в `datetime-local`, `app.js:482` читает как локальное время.

| TZ | сохранено | поле | после пересохранения |
|---|---|---|---|
| Europe/Prague | 18:00Z | 18:00 | 16:00Z |
| Europe/Kyiv | 18:00Z | 18:00 | 15:00Z |

### 6. Статус Mini App не учитывает паузу — подтверждено

`web_api.py:685` не отдаёт `muted_until`; `app.js:118` его не использует.

Дополнение: при `kill_switch=true` и `dry_run=true` обзор показывает «Лише чернетки»
и «Клієнти не отримують відповіді», хотя бот не делает вообще ничего. Формула
`live ? … : dry_run ? "Лише чернетки" : "Зупинено"` проверяет kill switch только
для live-режима.

### 7. «Виключити» не исключает — подтверждено

`texts.py:114`, `runtime.py:165`, `storage.py:1162`: пишется только
`ShadowFeedback(verdict='exclude')`. Уточнение: README-USER §5 прямо документирует
«Последняя кнопка в dry-run карточке только сохраняет оценку». Это осознанный
дизайн разметки, но название кнопки обещает действие.

## Приоритет 2

| № | Вердикт | Источник |
|---|---|---|
| 1 | продуктовое предложение | — |
| 2 | подтверждено | `app.js:311` — `selectContact` перезаписывает форму, dirty-state нет нигде |
| 3 | подтверждено | `app.js:559` — любое исключение bootstrap показывает `#auth-state` |
| 4 | подтверждено | `app.js:158` показывает общий максимум; `pipeline.py:188` режет до 60. Карточка обзора на `app.js:125` считает правильно |
| 5 | подтверждено | `web_api.py:815` — limit 500, затем фильтр в Python; `web_api.py:956` — limit 200 без пагинации |
| 6 | подтверждено | `web_api.py:851` — `replied + dry_run` за 30 дней; счётчики обращений из `ContactActivity` накопительные |
| 7 | подтверждено | `styles.css:172` — 1050 px; `styles.css:178` — 720 px |
| 8 | продуктовое предложение | — |
| 9 | сильнее, чем написано | см. ниже |
| 10 | продуктовое предложение, согласовано с plan-v2 | — |

Пункт 9. `styles.css:85` задаёт полям ввода фиксированный `background: #101925`,
а `color: var(--text)`, где `--text` берётся из `--tg-theme-text-color`. В светлой
теме Telegram это тёмный текст на тёмном поле. Визуально не проверено, но по CSS
это дефект, а не риск. То же с `#111b28` у карточек контактов, окон и таблиц.

## Саммари и соответствие планам

- **Окно 30 минут без догона** — подтверждено, воспроизведено на `summary_period`:
  09:29 → период есть, 09:31 → `None`.
- **FR-18** — расхождение есть, но аудит не заметил, что сокращение объёма уже
  зафиксировано в [`stage-3-delivery.md`](stage-3-delivery.md): «добавление новых
  кодов до пяти требует отдельного изменения ядра… произвольные новые коды не
  создаются». Не обновлён только plan-v2.
- **FR-22** — аналогично, [`stage-4-summary.md`](stage-4-summary.md) описывает
  «отправляется только reply на этот prompt» без шага подтверждения. plan-v2 не
  обновлён.
- **Незакрытые чек-листы** — подтверждено: в `plan.md` 26 незакрытых пунктов,
  включая DoD этапов 3 и 4, которые в plan-v2 закрыты; в `stage-1.6-onboarding.md`
  семь незакрытых пунктов живого теста; в plan-v2 один.

## Что аудит пропустил

### 1. `morning_queue` растёт без ограничения, FR-10 молча не работает

`pipeline.py:333` пишет строку на каждое денежное сообщение, включая dry-run.
Единственный потребитель — `daily_summary.py`, который на строке 57 пропускает
подключения с `message_retention_enabled=false`. `run_morning_digest` в
`application.py` не запускается. Удаления строк `MorningQueue` нет нигде.
Хранение по умолчанию выключено, значит у таких владельцев утренний список из
plan.md FR-10 не приходит вообще, а таблица растёт бессрочно.

### 2. Нет пути обратно в dry-run

`dry_run=True` пишут только `complete_onboarding` и `revoke_access_user`.
README-USER §5 это документирует («возврат dry_run=true выполняет администратор»).
Для продукта с принципом «при неопределённости молчать» отсутствие
владельческого отката серьёзнее, чем выглядит в документации.

### 3. Кнопки live нет в клавиатуре, документация её описывает

`control.py:931` рендерит статус, сегодня, питание, паузу и отправку от бота.
`README.md` (таблица кнопок) и README-USER §5 описывают кнопку
«⚠️ Увімкнути live». Работает только команда `/live` или ввод текста кнопки
вручную. Удаление осознанное по stage-3-design, документы не обновлены.

### 4. Одноразовая ссылка на PDF создаёт 30-дневную браузерную сессию

`web_api.py:638`: `/web/analytics/{token}/{month}` ставит ту же
`secretary_session` cookie на 30 дней, что и вход в браузер. Открытие PDF из
Telegram на чужом или общем устройстве оставляет там полный доступ к панели.

### 5. LLM-вызов внутри открытой транзакции БД

`pipeline.py:81–165`: `session.begin()` держится всё время `classify`, до 8 секунд
по таймауту. Docstring `deliver` формулирует обратное правило для Telegram. Пул
соединений общий с web API и workers.

### 6. N+1 в списке контактов

`web_api.py:830`: `_contact` вызывается для каждого из до 500 контактов, по
четыре запроса на каждый.

### 7. Ручной ответ от бота теряет FSM при сбое БД

`summary_actions.py:160`: удаление `DirectReplyState`/`SummaryReplyState` и запись
журнала идут после `sender.send` в отдельной транзакции. При сбое сообщение уже
ушло, владелец не получает «✅ Відповідь надіслано», состояние остаётся, повторный
reply на тот же prompt отправит ещё раз.

### 8. Один worker обновлений

`process_updates` обрабатывает все апдейты последовательно, включая классификацию.
Пропускная способность — одно сообщение за латентность LLM; при переполнении
очереди на 1000 webhook отвечает 503. Для одного владельца приемлемо, для
нескольких ночью нет.

## Расхождения документации

- README-USER §2, шаг 9: «подождите до четырёх минут» — из старого FR-8 (60–240 с),
  сейчас задержка до 60 с.
- README-USER §4–5 называет кнопки по-русски («⛔ Выключить», «🗓 Сегодня»,
  «❌ Не надо было»), в боте они украинские.
- stage-3-delivery: «до восьми окон» при `MAX_WINDOWS = 16`; «поиск по имени или
  Telegram ID», хотя `_contacts` ищет по имени и username.
- `ContactPayload` принимает `exclusion_until` в прошлом; UI показывает исключение
  как активное, gate его не применяет.

## Приложение A. Воспроизведение

Запуск из корня репозитория:

```bash
.venv/bin/python -m pytest -s -p no:cacheprovider -o addopts="" path/to/test_audit_repro.py
```

Все пять тестов проходят, то есть баги воспроизводятся. Тесты используют фикстуры
`tests/test_pipeline.py` и `tests/test_escalation.py`.

```python
"""Reproductions for audit-2026-09-05 findings, on the project's own fixtures."""
from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from secretary_bot import models
from secretary_bot.actions import LogAction
from secretary_bot.daily_summary import summary_period
from secretary_bot.escalation import EscalationActions
from secretary_bot.sender import BusinessReplySender
from secretary_bot.storage import set_contact_exclusion
from secretary_bot.workers import deliver_due_once
from tests.conftest import database  # noqa: F401
from tests.test_escalation import FakeBot as EscalationFakeBot
from tests.test_escalation import callback, seed_request
from tests.test_pipeline import NIGHT, message, scheduled, set_connection, world  # noqa: F401


@pytest.mark.asyncio
async def test_f1_exclusion_added_during_delay_does_not_stop_reply(world) -> None:
    pipeline, bot, _, db = world
    await set_connection(db, dry_run=False)
    await pipeline.process_incoming(message())
    (task,) = await scheduled(pipeline)
    async with db.session() as session, session.begin():
        await set_contact_exclusion(
            session, task.connection_id, task.contact_id, until=None, reason="repro"
        )
    result = await pipeline.deliver(task, now=NIGHT + timedelta(seconds=30))
    assert result is LogAction.REPLIED and len(bot.sent) == 1


@pytest.mark.asyncio
async def test_f1_schedule_removed_during_delay_does_not_stop_reply(world) -> None:
    pipeline, bot, _, db = world
    await set_connection(db, dry_run=False)
    await pipeline.process_incoming(message())
    (task,) = await scheduled(pipeline)
    async with db.session() as session, session.begin():
        row = await session.scalar(select(models.Schedule))
        row.time_from, row.time_to = time(12, 0), time(13, 0)
    result = await pipeline.deliver(task, now=NIGHT + timedelta(seconds=30))
    assert result is LogAction.REPLIED and len(bot.sent) == 1


class OwnerNotificationFails(EscalationFakeBot):
    async def send_message(self, **kwargs: Any) -> Any:
        if kwargs.get("chat_id") == 42:
            self.sent.append(kwargs)
            raise RuntimeError("telegram down for owner chat")
        return await super().send_message(**kwargs)


@pytest.mark.asyncio
async def test_f2_paid_status_persists_but_owner_never_notified(database) -> None:  # noqa: F811
    request_id = await seed_request(database)
    bot = OwnerNotificationFails()
    actions = EscalationActions(database=database, bot=bot, sender=BusinessReplySender(bot=bot))
    now = datetime(2026, 9, 4, 18, tzinfo=UTC)
    assert await actions.handle_callback(callback("offer", request_id), now=now)
    with pytest.raises(RuntimeError):
        await actions.handle_callback(callback("confirm", request_id), now=now)
    owner_attempts = sum(1 for s in bot.sent if s.get("chat_id") == 42)
    await actions.handle_callback(callback("confirm", request_id), now=now)
    owner_attempts_after_retry = sum(1 for s in bot.sent if s.get("chat_id") == 42)
    async with database.session() as session:
        row = await session.get(models.ContactRequest, request_id)
    assert row.status == "paid" and row.owner_notification_message_id is None
    assert owner_attempts == owner_attempts_after_retry == 1


@pytest.mark.asyncio
async def test_f4_db_failure_after_send_causes_duplicate(world, monkeypatch) -> None:
    pipeline, bot, _, db = world
    await set_connection(db, dry_run=False)
    await pipeline.process_incoming(message())
    import secretary_bot.pipeline as pipeline_module

    calls = {"n": 0}
    original = pipeline_module.record_auto_reply

    async def flaky(*args: Any, **kwargs: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("postgres hiccup after telegram accepted the message")
        await original(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "record_auto_reply", flaky)
    t = NIGHT + timedelta(minutes=10)
    await deliver_due_once(pipeline, pipeline.queue, now=t)
    await deliver_due_once(pipeline, pipeline.queue, now=t + timedelta(seconds=6))
    assert len(bot.sent) == 2


def test_summary_window_is_not_caught_up() -> None:
    from secretary_bot.gate import ConnectionPolicy
    from secretary_bot.storage import ConnectionRecord

    rec = ConnectionRecord(
        id=1, business_connection_id="c", owner_user_id=1, owner_chat_id=1, rights={},
        dry_run=True, sender_identity="bot", delay_min_seconds=10, delay_max_seconds=60,
        bot_delay_seconds=5, max_auto_replies_per_window=None, escalation_enabled=False,
        escalation_price_amount=0, escalation_currency="UAH", escalation_offer_text="",
        escalation_confirm_text="", escalation_decline_text="", mark_read=False,
        summary_time=time(9, 0), summary_channel_id=None, message_retention_enabled=True,
        control_state="main", policy=ConnectionPolicy(timezone="UTC"),
    )
    assert summary_period(rec, now=datetime(2026, 9, 5, 9, 29, tzinfo=UTC)) is not None
    assert summary_period(rec, now=datetime(2026, 9, 5, 9, 31, tzinfo=UTC)) is None
```
