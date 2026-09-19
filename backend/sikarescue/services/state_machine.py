"""Explicit recovery lifecycle. Any transition not listed here is illegal."""

from __future__ import annotations

from sikarescue.errors import IllegalTransitionError
from sikarescue.models import RecoveryState as S

ALLOWED_TRANSITIONS: dict[S, frozenset[S]] = {
    S.FAILED: frozenset({S.DIAGNOSING}),
    S.DIAGNOSING: frozenset({S.AWAITING_APPROVAL, S.MANUAL_REVIEW}),
    # Self-transition = a stale plan was replaced by a NEW plan that needs fresh approval.
    S.AWAITING_APPROVAL: frozenset({S.APPROVED, S.AWAITING_APPROVAL, S.MANUAL_REVIEW}),
    S.APPROVED: frozenset({S.RECOVERY_EXECUTING, S.AWAITING_APPROVAL, S.MANUAL_REVIEW}),
    S.RECOVERY_EXECUTING: frozenset({S.RECOVERED, S.RECOVERY_FAILED, S.MANUAL_REVIEW}),
    S.RECOVERY_FAILED: frozenset({S.AWAITING_APPROVAL, S.MANUAL_REVIEW}),
    S.RECOVERED: frozenset({S.RECONCILED}),
    S.RECONCILED: frozenset(),
    # Terminal for automation: resolving an ambiguous outcome needs human reconciliation.
    S.MANUAL_REVIEW: frozenset(),
}

# States from which a (new) recovery plan may be produced. DIAGNOSING is included so a
# planning run whose result was discarded as stale (or that crashed) can be retried.
PLANNABLE_STATES = frozenset(
    {S.FAILED, S.DIAGNOSING, S.RECOVERY_FAILED, S.AWAITING_APPROVAL, S.APPROVED}
)


def can_transition(current: S, target: S) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def assert_transition(current: S, target: S) -> None:
    if not can_transition(current, target):
        raise IllegalTransitionError(f"illegal recovery transition {current} -> {target}")
