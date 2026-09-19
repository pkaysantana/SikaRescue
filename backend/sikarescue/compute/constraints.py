"""Hard constraints. Pure, so every compute backend and the recovery service share one copy."""

from __future__ import annotations

from sikarescue.models import CandidateRecoveryRoute, RailStatus, RejectionReason


def hard_constraint_violations(
    candidate: CandidateRecoveryRoute,
) -> list[tuple[RejectionReason, str]]:
    """Every reason this route may not be used. Empty list = eligible for simulation/scoring."""
    violations: list[tuple[RejectionReason, str]] = []
    rail_id = candidate.rail.rail_id
    if not candidate.policy.permitted:
        detail = "; ".join(f"{d.rule_id}: {d.detail}" for d in candidate.policy.denials)
        violations.append((RejectionReason.POLICY_DENIED, detail))
    if candidate.rail.status is RailStatus.DOWN:
        violations.append((RejectionReason.RAIL_UNAVAILABLE, f"{rail_id} is DOWN"))
    if not candidate.recipient_compatible:
        violations.append(
            (RejectionReason.RECIPIENT_INCOMPATIBLE, f"{rail_id} cannot pay this endpoint type")
        )
    if not candidate.liquidity.sufficient:
        liq = candidate.liquidity
        violations.append(
            (
                RejectionReason.INSUFFICIENT_LIQUIDITY,
                f"available {liq.available} < required {liq.required}",
            )
        )
    if candidate.failed_earlier_for_transaction:
        violations.append(
            (
                RejectionReason.FAILED_EARLIER_FOR_TRANSACTION,
                f"{rail_id} already failed a payout attempt for this transaction",
            )
        )
    return violations
