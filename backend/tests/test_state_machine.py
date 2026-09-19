"""Explicit lifecycle: illegal transitions fail (requirement 8)."""

from __future__ import annotations

import itertools

import pytest

from sikarescue.demo_data.sk10421 import seed_transaction
from sikarescue.errors import IllegalTransitionError
from sikarescue.models import Actor
from sikarescue.models import RecoveryState as S
from sikarescue.services.state_machine import ALLOWED_TRANSITIONS, assert_transition, can_transition

HAPPY_PATH = [
    S.FAILED,
    S.DIAGNOSING,
    S.AWAITING_APPROVAL,
    S.APPROVED,
    S.RECOVERY_EXECUTING,
    S.RECOVERED,
    S.RECONCILED,
]


def test_happy_path_is_legal():
    for current, target in itertools.pairwise(HAPPY_PATH):
        assert can_transition(current, target), f"{current} -> {target}"


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (S.FAILED, S.RECOVERED),
        (S.FAILED, S.RECOVERY_EXECUTING),
        (S.FAILED, S.APPROVED),
        (S.DIAGNOSING, S.RECOVERY_EXECUTING),
        (S.AWAITING_APPROVAL, S.RECOVERY_EXECUTING),  # no execution before approval
        (S.AWAITING_APPROVAL, S.RECOVERED),
        (S.RECOVERY_FAILED, S.RECOVERY_EXECUTING),  # a failed plan needs a NEW approval
        (S.RECOVERY_FAILED, S.APPROVED),
        (S.RECOVERED, S.RECOVERY_EXECUTING),
        (S.RECONCILED, S.FAILED),
        (S.MANUAL_REVIEW, S.AWAITING_APPROVAL),
        (S.MANUAL_REVIEW, S.RECOVERY_EXECUTING),
    ],
)
def test_illegal_transitions_raise(current, target):
    assert not can_transition(current, target)
    with pytest.raises(IllegalTransitionError):
        assert_transition(current, target)


def test_failure_and_ambiguity_branches():
    assert can_transition(S.RECOVERY_EXECUTING, S.RECOVERY_FAILED)
    assert can_transition(S.RECOVERY_FAILED, S.AWAITING_APPROVAL)
    assert can_transition(S.RECOVERY_EXECUTING, S.MANUAL_REVIEW)


def test_terminal_states_have_no_exits():
    assert ALLOWED_TRANSITIONS[S.RECONCILED] == frozenset()
    assert ALLOWED_TRANSITIONS[S.MANUAL_REVIEW] == frozenset()


def test_every_state_has_a_rule():
    assert set(ALLOWED_TRANSITIONS) == set(S)


def test_aggregate_transition_is_guarded_and_audited():
    aggregate = seed_transaction()
    with pytest.raises(IllegalTransitionError):
        aggregate.transition(S.RECOVERED, actor=Actor.SYSTEM, reason="shortcut")
    assert aggregate.state is S.FAILED
    aggregate.transition(S.DIAGNOSING, actor=Actor.SYSTEM, reason="test")
    assert aggregate.state is S.DIAGNOSING
    assert aggregate.audit[-1].data == {"from_state": "FAILED", "to_state": "DIAGNOSING"}
