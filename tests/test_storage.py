from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretary_bot import models
from secretary_bot.actions import LogAction
from secretary_bot.classifier import ClassifierSettings
from secretary_bot.gate import GateDecision, evaluate_gate
from secretary_bot.storage import (
    ConnectionSnapshot,
    approve_access_user,
    claim_window,
    consume_access_invite,
    create_access_invite,
    daily_action_counts,
    enqueue_morning,
    ensure_master,
    feedback_belongs_to_owner,
    load_access_user,
    load_classifier_settings,
    load_connection,
    load_contact_state,
    load_forced_template_code,
    load_templates,
    log_decision,
    owner_replied_since,
    record_auto_reply,
    record_feedback,
    record_incoming,
    record_owner_reply,
    revoke_access_user,
    set_contact_exclusion,
    set_contact_template_override,
    set_control_state,
    upsert_connection,
)

NOW = datetime(2026, 8, 24, 0, 14, tzinfo=UTC)

SNAPSHOT = ConnectionSnapshot(
    business_connection_id="connection-1",
    owner_user_id=42,
    owner_chat_id=42,
    owner_username="owner",
    rights={"can_reply": True},
)


async def stored_connection(session: AsyncSession) -> int:
    record = await upsert_connection(session, SNAPSHOT)
    return record.id


@pytest.mark.asyncio
async def test_master_bootstrap_is_unique_and_environment_owned(session: AsyncSession) -> None:
    master = await ensure_master(session, 42, username="owner")

    assert master.is_master
    assert master.can_process
    with pytest.raises(RuntimeError, match="different master"):
        await ensure_master(session, 99)


@pytest.mark.asyncio
async def test_invite_is_one_time_pending_until_master_approval(session: AsyncSession) -> None:
    await ensure_master(session, 42)
    token = await create_access_invite(session, created_by=42, now=NOW, ttl=timedelta(hours=24))
    invite = await session.scalar(select(models.AccessInvite))
    assert invite is not None
    assert invite.token_hash != token.encode()
    assert len(invite.token_hash) == 32

    pending = await consume_access_invite(
        session, token=token, user_id=99, username="customer", now=NOW
    )

    assert pending is not None
    assert pending.status == "pending"
    assert pending.onboarding_state == "awaiting_connection"
    assert not pending.can_connect
    assert (
        await consume_access_invite(session, token=token, user_id=100, username="other", now=NOW)
        is None
    )

    assert await approve_access_user(session, user_id=99, approved_by=42, now=NOW)
    approved = await load_access_user(session, 99)
    assert approved is not None and approved.can_connect and not approved.can_process


@pytest.mark.asyncio
async def test_expired_invite_and_non_master_mutations_are_rejected(
    session: AsyncSession,
) -> None:
    await ensure_master(session, 42)
    token = await create_access_invite(session, created_by=42, now=NOW, ttl=timedelta(minutes=1))

    assert (
        await consume_access_invite(
            session,
            token=token,
            user_id=99,
            username=None,
            now=NOW + timedelta(minutes=1),
        )
        is None
    )
    with pytest.raises(PermissionError):
        await create_access_invite(session, created_by=99, now=NOW, ttl=timedelta(hours=1))


@pytest.mark.asyncio
async def test_master_can_revoke_but_not_demote_itself(session: AsyncSession) -> None:
    await ensure_master(session, 42)
    token = await create_access_invite(session, created_by=42, now=NOW, ttl=timedelta(hours=1))
    await consume_access_invite(session, token=token, user_id=99, username=None, now=NOW)
    await approve_access_user(session, user_id=99, approved_by=42, now=NOW)

    connection = await upsert_connection(
        session, ConnectionSnapshot("customer-connection", owner_user_id=99)
    )
    await session.execute(
        models.Connection.__table__.update()
        .where(models.Connection.id == connection.id)
        .values(dry_run=False, kill_switch=False, live_confirmation_until=NOW + timedelta(hours=1))
    )

    assert await revoke_access_user(session, user_id=99, revoked_by=42, now=NOW)
    revoked = await load_access_user(session, 99)
    assert revoked is not None and revoked.status == "revoked"
    connection_row = await session.get(models.Connection, connection.id)
    assert connection_row is not None
    assert connection_row.dry_run is True
    assert connection_row.kill_switch is True
    assert connection_row.live_confirmation_until is None
    assert not await revoke_access_user(session, user_id=42, revoked_by=42, now=NOW)

    new_token = await create_access_invite(session, created_by=42, now=NOW, ttl=timedelta(hours=1))
    reinvited = await consume_access_invite(
        session,
        token=new_token,
        user_id=99,
        username=None,
        now=NOW,
    )
    assert reinvited is not None and reinvited.status == "pending"
    connection_row = await session.get(models.Connection, connection.id)
    assert connection_row is not None
    assert connection_row.dry_run is True
    assert connection_row.kill_switch is True


@pytest.mark.asyncio
async def test_two_owners_with_the_same_contact_are_fully_isolated(
    session: AsyncSession,
) -> None:
    first = await upsert_connection(session, SNAPSHOT)
    second = await upsert_connection(
        session, ConnectionSnapshot("connection-2", owner_user_id=99, owner_chat_id=99)
    )
    session.add_all(
        [
            models.Schedule(
                connection_id=first.id,
                weekday_mask=127,
                time_from=time(22, 0),
                time_to=time(8, 0),
            ),
            models.Schedule(
                connection_id=second.id,
                weekday_mask=31,
                time_from=time(18, 0),
                time_to=time(9, 0),
            ),
        ]
    )
    await set_contact_exclusion(session, first.id, 100, until=None, reason="first_owner_only")
    await set_contact_template_override(
        session,
        first.id,
        100,
        template_code="money_priority",
        template_text="first owner template",
    )
    await claim_window(session, first.id, 100, window_key="first-window")
    await set_control_state(session, first.id, "mute_hours")
    first_log_id = await log_decision(
        session,
        connection_id=first.id,
        contact_id=100,
        action=LogAction.DRY_RUN,
        category="general",
        occurred_at=NOW,
    )
    second_log_id = await log_decision(
        session,
        connection_id=second.id,
        contact_id=100,
        action=LogAction.DRY_RUN,
        category="money",
        occurred_at=NOW,
    )

    first_record = await load_connection(session, "connection-1")
    second_record = await load_connection(session, "connection-2")
    assert first_record is not None and second_record is not None
    assert first_record.policy.windows[0].time_from == time(22, 0)
    assert second_record.policy.windows[0].time_from == time(18, 0)
    assert first_record.control_state == "mute_hours"
    assert second_record.control_state == "main"
    assert (await load_contact_state(session, first.id, 100)).exclusion is not None
    assert (await load_contact_state(session, second.id, 100)).exclusion is None
    assert await load_forced_template_code(session, first.id, 100) == "money_priority"
    assert await load_forced_template_code(session, second.id, 100) is None
    assert await load_templates(session, first.id) == {"money_priority": "first owner template"}
    assert await load_templates(session, second.id) == {}
    first_counts = await daily_action_counts(
        session, first.id, since=NOW - timedelta(minutes=1), until=NOW + timedelta(minutes=1)
    )
    second_counts = await daily_action_counts(
        session, second.id, since=NOW - timedelta(minutes=1), until=NOW + timedelta(minutes=1)
    )
    assert first_counts == [("dry_run", "general", 1)]
    assert second_counts == [("dry_run", "money", 1)]
    assert await feedback_belongs_to_owner(session, log_id=first_log_id, owner_user_id=42)
    assert not await feedback_belongs_to_owner(session, log_id=second_log_id, owner_user_id=42)


@pytest.mark.asyncio
async def test_connection_is_created_then_refreshed(session: AsyncSession) -> None:
    created = await upsert_connection(session, SNAPSHOT)
    refreshed = await upsert_connection(
        session, ConnectionSnapshot("connection-1", owner_user_id=42, is_enabled=False)
    )

    assert refreshed.id == created.id
    assert refreshed.policy.is_active is False
    assert refreshed.dry_run is True, "a fresh connection must start in dry run"
    assert refreshed.control_state == "main"
    # A connection update without user_chat_id must not erase the known one.
    assert refreshed.owner_chat_id == 42


@pytest.mark.asyncio
async def test_unknown_connection_reads_as_missing(session: AsyncSession) -> None:
    assert await load_connection(session, "nope") is None


@pytest.mark.asyncio
async def test_schedule_rows_become_the_gate_policy(session: AsyncSession) -> None:
    connection_id = await stored_connection(session)
    session.add_all(
        [
            models.Schedule(
                connection_id=connection_id,
                weekday_mask=0b1111111,
                time_from=time(22, 0),
                time_to=time(8, 0),
                is_active=True,
            ),
            models.Schedule(
                connection_id=connection_id,
                weekday_mask=0b1111111,
                time_from=time(12, 0),
                time_to=time(13, 0),
                is_active=False,
            ),
        ]
    )
    await session.flush()

    record = await load_connection(session, "connection-1")

    assert record is not None
    assert len(record.policy.windows) == 2
    assert (
        evaluate_gate(
            record.policy, await load_contact_state(session, record.id, 100), now=NOW
        ).decision
        is GateDecision.ALLOWED
    )


@pytest.mark.asyncio
async def test_exclusion_and_window_key_reach_the_gate(session: AsyncSession) -> None:
    connection_id = await stored_connection(session)
    session.add(
        models.Exclusion(connection_id=connection_id, contact_id=100, until=NOW + timedelta(days=1))
    )
    await record_auto_reply(session, connection_id, 100, at=NOW, window_key="2026-08-23:1")

    state = await load_contact_state(session, connection_id, 100)

    assert state.exclusion is not None and state.exclusion.covers(NOW)
    assert state.last_auto_reply_window_key == "2026-08-23:1"


@pytest.mark.asyncio
async def test_contact_without_history_has_empty_state(session: AsyncSession) -> None:
    connection_id = await stored_connection(session)

    state = await load_contact_state(session, connection_id, 100)

    assert state.exclusion is None
    assert state.last_auto_reply_window_key is None


@pytest.mark.asyncio
async def test_owner_reply_is_compared_against_the_incoming_message(session: AsyncSession) -> None:
    connection_id = await stored_connection(session)
    await record_incoming(session, connection_id, 100, at=NOW)

    assert await owner_replied_since(session, connection_id, 100, moment=NOW) is False

    await record_owner_reply(session, connection_id, 100, at=NOW + timedelta(minutes=1))

    assert await owner_replied_since(session, connection_id, 100, moment=NOW) is True
    # An older reply belongs to a previous conversation, not to this message.
    assert (
        await owner_replied_since(session, connection_id, 100, moment=NOW + timedelta(minutes=2))
        is False
    )


@pytest.mark.asyncio
async def test_owner_reply_without_activity_row_is_false(session: AsyncSession) -> None:
    connection_id = await stored_connection(session)

    assert await owner_replied_since(session, connection_id, 999, moment=NOW) is False


@pytest.mark.asyncio
async def test_decisions_are_logged_without_message_bodies(session: AsyncSession) -> None:
    connection_id = await stored_connection(session)

    log_id = await log_decision(
        session,
        connection_id=connection_id,
        contact_id=100,
        action=LogAction.DRY_RUN,
        tg_message_id=7,
        category="money",
        confidence=Decimal("0.91"),
        template_code="money_priority",
        occurred_at=NOW,
    )
    row = await session.get(models.MessageLog, log_id)

    assert row is not None
    assert row.action == "dry_run"
    assert row.direction == "in"
    assert row.body_encrypted is None
    assert row.retention_until is None


@pytest.mark.asyncio
async def test_shadow_feedback_attaches_to_a_logged_decision(session: AsyncSession) -> None:
    connection_id = await stored_connection(session)
    log_id = await log_decision(
        session, connection_id=connection_id, contact_id=100, action=LogAction.DRY_RUN
    )

    feedback_id = await record_feedback(session, log_id=log_id, verdict="ok")

    assert await session.get(models.ShadowFeedback, feedback_id) is not None


@pytest.mark.asyncio
async def test_repeated_feedback_updates_the_existing_row(session: AsyncSession) -> None:
    connection_id = await stored_connection(session)
    log_id = await log_decision(
        session, connection_id=connection_id, contact_id=100, action=LogAction.DRY_RUN
    )

    first_id = await record_feedback(session, log_id=log_id, verdict="ok")
    second_id = await record_feedback(session, log_id=log_id, verdict="wrong")

    assert second_id == first_id
    row = await session.get(models.ShadowFeedback, first_id)
    assert row is not None and row.verdict == "wrong"


@pytest.mark.asyncio
async def test_money_messages_queue_for_the_morning(session: AsyncSession) -> None:
    connection_id = await stored_connection(session)

    await enqueue_morning(
        session,
        connection_id=connection_id,
        contact_id=100,
        occurred_at=NOW,
        contact_name="Вася",
        summary="спрашивает про оплату",
    )
    pending = await session.scalar(
        select(func.count())
        .select_from(models.MorningQueue)
        .where(models.MorningQueue.is_delivered.is_(False))
    )

    assert pending == 1


@pytest.mark.asyncio
async def test_templates_come_from_the_database(session: AsyncSession) -> None:
    connection_id = await stored_connection(session)
    session.add_all(
        [
            models.Template(
                connection_id=connection_id, code="off_hours_default", text="позже", is_active=True
            ),
            models.Template(
                connection_id=connection_id, code="retired", text="старый", is_active=False
            ),
        ]
    )
    await session.flush()

    assert await load_templates(session, connection_id) == {"off_hours_default": "позже"}


@pytest.mark.asyncio
async def test_classifier_settings_fall_back_to_the_shipped_prompt(session: AsyncSession) -> None:
    connection_id = await stored_connection(session)

    assert await load_classifier_settings(session, connection_id) == ClassifierSettings()


@pytest.mark.asyncio
async def test_classifier_settings_are_owner_editable(session: AsyncSession) -> None:
    connection_id = await stored_connection(session)
    session.add(
        models.Prompt(
            connection_id=connection_id,
            code="classifier",
            system_prompt="свой промпт",
            model="claude-sonnet-5",
            confidence_min=Decimal("0.85"),
        )
    )
    await session.flush()

    settings = await load_classifier_settings(
        session, connection_id, defaults=ClassifierSettings(timeout_seconds=3.0)
    )

    assert settings.system_prompt == "свой промпт"
    assert settings.model == "claude-sonnet-5"
    assert settings.confidence_min == Decimal("0.85")
    assert settings.timeout_seconds == 3.0


@pytest.mark.asyncio
async def test_claiming_a_window_blocks_the_next_message_immediately(
    session: AsyncSession,
) -> None:
    connection_id = await stored_connection(session)

    await claim_window(session, connection_id, 100, window_key="2026-08-23:1")
    state = await load_contact_state(session, connection_id, 100)

    assert state.last_auto_reply_window_key == "2026-08-23:1"


@pytest.mark.asyncio
async def test_reconnecting_keeps_the_owners_settings(session: AsyncSession) -> None:
    first = await upsert_connection(session, SNAPSHOT)
    session.add(
        models.Schedule(
            connection_id=first.id, weekday_mask=127, time_from=time(22, 0), time_to=time(8, 0)
        )
    )
    await session.flush()

    # The owner switches the bot off and on; Telegram issues a new id.
    await upsert_connection(
        session, ConnectionSnapshot("connection-1", owner_user_id=42, is_enabled=False)
    )
    reconnected = await upsert_connection(
        session,
        ConnectionSnapshot("connection-2", owner_user_id=42, owner_chat_id=42),
    )

    assert reconnected.id == first.id, "a reconnect must not orphan the schedule"
    assert len(reconnected.policy.windows) == 1
    assert reconnected.policy.is_active is True


@pytest.mark.asyncio
async def test_a_second_owner_gets_a_row_of_their_own(session: AsyncSession) -> None:
    first = await upsert_connection(session, SNAPSHOT)

    other = await upsert_connection(
        session, ConnectionSnapshot("connection-9", owner_user_id=99, owner_chat_id=99)
    )

    assert other.id != first.id
