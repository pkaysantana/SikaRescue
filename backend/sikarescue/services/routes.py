"""Route discovery, hard-constraint filtering and deterministic evaluation.

Order is fixed: build candidates -> apply hard constraints -> score survivors -> rank.
A rejected route is never scored, so optimisation can never trade away a constraint.
"""

from __future__ import annotations

from sikarescue.compute.scoring import ScoringInput, score_route
from sikarescue.models import (
    AttemptOutcome,
    CandidateRecoveryRoute,
    EligibilitySnapshot,
    HardConstraintStatus,
    OperationType,
    OutstandingObligation,
    RailId,
    RailStatus,
    RejectionReason,
    RouteEvaluation,
    route_id_for,
)
from sikarescue.services.liquidity import LiquidityBook
from sikarescue.services.policy import PolicyEngine
from sikarescue.services.rails import RailRegistry
from sikarescue.services.repository import TransactionAggregate


def failed_payout_rails(aggregate: TransactionAggregate) -> frozenset[RailId]:
    return frozenset(
        a.rail_id
        for a in aggregate.journal.attempts()
        if a.operation is OperationType.RECIPIENT_CREDIT
        and a.outcome is AttemptOutcome.DEFINITIVE_FAILED
    )


def build_candidate(
    aggregate: TransactionAggregate,
    obligation: OutstandingObligation,
    rail_id: RailId,
    registry: RailRegistry,
    policy: PolicyEngine,
    liquidity: LiquidityBook,
) -> CandidateRecoveryRoute:
    rail = registry.get(rail_id)
    instruction = aggregate.instruction
    return CandidateRecoveryRoute(
        route_id=route_id_for(rail_id),
        transaction_id=instruction.transaction_id,
        rail=rail,
        source=obligation.source,
        amount=obligation.amount,
        quote=registry.quote(rail_id),
        policy=policy.check(instruction, rail, obligation.source, obligation.amount),
        liquidity=liquidity.check(rail_id, obligation.amount),
        recipient_compatible=obligation.endpoint_type in rail.supported_endpoints,
        is_original_rail=rail_id in instruction.original_route,
        failed_earlier_for_transaction=rail_id in failed_payout_rails(aggregate),
    )


def discover_candidates(
    aggregate: TransactionAggregate,
    obligation: OutstandingObligation,
    registry: RailRegistry,
    policy: PolicyEngine,
    liquidity: LiquidityBook,
) -> tuple[CandidateRecoveryRoute, ...]:
    """Every payout rail able to pay out from where the funds are now (incl. the failed one)."""
    return tuple(
        build_candidate(aggregate, obligation, rail.rail_id, registry, policy, liquidity)
        for rail in registry.payout_rails_from(obligation.source)
    )


def hard_constraint_violations(
    candidate: CandidateRecoveryRoute,
) -> list[tuple[RejectionReason, str]]:
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


def eligibility_of(candidate: CandidateRecoveryRoute) -> EligibilitySnapshot:
    return EligibilitySnapshot(
        rail_status=candidate.rail.status,
        policy_permitted=candidate.policy.permitted,
        policy_version=candidate.policy.policy_version,
        liquidity_sufficient=candidate.liquidity.sufficient,
        recipient_compatible=candidate.recipient_compatible,
    )


def evaluate_routes(
    candidates: tuple[CandidateRecoveryRoute, ...], compute_backend: str = "local"
) -> tuple[RouteEvaluation, ...]:
    """Hard-filter, then score and rank survivors. Passing routes first (rank 1 = best)."""
    scored = []
    rejected = []
    for c in candidates:
        common = {
            "route_id": c.route_id,
            "rail_id": c.rail.rail_id,
            "estimated_incremental_cost": c.quote.incremental_fee,
            "expected_latency_seconds": c.quote.expected_latency_seconds,
            "quoted_reliability": c.quote.quoted_reliability,
            "compute_backend": compute_backend,
        }
        violations = hard_constraint_violations(c)
        if violations:
            rejected.append(
                RouteEvaluation(
                    hard_constraint_status=HardConstraintStatus.REJECTED,
                    rejection_reasons=tuple(r for r, _ in violations),
                    rejection_details=tuple(d for _, d in violations),
                    **common,
                )
            )
            continue
        score = score_route(
            ScoringInput(
                route_id=c.route_id,
                reliability=c.quote.quoted_reliability,
                incremental_fee_gbp=c.quote.incremental_fee.amount,
                latency_seconds=c.quote.expected_latency_seconds,
                dependency_count=c.rail.dependency_count,
            )
        )
        scored.append((score, c, common))

    # Deterministic ordering: score desc, then cheaper, then faster, then route id.
    scored.sort(
        key=lambda s: (
            -s[0].total,
            s[1].quote.incremental_fee.amount,
            s[1].quote.expected_latency_seconds,
            s[1].route_id,
        )
    )
    passed = [
        RouteEvaluation(
            hard_constraint_status=HardConstraintStatus.PASSED,
            score=score,
            rank=rank,
            **common,
        )
        for rank, (score, _, common) in enumerate(scored, start=1)
    ]
    return (*passed, *rejected)
