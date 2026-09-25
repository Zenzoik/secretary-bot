from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

DAY = timedelta(days=1)
MIDNIGHT = time(0, 0)


class GateDecision(StrEnum):
    ALLOWED = "allowed"
    SKIPPED_INACTIVE = "skipped_inactive"
    SKIPPED_KILL_SWITCH = "skipped_kill_switch"
    SKIPPED_EXCLUDED = "skipped_excluded"
    SKIPPED_UNCONFIGURED = "skipped_unconfigured"
    SKIPPED_SCHEDULE = "skipped_schedule"
    SKIPPED_WINDOW_LIMIT = "skipped_window_limit"


@dataclass(frozen=True, slots=True)
class QuietWindow:
    """One row of ``schedules``: when the bot is allowed to answer."""

    schedule_id: int
    weekday_mask: int
    time_from: time
    time_to: time
    is_active: bool = True

    def starts_on(self, day: date) -> bool:
        return bool(self.weekday_mask & (1 << day.weekday()))

    @property
    def full_day(self) -> bool:
        """00:00–00:00 is the one explicit way to say "around the clock"."""
        return self.time_from == MIDNIGHT and self.time_to == MIDNIGHT

    @property
    def duration(self) -> timedelta:
        if self.full_day:
            return DAY
        span = datetime.combine(date.min, self.time_to) - datetime.combine(date.min, self.time_from)
        return span % DAY


@dataclass(frozen=True, slots=True)
class WindowOccurrence:
    """A concrete run of a quiet window, identified by the day it started on."""

    schedule_id: int
    starts_at: datetime
    ends_at: datetime

    @property
    def key(self) -> str:
        return f"{self.starts_at.date().isoformat()}:{self.schedule_id}"


@dataclass(frozen=True, slots=True)
class Exclusion:
    """A contact the bot must not touch. ``until = None`` means forever."""

    until: datetime | None = None

    def covers(self, moment: datetime) -> bool:
        return self.until is None or moment < self.until


@dataclass(frozen=True, slots=True)
class ConnectionPolicy:
    timezone: str
    windows: tuple[QuietWindow, ...] = ()
    is_active: bool = True
    kill_switch: bool = False
    muted_until: datetime | None = None
    max_auto_replies_per_window: int | None = None


@dataclass(frozen=True, slots=True)
class ContactState:
    exclusion: Exclusion | None = None
    last_auto_reply_window_key: str | None = None
    auto_reply_count_in_window: int = 0
    windows: tuple[QuietWindow, ...] = ()
    # A contact the owner has not reviewed yet gets no automatic reply.
    configured: bool = True


@dataclass(frozen=True, slots=True)
class GateResult:
    decision: GateDecision
    window_key: str | None = None

    @property
    def is_allowed(self) -> bool:
        return self.decision is GateDecision.ALLOWED


def evaluate_gate(policy: ConnectionPolicy, contact: ContactState, *, now: datetime) -> GateResult:
    """Apply §6.2 in order; the first rule that fires stops the pipeline.

    ``now`` must be timezone-aware. An unknown owner timezone raises
    ``ZoneInfoNotFoundError`` — a misconfigured schedule is an error, not a
    silent pass.
    """
    if not policy.is_active:
        return GateResult(GateDecision.SKIPPED_INACTIVE)
    if policy.kill_switch or (policy.muted_until is not None and now < policy.muted_until):
        return GateResult(GateDecision.SKIPPED_KILL_SWITCH)
    if contact.exclusion is not None and contact.exclusion.covers(now):
        return GateResult(GateDecision.SKIPPED_EXCLUDED)
    if not contact.configured:
        return GateResult(GateDecision.SKIPPED_UNCONFIGURED)

    windows = contact.windows or policy.windows
    occurrence = current_window(windows, now.astimezone(ZoneInfo(policy.timezone)))
    if occurrence is None:
        return GateResult(GateDecision.SKIPPED_SCHEDULE)
    if (
        policy.max_auto_replies_per_window is not None
        and contact.last_auto_reply_window_key == occurrence.key
        and contact.auto_reply_count_in_window >= policy.max_auto_replies_per_window
    ):
        return GateResult(GateDecision.SKIPPED_WINDOW_LIMIT, window_key=occurrence.key)
    return GateResult(GateDecision.ALLOWED, window_key=occurrence.key)


def current_window(
    windows: tuple[QuietWindow, ...], local_now: datetime
) -> WindowOccurrence | None:
    """Return the quiet window covering ``local_now``, or ``None``.

    A window whose ``time_to`` is not after ``time_from`` crosses midnight, so
    both today and yesterday are candidate start days. The weekday mask always
    refers to the day the window *starts* on. ``time_to == time_from`` covers
    nothing: an ambiguous schedule keeps the bot silent. The one exception is
    00:00–00:00, the explicit whole day.
    """
    for occurrence in _occurrences(windows, local_now):
        if occurrence.starts_at <= local_now < occurrence.ends_at:
            return occurrence
    return None


def _occurrences(windows: tuple[QuietWindow, ...], local_now: datetime) -> list[WindowOccurrence]:
    occurrences = []
    for window in windows:
        if not window.is_active or not window.duration:
            continue
        for day in (local_now.date(), local_now.date() - DAY):
            if not window.starts_on(day):
                continue
            starts_at = datetime.combine(day, window.time_from, tzinfo=local_now.tzinfo)
            occurrences.append(
                WindowOccurrence(
                    schedule_id=window.schedule_id,
                    starts_at=starts_at,
                    ends_at=starts_at + window.duration,
                )
            )
    return occurrences
