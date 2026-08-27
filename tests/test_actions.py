from __future__ import annotations

from sqlalchemy import CheckConstraint

from secretary_bot.actions import LogAction
from secretary_bot.gate import GateDecision
from secretary_bot.hard_filter import HardFilterResult
from secretary_bot.models import Base


def test_every_gate_refusal_has_a_log_action() -> None:
    refusals = set(GateDecision) - {GateDecision.ALLOWED}

    assert {LogAction(decision.value) for decision in refusals} <= set(LogAction)


def test_dropped_content_has_a_log_action() -> None:
    reason = HardFilterResult.UNSUPPORTED_CONTENT.value

    assert LogAction(f"skipped_{reason}") is LogAction.SKIPPED_UNSUPPORTED_CONTENT


def test_database_accepts_exactly_the_declared_actions() -> None:
    constraint = next(
        constraint
        for constraint in Base.metadata.tables["message_log"].constraints
        if isinstance(constraint, CheckConstraint)
        and constraint.name == "ck_message_log_action_values"
    )
    condition = str(constraint.sqltext)

    for action in LogAction:
        assert f"'{action.value}'" in condition
