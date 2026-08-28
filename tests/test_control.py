from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any

import pytest
from aiogram.types import CallbackQuery, Message

from secretary_bot import models
from secretary_bot.actions import LogAction
from secretary_bot.control import (
    BUTTON_BACK,
    BUTTON_CANCEL,
    BUTTON_INVITE,
    BUTTON_LIVE,
    BUTTON_LIVE_CONFIRM,
    BUTTON_MUTE,
    BUTTON_OFF,
    BUTTON_ON,
    BUTTON_STATUS,
    BUTTON_TODAY,
    BUTTON_USERS,
    ControlPlane,
)
from secretary_bot.gate import ContactState, GateDecision, evaluate_gate
from secretary_bot.storage import (
    ConnectionSnapshot,
    Database,
    approve_access_user,
    consume_access_invite,
    create_access_invite,
    ensure_master,
    load_access_user,
    load_contact_state,
    load_owner_connection,
    log_decision,
    upsert_connection,
)

NOW = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.answered: list[str] = []
        self.edited: list[dict[str, Any]] = []

    async def send_message(self, **kwargs: Any) -> None:
        self.sent.append(kwargs)

    async def answer_callback_query(self, callback_query_id: str, **kwargs: Any) -> None:
        self.answered.append(callback_query_id)

    async def edit_message_text(self, **kwargs: Any) -> None:
        self.edited.append(kwargs)

    async def edit_message_reply_markup(self, **kwargs: Any) -> None:
        self.edited.append(kwargs)


def owner_message(text: str, *, owner_id: int = 42, chat_id: int = 42) -> Message:
    return Message.model_validate(
        {
            "message_id": 1,
            "date": NOW,
            "chat": {"id": chat_id, "type": "private", "first_name": "Owner"},
            "from": {"id": owner_id, "is_bot": False, "first_name": "Owner"},
            "text": text,
        }
    )


def owner_callback(data: str, *, owner_id: int = 42) -> CallbackQuery:
    return CallbackQuery.model_validate(
        {
            "id": "callback-1",
            "from": {"id": owner_id, "is_bot": False, "first_name": "Owner"},
            "chat_instance": "instance",
            "data": data,
            "message": {
                "message_id": 5,
                "date": NOW,
                "chat": {"id": 42, "type": "private", "first_name": "Owner"},
                "text": "Contact action",
            },
        }
    )


def keyboard_texts(message: dict[str, Any]) -> list[str]:
    return [button.text for row in message["reply_markup"].keyboard for button in row]


async def store_owner(database: Database) -> int:
    async with database.session() as session, session.begin():
        await ensure_master(session, 42)
        connection = await upsert_connection(
            session,
            ConnectionSnapshot(
                business_connection_id="connection-1",
                owner_user_id=42,
                owner_chat_id=42,
            ),
        )
        session.add(
            models.Schedule(
                connection_id=connection.id,
                weekday_mask=127,
                time_from=time(0, 0),
                time_to=time(23, 59),
            )
        )
        return connection.id


@pytest.mark.asyncio
async def test_off_status_and_on_change_the_persistent_gate(database: Database) -> None:
    await store_owner(database)
    bot = FakeBot()
    control = ControlPlane(database, bot)

    assert await control.handle_message(owner_message("/off"), now=NOW)
    assert await control.handle_message(owner_message("/status"), now=NOW)
    async with database.session() as session:
        connection = await load_owner_connection(session, 42)
        assert connection is not None
        assert connection.policy.kill_switch is True
    assert "выключен" in bot.sent[-1]["text"]
    assert BUTTON_ON in keyboard_texts(bot.sent[-1])
    assert BUTTON_OFF not in keyboard_texts(bot.sent[-1])

    assert await control.handle_message(owner_message("/on"), now=NOW)
    async with database.session() as session:
        connection = await load_owner_connection(session, 42)
        assert connection is not None
        assert connection.policy.kill_switch is False
        assert connection.policy.muted_until is None
    assert BUTTON_OFF in keyboard_texts(bot.sent[-1])
    assert BUTTON_ON not in keyboard_texts(bot.sent[-1])


@pytest.mark.asyncio
async def test_start_opens_the_button_control_panel(database: Database) -> None:
    await store_owner(database)
    bot = FakeBot()

    assert await ControlPlane(database, bot).handle_message(owner_message("/start"), now=NOW)

    assert keyboard_texts(bot.sent[-1]) == [
        BUTTON_STATUS,
        BUTTON_TODAY,
        BUTTON_OFF,
        BUTTON_MUTE,
        BUTTON_LIVE,
        BUTTON_USERS,
        BUTTON_INVITE,
    ]


@pytest.mark.asyncio
async def test_master_creates_an_invite_and_candidate_becomes_pending(
    database: Database,
) -> None:
    await store_owner(database)
    bot = FakeBot()
    control = ControlPlane(database, bot, bot_username="secretary_test_bot")

    assert await control.handle_message(owner_message(BUTTON_INVITE), now=NOW)
    link = bot.sent[-1]["text"].splitlines()[-1]
    assert link.startswith("https://t.me/secretary_test_bot?start=invite_")
    token = link.rsplit("invite_", 1)[1]

    assert await control.handle_message(
        owner_message(f"/start invite_{token}", owner_id=99, chat_id=99), now=NOW
    )
    async with database.session() as session:
        candidate = await load_access_user(session, 99)
        assert candidate is not None
        assert candidate.status == "pending"
        assert candidate.username is None
    assert "Дождитесь подтверждения" in bot.sent[-2]["text"]
    assert "новый запрос" in bot.sent[-1]["text"]


@pytest.mark.asyncio
async def test_master_approves_and_revokes_a_pending_candidate(database: Database) -> None:
    await store_owner(database)
    async with database.session() as session, session.begin():
        token = await create_access_invite(session, created_by=42, now=NOW, ttl=timedelta(hours=1))
        await consume_access_invite(session, token=token, user_id=99, username="customer", now=NOW)
    bot = FakeBot()
    control = ControlPlane(database, bot, bot_username="secretary_test_bot")

    assert await control.handle_message(owner_message(BUTTON_USERS), now=NOW)
    callbacks = [
        button.callback_data
        for row in bot.sent[-1]["reply_markup"].inline_keyboard
        for button in row
    ]
    assert callbacks == ["access:approve:99", "access:revoke:99"]

    assert await control.handle_callback(owner_callback("access:approve:99"), now=NOW)
    async with database.session() as session:
        candidate = await load_access_user(session, 99)
        assert candidate is not None and candidate.status == "active"
    assert bot.edited[-1]["reply_markup"] is None
    assert "Доступ подтверждён" in bot.sent[-1]["text"]

    assert await control.handle_callback(owner_callback("access:revoke:99"), now=NOW)
    async with database.session() as session:
        candidate = await load_access_user(session, 99)
        assert candidate is not None and candidate.status == "revoked"


@pytest.mark.asyncio
async def test_non_master_cannot_use_access_callbacks(database: Database) -> None:
    await store_owner(database)
    async with database.session() as session, session.begin():
        token = await create_access_invite(session, created_by=42, now=NOW, ttl=timedelta(hours=1))
        await consume_access_invite(session, token=token, user_id=99, username=None, now=NOW)
        await approve_access_user(session, user_id=99, approved_by=42, now=NOW)
    bot = FakeBot()

    assert not await ControlPlane(database, bot).handle_callback(
        owner_callback("access:revoke:42", owner_id=99), now=NOW
    )
    assert bot.sent == []


@pytest.mark.asyncio
async def test_mute_blocks_for_requested_hours_then_expires(database: Database) -> None:
    await store_owner(database)
    bot = FakeBot()
    control = ControlPlane(database, bot)

    assert await control.handle_message(owner_message("/mute 3"), now=NOW)
    async with database.session() as session:
        connection = await load_owner_connection(session, 42)
        assert connection is not None
        assert connection.policy.muted_until == NOW + timedelta(hours=3)
        assert evaluate_gate(connection.policy, ContactState(), now=NOW).decision is (
            GateDecision.SKIPPED_KILL_SWITCH
        )
        assert (
            evaluate_gate(connection.policy, ContactState(), now=NOW + timedelta(hours=3)).decision
            is GateDecision.ALLOWED
        )


@pytest.mark.asyncio
async def test_invalid_mute_does_not_change_state(database: Database) -> None:
    await store_owner(database)
    bot = FakeBot()
    control = ControlPlane(database, bot)

    assert await control.handle_message(owner_message("/mute forever"), now=NOW)
    async with database.session() as session:
        connection = await load_owner_connection(session, 42)
        assert connection is not None and connection.policy.muted_until is None
    assert "Выберите кнопку" in bot.sent[-1]["text"]


@pytest.mark.asyncio
async def test_mute_keyboard_state_persists_between_handlers(database: Database) -> None:
    await store_owner(database)
    bot = FakeBot()

    assert await ControlPlane(database, bot).handle_message(owner_message(BUTTON_MUTE), now=NOW)
    assert keyboard_texts(bot.sent[-1]) == ["1 час", "3 часа", "8 часов", "24 часа", BUTTON_BACK]
    async with database.session() as session:
        connection = await load_owner_connection(session, 42)
        assert connection is not None and connection.control_state == "mute_hours"

    assert await ControlPlane(database, bot).handle_message(owner_message("3 часа"), now=NOW)
    async with database.session() as session:
        connection = await load_owner_connection(session, 42)
        assert connection is not None
        assert connection.control_state == "main"
        assert connection.policy.muted_until == NOW + timedelta(hours=3)
    assert BUTTON_ON in keyboard_texts(bot.sent[-1])


@pytest.mark.asyncio
async def test_mute_duration_is_not_a_command_in_the_main_state(database: Database) -> None:
    await store_owner(database)
    bot = FakeBot()

    assert not await ControlPlane(database, bot).handle_message(owner_message("3 часа"), now=NOW)
    assert bot.sent == []


@pytest.mark.asyncio
async def test_today_lists_only_the_owners_local_day(database: Database) -> None:
    connection_id = await store_owner(database)
    async with database.session() as session, session.begin():
        await log_decision(
            session,
            connection_id=connection_id,
            contact_id=100,
            action=LogAction.DRY_RUN,
            category="general",
            occurred_at=NOW,
        )
        await log_decision(
            session,
            connection_id=connection_id,
            contact_id=100,
            action=LogAction.SKIPPED_SCHEDULE,
            occurred_at=NOW - timedelta(days=1),
        )

    bot = FakeBot()
    assert await ControlPlane(database, bot).handle_message(owner_message("/today"), now=NOW)

    assert "dry_run/general: 1" in bot.sent[-1]["text"]
    assert "skipped_schedule" not in bot.sent[-1]["text"]


@pytest.mark.asyncio
async def test_unknown_or_unauthorized_commands_are_ignored(database: Database) -> None:
    await store_owner(database)
    bot = FakeBot()
    control = ControlPlane(database, bot)

    assert not await control.handle_message(owner_message("/unknown"), now=NOW)
    assert not await control.handle_message(owner_message("/off", owner_id=7, chat_id=7), now=NOW)
    assert bot.sent == []


@pytest.mark.asyncio
async def test_business_deep_link_opens_the_requested_contact_card(database: Database) -> None:
    await store_owner(database)
    bot = FakeBot()

    assert await ControlPlane(database, bot).handle_message(
        owner_message("/start bizChat100"), now=NOW
    )

    assert "Контакт 100" in bot.sent[-1]["text"]
    callbacks = [
        button.callback_data
        for row in bot.sent[-1]["reply_markup"].inline_keyboard
        for button in row
    ]
    assert "contact:100:exclude" in callbacks
    assert "contact:100:today" in callbacks
    assert all("101" not in callback for callback in callbacks if callback is not None)


@pytest.mark.asyncio
async def test_contact_card_can_exclude_forever(database: Database) -> None:
    connection_id = await store_owner(database)
    bot = FakeBot()

    assert await ControlPlane(database, bot).handle_callback(
        owner_callback("contact:100:exclude"), now=NOW
    )

    async with database.session() as session:
        exclusion = await session.get(models.Exclusion, 1)
        assert exclusion is not None
        assert exclusion.connection_id == connection_id
        assert exclusion.contact_id == 100
        assert exclusion.until is None
        connection = await load_owner_connection(session, 42)
        assert connection is not None
        state = await load_contact_state(session, connection_id, 100)
        assert evaluate_gate(connection.policy, state, now=NOW).decision is (
            GateDecision.SKIPPED_EXCLUDED
        )
    assert bot.edited[-1]["reply_markup"] is None
    assert "исключён навсегда" in bot.edited[-1]["text"]


@pytest.mark.asyncio
async def test_contact_card_can_exclude_until_local_midnight(database: Database) -> None:
    await store_owner(database)
    bot = FakeBot()

    assert await ControlPlane(database, bot).handle_callback(
        owner_callback("contact:100:today"), now=NOW
    )

    async with database.session() as session:
        exclusion = await session.get(models.Exclusion, 1)
        assert exclusion is not None
        assert exclusion.until == datetime(2026, 8, 28, 21, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_contact_card_can_force_a_template(database: Database) -> None:
    await store_owner(database)
    bot = FakeBot()
    control = ControlPlane(database, bot)

    assert await control.handle_callback(owner_callback("contact:100:templates"), now=NOW)
    assert "Выберите шаблон" in bot.sent[-1]["text"]
    assert await control.handle_callback(
        owner_callback("contact:100:template:money_priority"), now=NOW
    )

    async with database.session() as session:
        override = await session.get(models.Override, 1)
        assert override is not None and override.mode == "force_template"
        template = await session.get(models.Template, override.template_id)
        assert template is not None and template.code == "money_priority"


@pytest.mark.asyncio
async def test_contact_callback_from_a_stranger_is_ignored(database: Database) -> None:
    await store_owner(database)
    bot = FakeBot()

    assert not await ControlPlane(database, bot).handle_callback(
        owner_callback("contact:100:exclude", owner_id=7), now=NOW
    )
    assert bot.sent == []


@pytest.mark.asyncio
async def test_live_command_requires_a_separate_confirmation(database: Database) -> None:
    await store_owner(database)
    bot = FakeBot()
    control = ControlPlane(database, bot)

    assert await control.handle_message(owner_message("/live"), now=NOW)
    async with database.session() as session:
        connection = await load_owner_connection(session, 42)
        assert connection is not None
        assert connection.dry_run is True
        assert connection.control_state == "live_confirm"
    assert keyboard_texts(bot.sent[-1]) == [BUTTON_LIVE_CONFIRM, BUTTON_CANCEL]

    assert await ControlPlane(database, bot).handle_message(
        owner_message(BUTTON_LIVE_CONFIRM), now=NOW
    )
    async with database.session() as session:
        connection = await load_owner_connection(session, 42)
        assert connection is not None
        assert connection.dry_run is False
        assert connection.control_state == "main"


@pytest.mark.asyncio
async def test_expired_live_confirmation_keeps_dry_run(database: Database) -> None:
    await store_owner(database)
    bot = FakeBot()
    control = ControlPlane(database, bot)

    assert await control.handle_message(owner_message("/live"), now=NOW)
    assert await control.handle_message(
        owner_message(BUTTON_LIVE_CONFIRM), now=NOW + timedelta(minutes=5)
    )

    async with database.session() as session:
        connection = await load_owner_connection(session, 42)
        assert connection is not None and connection.dry_run is True
    assert "истекло" in bot.sent[-1]["text"]


@pytest.mark.asyncio
async def test_cancelled_live_confirmation_keeps_dry_run(database: Database) -> None:
    await store_owner(database)
    bot = FakeBot()
    control = ControlPlane(database, bot)

    assert await control.handle_message(owner_message("/live"), now=NOW)
    assert await control.handle_message(owner_message(BUTTON_CANCEL), now=NOW)

    async with database.session() as session:
        connection = await load_owner_connection(session, 42)
        assert connection is not None
        assert connection.dry_run is True
        assert connection.control_state == "main"


@pytest.mark.asyncio
async def test_off_invalidates_a_pending_live_confirmation(database: Database) -> None:
    await store_owner(database)
    bot = FakeBot()
    control = ControlPlane(database, bot)

    assert await control.handle_message(owner_message("/live"), now=NOW)
    assert await control.handle_message(owner_message("/off"), now=NOW)
    assert await control.handle_callback(owner_callback("live:confirm"), now=NOW)

    async with database.session() as session:
        connection = await load_owner_connection(session, 42)
        assert connection is not None
        assert connection.dry_run is True
        assert connection.policy.kill_switch is True
