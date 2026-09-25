from __future__ import annotations

import asyncio
from datetime import UTC, datetime, time, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from secretary_bot import models
from secretary_bot.actions import LogAction
from secretary_bot.escalation import EscalationActions
from secretary_bot.ingest import IngestResult, RedisDeduplicator, UpdateIngestor
from secretary_bot.outbox import deliver_notifications
from secretary_bot.sender import BusinessReplySender
from secretary_bot.workers import deliver_due_once
from tests.test_escalation import NOW, FakeBot, callback, seed_request
from tests.test_ingest import FakeRedis, make_update
from tests.test_pipeline import NIGHT, message, scheduled, set_connection, world  # noqa: F401
from tests.test_web_api import headers, seed_owner, web_app


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["exclude", "schedule"])
async def test_rules_changed_during_delay_stop_delivery(world, change):  # noqa: F811
    pipeline, bot, _, db = world
    await set_connection(db, dry_run=False)
    await pipeline.process_incoming(message())
    task = (await scheduled(pipeline))[0]
    async with db.session() as session, session.begin():
        if change == "exclude":
            session.add(models.Exclusion(connection_id=1, contact_id=100))
        else:
            row = await session.scalar(select(models.Schedule))
            row.time_from, row.time_to = time(12), time(13)
    outcome = await pipeline.deliver(task, now=NIGHT + timedelta(seconds=30))
    assert outcome in {LogAction.SKIPPED_EXCLUDED, LogAction.SKIPPED_SCHEDULE}
    assert bot.sent == []


@pytest.mark.asyncio
async def test_database_failure_after_send_does_not_duplicate(world, monkeypatch):  # noqa: F811
    pipeline, bot, _, db = world
    await set_connection(db, dry_run=False)
    await pipeline.process_incoming(message())
    import secretary_bot.pipeline as module

    original = module.record_auto_reply
    calls = 0

    async def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database unavailable after accepted send")
        await original(*args, **kwargs)

    monkeypatch.setattr(module, "record_auto_reply", fail_once)
    await deliver_due_once(pipeline, pipeline.queue, now=NIGHT + timedelta(minutes=1))
    await deliver_due_once(pipeline, pipeline.queue, now=NIGHT + timedelta(minutes=2))
    assert len(bot.sent) == 1
    assert await pipeline.queue.pending() == 0


@pytest.mark.asyncio
async def test_same_message_number_in_different_chats_is_not_one_request(world):  # noqa: F811
    pipeline, _, _, db = world
    await pipeline.process_incoming(message(chat_id=100))
    await pipeline.process_incoming(message(chat_id=101))
    async with db.session() as session:
        rows = list(await session.scalars(select(models.ContactRequest)))
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_payment_notification_recovers_without_charging_twice(database):
    request_id = await seed_request(database)

    class FlakyOwner(FakeBot):
        fails = True

        async def send_message(self, **kwargs):
            if kwargs.get("chat_id") == 42 and self.fails:
                raise RuntimeError("owner unavailable")
            return await super().send_message(**kwargs)

    bot = FlakyOwner()
    sender = BusinessReplySender(bot=bot)
    actions = EscalationActions(database=database, bot=bot, sender=sender)
    await actions.handle_callback(callback("offer", request_id), now=NOW)
    await actions.handle_callback(callback("confirm", request_id), now=NOW)
    async with database.session() as session, session.begin():
        job = await session.get(models.NotificationJob, f"paid-owner:{request_id}")
        assert job.completed_at is None
        job.due_at = NOW
    bot.fails = False
    await deliver_notifications(database, sender)
    await actions.handle_callback(callback("confirm", request_id), now=NOW)
    async with database.session() as session:
        request = await session.get(models.ContactRequest, request_id)
        activity = await session.get(models.ContactActivity, (request.connection_id, 100))
        assert request.owner_notification_message_id is not None
        assert activity.paid_escalation_count == 1
    assert len([sent for sent in bot.sent if sent["chat_id"] == 42]) == 1


@pytest.mark.asyncio
async def test_webhook_failure_releases_claim_and_success_is_completed():
    redis = FakeRedis()
    dedup = RedisDeduplicator(redis, ttl_seconds=86400)
    calls = 0

    async def process(update):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("retry me")

    ingestor = UpdateIngestor(asyncio.Queue(), dedup, processor=process)
    with pytest.raises(RuntimeError):
        await ingestor.enqueue(make_update())
    assert await ingestor.enqueue(make_update()) is IngestResult.ACCEPTED
    assert await ingestor.enqueue(make_update()) is IngestResult.DUPLICATE
    assert calls == 2
    assert list(redis.values.values()) == ["done"]


@pytest.mark.asyncio
async def test_paused_and_stopped_dry_run_statuses_are_truthful(database):
    connection_id = await seed_owner(database)
    async with AsyncClient(
        transport=ASGITransport(app=web_app(database)), base_url="https://testserver"
    ) as client:
        response = await client.post("/api/v1/control", headers=headers(), json={"action": "pause"})
        assert response.status_code == 200
        assert response.json()["status"]["code"] == "paused"
        response = await client.post("/api/v1/control", headers=headers(), json={"action": "stop"})
        assert response.json()["status"]["code"] == "stopped"
        response = await client.post("/api/v1/control", headers=headers(), json={"action": "live"})
        assert response.status_code == 422
        response = await client.post(
            "/api/v1/control", headers=headers(), json={"action": "dry_run"}
        )
        assert response.json()["connection"]["dry_run"] is True
    assert connection_id


@pytest.mark.asyncio
async def test_expired_exclusions_are_not_shown_as_active(database):
    connection_id = await seed_owner(database)
    async with database.session() as session, session.begin():
        session.add(models.ContactActivity(connection_id=connection_id, contact_id=100))
        session.add(
            models.Exclusion(
                connection_id=connection_id,
                contact_id=100,
                until=datetime.now(UTC) - timedelta(hours=1),
            )
        )
    async with AsyncClient(
        transport=ASGITransport(app=web_app(database)), base_url="https://testserver"
    ) as client:
        result = await client.get("/api/v1/contacts", headers=headers())
        assert result.json()["items"][0]["exclusion"] == "none"
        result = await client.put(
            "/api/v1/contacts/100",
            headers=headers(),
            json={"exclusion": "until", "exclusion_until": "2020-01-01T00:00:00Z", "windows": []},
        )
        assert result.status_code == 422


@pytest.mark.asyncio
async def test_pdf_link_is_scoped_to_month_and_never_creates_panel_session(database):
    await seed_owner(database)
    async with AsyncClient(
        transport=ASGITransport(app=web_app(database)), base_url="https://testserver"
    ) as client:
        link = (
            await client.post("/api/v1/analytics/monthly-link?month=2026-09", headers=headers())
        ).json()["url"]
        assert (await client.get(link.replace("2026-09", "2026-08"))).status_code == 401
        pdf = await client.get(link)
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
        assert "set-cookie" not in pdf.headers
        assert (await client.get("/api/v1/bootstrap")).status_code == 401
        assert (await client.get(link)).status_code == 401


@pytest.mark.asyncio
async def test_contact_search_happens_before_limit_and_uses_batched_queries(database):
    from sqlalchemy import event

    owner = await seed_owner(database)
    async with database.session() as session, session.begin():
        session.add_all(
            [
                models.ContactActivity(
                    connection_id=owner,
                    contact_id=1000 + i,
                    contact_name=f"Contact {i}",
                    last_incoming_at=datetime.now(UTC),
                )
                for i in range(501)
            ]
        )
        session.add(
            models.ContactActivity(
                connection_id=owner, contact_id=100, contact_name="Old needle", last_incoming_at=NOW
            )
        )
    queries = []

    def count_sql(*args):
        queries.append(args[2])

    event.listen(database.engine.sync_engine, "before_cursor_execute", count_sql)
    async with AsyncClient(
        transport=ASGITransport(app=web_app(database)), base_url="https://testserver"
    ) as client:
        found = await client.get("/api/v1/contacts?search=needle", headers=headers())
        assert found.json()["items"][0]["contact_id"] == 100
        queries.clear()
        page = (await client.get("/api/v1/contacts", headers=headers())).json()
        assert len(page["items"]) == 100 and page["has_more"]
        assert len(queries) < 12
        next_page = (await client.get("/api/v1/contacts?offset=100", headers=headers())).json()
        assert not (
            {r["contact_id"] for r in page["items"]} & {r["contact_id"] for r in next_page["items"]}
        )
    event.remove(database.engine.sync_engine, "before_cursor_execute", count_sql)


@pytest.mark.asyncio
async def test_manual_send_reuses_receipt_after_database_failure(database, monkeypatch):
    import secretary_bot.summary_actions as module
    from tests import test_summary_actions as helper

    await helper.seed_summary(database)
    bot = helper.FakeBot()
    handler = helper.actions(database, bot)
    await handler.handle_callback(helper.callback("direct:select:100"), now=helper.NOW)
    prompt = bot.sent[-1]
    async with database.session() as session:
        state = await session.scalar(select(models.DirectReplyState))
        prompt_id = state.prompt_message_id
    original = module.log_decision
    calls = 0

    async def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database write failed")
        return await original(*args, **kwargs)

    monkeypatch.setattr(module, "log_decision", fail_once)
    message = helper.reply_message("Hello", reply_to_message_id=prompt_id)
    with pytest.raises(RuntimeError):
        await handler.handle_message(message, now=helper.NOW)
    assert await handler.handle_message(message, now=helper.NOW)
    assert len([sent for sent in bot.sent if sent["chat_id"] == 100]) == 1
    async with database.session() as session:
        assert await session.scalar(select(models.DirectReplyState)) is None
    assert prompt


# --- Follow-up fixes on the reliability work itself ---------------------------


async def _seed_connection(database) -> int:
    async with database.session() as session, session.begin():
        row = models.Connection(business_connection_id="receipt-connection", owner_user_id=42)
        session.add(row)
        await session.flush()
        return row.id


@pytest.mark.asyncio
async def test_uncertain_send_is_retried_only_with_an_explicit_cooldown(database):
    from aiogram.exceptions import TelegramNetworkError

    from secretary_bot.delivery import send_once
    from tests.test_sender import METHOD
    from tests.test_sender import FakeBot as SenderBot

    connection_id = await _seed_connection(database)
    bot = SenderBot(TelegramNetworkError(method=METHOD, message="timeout"))
    sender = BusinessReplySender(bot=bot)
    kwargs = dict(business_connection_id=None, chat_id=42, text="звіт")

    first = await send_once(database, sender, key="k", connection_id=connection_id, **kwargs)
    assert not first.is_sent and first.error_code == "DELIVERY_UNCERTAIN"
    # Contacts: never resend automatically.
    again = await send_once(database, sender, key="k", connection_id=connection_id, **kwargs)
    assert not again.is_sent and len(bot.calls) == 1
    # Owner-facing: resend only after the cooldown has passed.
    early = await send_once(
        database,
        sender,
        key="k",
        connection_id=connection_id,
        retry_uncertain_after=timedelta(minutes=10),
        **kwargs,
    )
    assert not early.is_sent and len(bot.calls) == 1
    async with database.session() as session, session.begin():
        receipt = await session.get(models.DeliveryReceipt, "k")
        receipt.updated_at = datetime.now(UTC) - timedelta(minutes=11)
    late = await send_once(
        database,
        sender,
        key="k",
        connection_id=connection_id,
        retry_uncertain_after=timedelta(minutes=10),
        **kwargs,
    )
    assert late.is_sent and len(bot.calls) == 2


@pytest.mark.asyncio
async def test_stuck_summary_run_is_abandoned_and_newer_periods_continue(database):
    from secretary_bot.classifier import ClassifierSettings
    from secretary_bot.daily_summary import ABANDONED, DailySummary
    from secretary_bot.retention import MessageCipher
    from secretary_bot.storage import ConnectionSnapshot, upsert_connection
    from tests.test_daily_summary import NOW as SUMMARY_NOW
    from tests.test_daily_summary import SCHEDULED, FakeBot, FakeModel, seed_message

    cipher = MessageCipher.from_encoded_key(MessageCipher.generate_encoded_key())
    async with database.session() as session, session.begin():
        connection = await upsert_connection(
            session,
            ConnectionSnapshot(
                business_connection_id="stuck-summary",
                owner_user_id=42,
                owner_chat_id=42,
                rights={"can_reply": True},
            ),
        )
        row = await session.get(models.Connection, connection.id)
        row.message_retention_enabled = True
        row.summary_time = time(12, 0)
        stuck_end = SCHEDULED - timedelta(days=4)
        session.add(
            models.SummaryRun(
                connection_id=connection.id,
                period_start=stuck_end - timedelta(hours=24),
                period_end=stuck_end,
                status="error",
                error_code="DELIVERY_UNCERTAIN",
                destination_chat_id=42,
            )
        )
        session.add(
            models.ContactActivity(
                connection_id=connection.id, contact_id=100, configured_at=stuck_end
            )
        )
        await seed_message(
            session,
            cipher,
            connection_id=connection.id,
            contact_id=100,
            message_id=10,
            direction="in",
            text="Коли буде рахунок?",
            occurred_at=SCHEDULED - timedelta(hours=1),
        )

    bot = FakeBot()
    digest = DailySummary(
        database=database,
        bot=bot,
        cipher=cipher,
        model=FakeModel(),
        classifier_defaults=ClassifierSettings(),
    )
    # Tick 1: the stale failing run is abandoned with a visible note, nothing else.
    assert await digest.run_once(now=SUMMARY_NOW) == 0
    async with database.session() as session:
        stuck = await session.scalar(
            select(models.SummaryRun).where(models.SummaryRun.status == "error")
        )
    assert stuck.error_code == ABANDONED
    assert "не вдалося надіслати" in bot.sent[-1]["text"]
    # Tick 2: the gap since the abandoned run becomes one incomplete catch-up report.
    assert await digest.run_once(now=SUMMARY_NOW + timedelta(minutes=1)) == 1
    assert "Підсумок неповний" in bot.sent[-1]["text"]
    # Tick 3: the current day is delivered as usual.
    assert await digest.run_once(now=SUMMARY_NOW + timedelta(minutes=2)) == 1
    assert "Добовий підсумок" in bot.sent[-2]["text"]
    async with database.session() as session:
        current = await session.scalar(
            select(models.SummaryRun).where(models.SummaryRun.period_end == SCHEDULED)
        )
    assert current is not None and current.status == "delivered"


@pytest.mark.asyncio
async def test_reply_restored_hours_late_is_refused(world):  # noqa: F811
    from secretary_bot.pipeline import STALE_REPLY

    pipeline, bot, _, db = world
    await set_connection(db, dry_run=False)
    await pipeline.process_incoming(message())
    (task,) = await scheduled(pipeline)

    outcome = await pipeline.deliver(task, now=NIGHT + timedelta(hours=3))

    assert outcome is LogAction.ERROR
    assert bot.sent == []
    async with db.session() as session:
        row = await session.scalar(
            select(models.MessageLog).where(models.MessageLog.action == LogAction.ERROR.value)
        )
    assert row is not None and row.error_code == STALE_REPLY


@pytest.mark.asyncio
async def test_reply_due_seconds_after_the_window_closes_is_still_sent(world):  # noqa: F811
    pipeline, bot, _, db = world
    await set_connection(db, dry_run=False)
    # 07:59:30 in Kyiv, thirty seconds before the 22:00–08:00 window closes.
    edge = datetime(2026, 8, 24, 4, 59, 30, tzinfo=UTC)
    await pipeline.process_incoming(message(received_at=edge))
    (task,) = await pipeline.queue.pop_due(now=edge + timedelta(minutes=2))

    outcome = await pipeline.deliver(task, now=edge + timedelta(seconds=60))

    assert outcome is LogAction.REPLIED
    assert len(bot.sent) == 1


@pytest.mark.asyncio
async def test_pause_keeps_the_scheduled_reply_and_logs_the_refusal(world):  # noqa: F811
    from secretary_bot.storage import set_connection_control

    pipeline, bot, _, db = world
    await set_connection(db, dry_run=False)
    await pipeline.process_incoming(message())
    (task,) = await scheduled(pipeline)
    async with db.session() as session, session.begin():
        await set_connection_control(
            session, task.connection_id, kill_switch=False, muted_until=NIGHT + timedelta(hours=1)
        )
        job = await session.scalar(select(models.ReplyJob))
        assert job.completed_at is None

    outcome = await pipeline.deliver(task, now=NIGHT + timedelta(seconds=30))

    assert outcome is LogAction.SKIPPED_KILL_SWITCH
    assert bot.sent == []
    async with db.session() as session:
        actions = list(await session.scalars(select(models.MessageLog.action)))
    assert LogAction.SKIPPED_KILL_SWITCH.value in actions


@pytest.mark.asyncio
async def test_notification_job_gives_up_after_the_attempt_limit(database):
    from secretary_bot.outbox import MAX_NOTIFICATION_ATTEMPTS
    from tests.test_pipeline import FakeBot as PipelineBot

    connection_id = await _seed_connection(database)
    async with database.session() as session, session.begin():
        session.add(
            models.NotificationJob(
                key="paid-owner:999",
                connection_id=connection_id,
                due_at=datetime.now(UTC) - timedelta(seconds=1),
                attempts=MAX_NOTIFICATION_ATTEMPTS - 1,
                payload={"business_connection_id": None, "chat_id": 42, "text": "x"},
            )
        )
    bot = PipelineBot(error=RuntimeError("owner chat unavailable"))

    await deliver_notifications(database, BusinessReplySender(bot=bot))

    async with database.session() as session:
        job = await session.get(models.NotificationJob, "paid-owner:999")
    assert job.attempts == MAX_NOTIFICATION_ATTEMPTS
    assert job.completed_at is not None and job.error_code == "RuntimeError"


# --- Review of the uncommitted work (docs/uncommitted-review-2026-09-05.md) ----


@pytest.mark.asyncio
async def test_reconciliation_republishes_lost_jobs_without_duplicating_leases(world):  # noqa: F811
    from dataclasses import replace

    from secretary_bot.workers import reconcile_reply_jobs

    pipeline, _, _, _ = world
    await pipeline.process_incoming(message())
    (task,) = await pipeline.queue.snapshot()

    # R1: Redis lost the member while the process kept running.
    pipeline.queue.client.scores.clear()
    assert await reconcile_reply_jobs(pipeline, pipeline.queue) == 1
    assert await pipeline.queue.pending() == 1

    # A leased retry member already represents the job: no attempt-0 duplicate.
    pipeline.queue.client.scores.clear()
    await pipeline.queue.schedule(replace(task, delivery_attempts=1), due_at=NIGHT)
    assert await reconcile_reply_jobs(pipeline, pipeline.queue) == 0
    assert await pipeline.queue.pending() == 1


@pytest.mark.asyncio
async def test_abandoned_notification_is_visible_and_retryable(database):
    from secretary_bot.outbox import MAX_NOTIFICATION_ATTEMPTS, NOTIFICATION_FAILED
    from tests.test_pipeline import FakeBot as PipelineBot

    connection_id = await seed_owner(database)
    async with database.session() as session, session.begin():
        request = models.ContactRequest(
            connection_id=connection_id,
            contact_id=100,
            tg_message_id=7,
            occurred_at=NOW,
            offer_expires_at=NOW + timedelta(hours=24),
            status="paid",
        )
        session.add(request)
        await session.flush()
        session.add(
            models.NotificationJob(
                key=f"paid-owner:{request.id}",
                connection_id=connection_id,
                due_at=datetime.now(UTC) - timedelta(seconds=1),
                attempts=MAX_NOTIFICATION_ATTEMPTS - 1,
                payload={"business_connection_id": None, "chat_id": 42, "text": "x"},
            )
        )
    failing = BusinessReplySender(bot=PipelineBot(error=RuntimeError("owner chat unavailable")))
    await deliver_notifications(database, failing)

    async with AsyncClient(
        transport=ASGITransport(app=web_app(database)), base_url="https://testserver"
    ) as client:
        status = (await client.get("/api/v1/bootstrap", headers=headers())).json()["status"]
        # R2: the final failure is counted and surfaces as the last error.
        assert status["failed_notifications"] == 1 and status["pending_notifications"] == 0
        assert status["last_error"] == NOTIFICATION_FAILED
        retried = (await client.post("/api/v1/notifications/retry", headers=headers())).json()
        assert retried["status"]["failed_notifications"] == 0
        assert retried["status"]["pending_notifications"] == 1
    async with database.session() as session:
        job = await session.scalar(select(models.NotificationJob))
        stored = await session.scalar(select(models.ContactRequest))
    assert job.completed_at is None and job.attempts == 0
    assert stored.status == "paid"  # retrying delivery never re-charges


@pytest.mark.asyncio
async def test_preview_follows_reply_rights_and_forced_template(database):
    from secretary_bot.storage import set_contact_template_override
    from secretary_bot.templates import DEFAULT_TEMPLATES, TemplateCode

    connection_id = await seed_owner(database)
    async with AsyncClient(
        transport=ASGITransport(app=web_app(database)), base_url="https://testserver"
    ) as client:
        async with database.session() as session, session.begin():
            row = await session.get(models.Connection, connection_id)
            row.rights_json = {}
        denied = await client.post("/api/v1/preview", headers=headers(), json={"text": "Привіт"})
        assert denied.json()["decision"] == "skipped_inactive"  # R3a

        async with database.session() as session, session.begin():
            row = await session.get(models.Connection, connection_id)
            row.rights_json = {"can_reply": True}
            await set_contact_template_override(
                session,
                connection_id,
                100,
                template_code=TemplateCode.MONEY_PRIORITY.value,
                template_text=DEFAULT_TEMPLATES[TemplateCode.MONEY_PRIORITY],
            )
        forced = (
            await client.post(
                "/api/v1/preview", headers=headers(), json={"text": "Привіт", "contact_id": 100}
            )
        ).json()
    assert forced["category"] == "general" and forced["forced_template"] is True  # R3b
    assert forced["template_code"] == "money_priority"
    assert DEFAULT_TEMPLATES[TemplateCode.MONEY_PRIORITY] in forced["text"]


@pytest.mark.asyncio
async def test_summary_reply_prompt_warns_about_the_real_send(database):
    from tests import test_summary_actions as helper

    _, item_id, _ = await helper.seed_summary(database)
    bot = helper.FakeBot()
    handler = helper.actions(database, bot)

    query = helper.callback(f"summary:reply:{item_id}")
    assert await handler.handle_callback(query, now=helper.NOW)

    prompt = next(sent for sent in bot.sent if sent["chat_id"] == 42)
    assert "навіть у тестовому режимі" in prompt["text"]  # R6
