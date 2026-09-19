"""Route discovery (from live state) and independent verification of compute results.

Evaluation (hard filters -> simulation -> scoring) and its independent verification live in
`sikarescue.compute` so every backend shares them. This module builds candidates from the
repository; `verify_evaluations` is re-exported for the recovery service.
"""

from __future__ import annotations

import hashlib
import json

from sikarescue.compute.constraints import hard_constraint_violations
from sikarescue.compute.verification import verify_evaluations
from sikarescue.models import (
    AttemptOutcome,
    CandidateRecoveryRoute,
    EligibilitySnapshot,
    OperationType,
    OutstandingObligation,
    RailId,
    SimulationConfig,
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
    "route_set_fingerprint",
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


def route_set_fingerprint(
    candidates: tuple[CandidateRecoveryRoute, ...], config: SimulationConfig
) -> str:
    """Hash of everything that decides WHICH route ranks first.

    Covers every candidate's hard-constraint result, quote (id, fee, latency, reliability),
    dependency count and simulation profile, plus the simulation config. Simulation is seeded
    and counter-based, so while this is unchanged the ranking is unchanged too. A plan whose
    fingerprint no longer matches may no longer be the rank-1 route and must be re-planned.
    """
    routes = sorted(
        (
            {
                "rail": c.rail.rail_id.value,
                "violations": [reason.value for reason, _ in hard_constraint_violations(c)],
                "quote_id": c.quote.quote_id,
                "fee": str(c.quote.incremental_fee),
                "latency": c.quote.expected_latency_seconds,
                "quoted_reliability": c.quote.quoted_reliability,
                "dependencies": c.rail.dependency_count,
                "profile": c.simulation_profile.model_dump(mode="json"),
            }
            for c in candidates
        ),
        key=lambda r: r["rail"],
    )
    payload = {"routes": routes, "simulation": config.model_dump(mode="json")}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def eligibility_of(candidate: CandidateRecoveryRoute) -> EligibilitySnapshot:
    return EligibilitySnapshot(
        rail_status=candidate.rail.status,
        policy_permitted=candidate.policy.permitted,
        policy_version=candidate.policy.policy_version,
        liquidity_sufficient=candidate.liquidity.sufficient,
        recipient_compatible=candidate.recipient_compatible,
    )
