from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from secretary_bot.gate import (
    ConnectionPolicy,
    ContactState,
    Exclusion,
    GateDecision,
    QuietWindow,
    current_window,
    evaluate_gate,
)

KYIV = ZoneInfo("Europe/Kyiv")
ALL_DAYS = 0b1111111
MONDAY = 1 << 0
TUESDAY = 1 << 1

NIGHT = QuietWindow(
    schedule_id=1,
    weekday_mask=ALL_DAYS,
    time_from=time(22, 0),
    time_to=time(8, 0),
)
# Monday 2026-08-24 03:14 in Kyiv: inside the window that started Sunday 22:00.
INSIDE_NIGHT = datetime(2026, 8, 24, 3, 14, tzinfo=KYIV).astimezone(UTC)
OUTSIDE_NIGHT = datetime(2026, 8, 24, 12, 0, tzinfo=KYIV).astimezone(UTC)


def policy(**changes: object) -> ConnectionPolicy:
    defaults: dict[str, object] = {"timezone": "Europe/Kyiv", "windows": (NIGHT,)}
    return ConnectionPolicy(**(defaults | changes))  # type: ignore[arg-type]


def test_inactive_connection_stops_before_every_other_rule() -> None:
    result = evaluate_gate(
        policy(is_active=False, kill_switch=True),
        ContactState(exclusion=Exclusion()),
        now=INSIDE_NIGHT,
    )

    assert result.decision is GateDecision.SKIPPED_INACTIVE
    assert result.window_key is None


def test_kill_switch_stops_before_exclusions_and_schedule() -> None:
    result = evaluate_gate(
        policy(kill_switch=True), ContactState(exclusion=Exclusion()), now=INSIDE_NIGHT
    )

    assert result.decision is GateDecision.SKIPPED_KILL_SWITCH


def test_temporary_mute_stops_then_expires_without_a_worker() -> None:
    muted = policy(muted_until=INSIDE_NIGHT + timedelta(hours=3))

    assert evaluate_gate(muted, ContactState(), now=INSIDE_NIGHT).decision is (
        GateDecision.SKIPPED_KILL_SWITCH
    )
    assert evaluate_gate(muted, ContactState(), now=INSIDE_NIGHT + timedelta(hours=3)).decision is (
        GateDecision.ALLOWED
    )


def test_permanent_exclusion_stops_processing() -> None:
    result = evaluate_gate(policy(), ContactState(exclusion=Exclusion()), now=INSIDE_NIGHT)

    assert result.decision is GateDecision.SKIPPED_EXCLUDED


def test_temporary_exclusion_applies_until_its_deadline() -> None:
    contact = ContactState(exclusion=Exclusion(until=INSIDE_NIGHT + timedelta(hours=1)))

    assert evaluate_gate(policy(), contact, now=INSIDE_NIGHT).decision is (
        GateDecision.SKIPPED_EXCLUDED
    )


def test_expired_exclusion_no_longer_blocks() -> None:
    contact = ContactState(exclusion=Exclusion(until=INSIDE_NIGHT - timedelta(seconds=1)))

    assert evaluate_gate(policy(), contact, now=INSIDE_NIGHT).decision is GateDecision.ALLOWED


def test_message_outside_the_quiet_window_is_skipped() -> None:
    result = evaluate_gate(policy(), ContactState(), now=OUTSIDE_NIGHT)

    assert result.decision is GateDecision.SKIPPED_SCHEDULE
    assert result.window_key is None


def test_second_message_in_the_same_window_is_skipped() -> None:
    allowed = evaluate_gate(policy(), ContactState(), now=INSIDE_NIGHT)
    contact = ContactState(last_auto_reply_window_key=allowed.window_key)

    result = evaluate_gate(policy(), contact, now=INSIDE_NIGHT)

    assert result.decision is GateDecision.SKIPPED_WINDOW_LIMIT
    assert result.window_key == allowed.window_key


def test_next_night_is_a_new_window() -> None:
    previous = evaluate_gate(policy(), ContactState(), now=INSIDE_NIGHT)
    contact = ContactState(last_auto_reply_window_key=previous.window_key)

    result = evaluate_gate(policy(), contact, now=INSIDE_NIGHT + timedelta(days=1))

    assert result.decision is GateDecision.ALLOWED
    assert result.window_key != previous.window_key


def test_allowed_message_reports_the_window_it_belongs_to() -> None:
    result = evaluate_gate(policy(), ContactState(), now=INSIDE_NIGHT)

    assert result.is_allowed
    # The window started the previous evening, so the key names Sunday.
    assert result.window_key == "2026-08-23:1"


def test_owner_timezone_decides_whether_the_window_is_open() -> None:
    contact = ContactState()
    at_utc = datetime(2026, 8, 24, 4, 0, tzinfo=UTC)  # 07:00 in Kyiv, 16:00 in Auckland

    assert evaluate_gate(policy(), contact, now=at_utc).decision is GateDecision.ALLOWED
    assert (
        evaluate_gate(policy(timezone="Pacific/Auckland"), contact, now=at_utc).decision
        is GateDecision.SKIPPED_SCHEDULE
    )


def test_unknown_timezone_is_an_error_not_a_silent_pass() -> None:
    with pytest.raises(ZoneInfoNotFoundError):
        evaluate_gate(policy(timezone="Mars/Olympus"), ContactState(), now=INSIDE_NIGHT)


def test_inactive_and_empty_windows_are_ignored() -> None:
    windows = (
        QuietWindow(2, ALL_DAYS, time(22, 0), time(8, 0), is_active=False),
        QuietWindow(3, ALL_DAYS, time(0, 0), time(0, 0)),
    )

    assert current_window(windows, datetime(2026, 8, 24, 3, 14, tzinfo=KYIV)) is None


def test_weekday_mask_refers_to_the_day_the_window_starts() -> None:
    sunday_night = (QuietWindow(4, 1 << 6, time(22, 0), time(8, 0)),)

    monday_morning = current_window(sunday_night, datetime(2026, 8, 24, 3, 14, tzinfo=KYIV))
    monday_night = current_window(sunday_night, datetime(2026, 8, 24, 23, 0, tzinfo=KYIV))

    assert monday_morning is not None and monday_morning.key == "2026-08-23:4"
    assert monday_night is None


def test_same_day_window_needs_no_previous_day_candidate() -> None:
    lunch = (QuietWindow(5, MONDAY, time(13, 0), time(14, 0)),)

    inside = current_window(lunch, datetime(2026, 8, 24, 13, 30, tzinfo=KYIV))
    after = current_window(lunch, datetime(2026, 8, 24, 14, 0, tzinfo=KYIV))

    assert inside is not None and inside.key == "2026-08-24:5"
    assert after is None


def test_window_boundaries_are_half_open() -> None:
    windows = (NIGHT,)

    assert current_window(windows, datetime(2026, 8, 24, 22, 0, tzinfo=KYIV)) is not None
    assert current_window(windows, datetime(2026, 8, 24, 8, 0, tzinfo=KYIV)) is None


def test_first_matching_window_wins_when_several_overlap() -> None:
    windows = (NIGHT, QuietWindow(6, TUESDAY, time(0, 0), time(6, 0)))

    occurrence = current_window(windows, datetime(2026, 8, 25, 3, 0, tzinfo=KYIV))

    assert occurrence is not None and occurrence.schedule_id == 1


def test_no_schedule_at_all_keeps_the_bot_silent() -> None:
    result = evaluate_gate(policy(windows=()), ContactState(), now=INSIDE_NIGHT)

    assert result.decision is GateDecision.SKIPPED_SCHEDULE
