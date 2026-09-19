"""Pure route evaluation: hard filters -> simulate survivors -> score -> rank.

Every backend uses the same three steps; they differ only in WHERE the simulations run:
  1. `split_candidates`      - hard constraints, in-process, before any compute;
  2. simulate the survivors  - in-process (local) or fanned out (Modal);
  3. `assemble_evaluations`  - score and rank from the simulation results.
A route that fails any hard constraint is never simulated, scored or ranked.
"""

from __future__ import annotations

from collections.abc import Mapping

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


def _common_fields(c: CandidateRecoveryRoute, backend_name: str) -> dict:
    return {
        "route_id": c.route_id,
        "rail_id": c.rail.rail_id,
        "estimated_incremental_cost": c.quote.incremental_fee,
        "expected_latency_seconds": c.quote.expected_latency_seconds,
        "quoted_reliability": c.quote.quoted_reliability,
        "compute_backend": backend_name,
    }


def split_candidates(
    candidates: tuple[CandidateRecoveryRoute, ...], backend_name: str
) -> tuple[tuple[CandidateRecoveryRoute, ...], tuple[RouteEvaluation, ...]]:
    """Step 1: (survivors to simulate, rejected evaluations). Runs before any compute."""
    survivors: list[CandidateRecoveryRoute] = []
    rejected: list[RouteEvaluation] = []
    for c in candidates:
        violations = hard_constraint_violations(c)
        if not violations:
            survivors.append(c)
            continue
        rejected.append(
            RouteEvaluation(
                hard_constraint_status=HardConstraintStatus.REJECTED,
                rejection_reasons=tuple(r for r, _ in violations),
                rejection_details=tuple(d for _, d in violations),
                **_common_fields(c, backend_name),
            )
        )
    return tuple(survivors), tuple(rejected)


def assemble_evaluations(
    survivors: tuple[CandidateRecoveryRoute, ...],
    rejected: tuple[RouteEvaluation, ...],
    results_by_route: Mapping[str, tuple[RouteSimulationResult, ...]],
    backend_name: str,
) -> tuple[RouteEvaluation, ...]:
    """Step 3: score and rank survivors. Passing routes first (rank 1 = best), then rejected."""
    scored = []
    for c in survivors:
        results = results_by_route[c.route_id]
        score = score_route(scoring_input(c, results[0]))
        scored.append((ranking_key(score.total, c), score, results, c))
    scored.sort(key=lambda s: s[0])
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
            **_common_fields(c, backend_name),
        )
        for rank, (_, score, results, c) in enumerate(scored, start=1)
    ]
    return (*passed, *rejected)


def evaluate_candidates(
    candidates: tuple[CandidateRecoveryRoute, ...],
    config: SimulationConfig,
    backend_name: str,
) -> tuple[RouteEvaluation, ...]:
    """In-process reference implementation of all three steps."""
    survivors, rejected = split_candidates(candidates, backend_name)
    results_by_route = {
        c.route_id: tuple(
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
        for c in survivors
    }
    return assemble_evaluations(survivors, rejected, results_by_route, backend_name)
