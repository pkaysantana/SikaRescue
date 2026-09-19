"""Pure route evaluation: hard filters -> simulate survivors -> score -> rank.

This is what a compute backend runs. The order is fixed: a route that fails any hard
constraint is never simulated or scored, so optimisation can never trade a constraint away.
"""

from __future__ import annotations

from sikarescue.compute.constraints import hard_constraint_violations
from sikarescue.compute.scoring import LATENCY_CEILING_SECONDS, ScoringInput, score_route
from sikarescue.compute.simulation import simulate_route
from sikarescue.models import (
    CandidateRecoveryRoute,
    HardConstraintStatus,
    RouteEvaluation,
    RouteSimulationResult,
    SimulationConfig,
)


def scoring_input(
    candidate: CandidateRecoveryRoute, primary: RouteSimulationResult
) -> ScoringInput:
    """Scoring uses SIMULATED reliability and p95 latency; money comes from the quote."""
    return ScoringInput(
        route_id=candidate.route_id,
        reliability=primary.simulated_success_probability,
        incremental_fee_gbp=candidate.quote.incremental_fee.amount,
        latency_seconds=(
            primary.p95_latency_seconds
            if primary.p95_latency_seconds is not None
            else LATENCY_CEILING_SECONDS
        ),
        dependency_count=candidate.rail.dependency_count,
    )


def ranking_key(total_score: float, candidate: CandidateRecoveryRoute) -> tuple:
    """Deterministic order: score desc, then cheaper, then faster quote, then route id."""
    return (
        -total_score,
        candidate.quote.incremental_fee.amount,
        candidate.quote.expected_latency_seconds,
        candidate.route_id,
    )


def evaluate_candidates(
    candidates: tuple[CandidateRecoveryRoute, ...],
    config: SimulationConfig,
    backend_name: str,
) -> tuple[RouteEvaluation, ...]:
    """Passing routes first (rank 1 = best), then rejected routes."""
    rejected: list[RouteEvaluation] = []
    survivors = []
    for c in candidates:
        common = {
            "route_id": c.route_id,
            "rail_id": c.rail.rail_id,
            "estimated_incremental_cost": c.quote.incremental_fee,
            "expected_latency_seconds": c.quote.expected_latency_seconds,
            "quoted_reliability": c.quote.quoted_reliability,
            "compute_backend": backend_name,
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
        results = tuple(
            simulate_route(
                c.route_id,
                c.simulation_profile,
                scenario,
                seed=config.seed,
                trials=config.trials_per_scenario,
                sla_seconds=config.sla_seconds,
            )
            for scenario in config.scenarios
        )
        score = score_route(scoring_input(c, results[0]))
        survivors.append((ranking_key(score.total, c), score, results, common))

    survivors.sort(key=lambda s: s[0])
    passed = [
        RouteEvaluation(
            hard_constraint_status=HardConstraintStatus.PASSED,
            score=score,
            rank=rank,
            scenario_results=results,
            simulated_success_probability=results[0].simulated_success_probability,
            p50_latency_seconds=results[0].p50_latency_seconds,
            p95_latency_seconds=results[0].p95_latency_seconds,
            simulated_within_sla_probability=results[0].recovery_within_sla_probability,
            **common,
        )
        for rank, (_, score, results, common) in enumerate(survivors, start=1)
    ]
    return (*passed, *rejected)
