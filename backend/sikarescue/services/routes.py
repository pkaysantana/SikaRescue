"""Route discovery (from live state) and independent verification of compute results.

Evaluation itself (hard filters -> simulation -> scoring) lives in `sikarescue.compute` so
any compute backend can run it. This module only builds candidates from the repository and
re-verifies whatever a backend returns before it may influence a plan.
"""

from __future__ import annotations

from sikarescue.compute.constraints import hard_constraint_violations
from sikarescue.compute.evaluation import ranking_key, scoring_input
from sikarescue.compute.scoring import score_route
from sikarescue.models import (
    AttemptOutcome,
    CandidateRecoveryRoute,
    EligibilitySnapshot,
    OperationType,
    OutstandingObligation,
    RailId,
    RouteEvaluation,
    route_id_for,
)
from sikarescue.services.liquidity import LiquidityBook
from sikarescue.services.policy import PolicyEngine
from sikarescue.services.rails import RailRegistry
from sikarescue.services.repository import TransactionAggregate

__all__ = [
    "build_candidate",
    "discover_candidates",
    "eligibility_of",
    "failed_payout_rails",
    "hard_constraint_violations",
    "verify_evaluations",
]


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
        simulation_profile=registry.simulation_profile(rail_id),
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


def eligibility_of(candidate: CandidateRecoveryRoute) -> EligibilitySnapshot:
    return EligibilitySnapshot(
        rail_status=candidate.rail.status,
        policy_permitted=candidate.policy.permitted,
        policy_version=candidate.policy.policy_version,
        liquidity_sufficient=candidate.liquidity.sufficient,
        recipient_compatible=candidate.recipient_compatible,
    )


def verify_evaluations(
    candidates: tuple[CandidateRecoveryRoute, ...],
    evaluations: tuple[RouteEvaluation, ...],
) -> list[str]:
    """Re-derive everything except the Monte Carlo statistics. Empty list = trustworthy.

    A compute backend can therefore never admit a policy-denied / down / incompatible /
    illiquid route, alter a fee, or change a score or rank: only simulated statistics are
    taken from it, and those feed the locally recomputed score.
    """
    by_route = {e.route_id: e for e in evaluations}
    if len(by_route) != len(evaluations) or set(by_route) != {c.route_id for c in candidates}:
        return ["evaluations do not correspond one-to-one with the candidates"]
    problems: list[str] = []
    passing: list[tuple[tuple, int]] = []
    for c in candidates:
        e = by_route[c.route_id]
        rail_id = c.rail.rail_id
        expected = tuple(reason for reason, _ in hard_constraint_violations(c))
        if e.rail_id is not rail_id or e.rejection_reasons != expected:
            problems.append(f"{rail_id}: hard-constraint result differs from local check")
            continue
        if e.estimated_incremental_cost != c.quote.incremental_fee:
            problems.append(f"{rail_id}: incremental fee differs from the quote")
        if e.passed:
            recomputed = score_route(scoring_input(c, e.scenario_results[0]))
            if recomputed != e.score:
                problems.append(f"{rail_id}: score differs from local recomputation")
            assert e.rank is not None
            passing.append((ranking_key(recomputed.total, c), e.rank))
    ranks_in_expected_order = [rank for _, rank in sorted(passing)]
    if ranks_in_expected_order != list(range(1, len(passing) + 1)):
        problems.append("ranking differs from local recomputation")
    return problems
