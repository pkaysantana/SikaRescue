"""Independent verification of compute-backend output (pure; no repository access).

Everything except the Monte Carlo statistics is re-derived locally from the candidates, so a
backend (local, remote, buggy or malicious) can never admit a policy-denied / down /
incompatible / illiquid route or alter a fee or amount. Scores and ranks are recomputed from
the returned statistics and must match; the statistics themselves are NOT reproduced locally,
so a forged but internally consistent result could change the order of eligible routes.
"""

from __future__ import annotations

from sikarescue.compute.constraints import hard_constraint_violations
from sikarescue.compute.evaluation import ranking_key, scoring_input
from sikarescue.compute.scoring import score_route
from sikarescue.models import CandidateRecoveryRoute, RouteEvaluation, SimulationConfig


def verify_evaluations(
    candidates: tuple[CandidateRecoveryRoute, ...],
    evaluations: tuple[RouteEvaluation, ...],
    config: SimulationConfig | None = None,
) -> list[str]:
    """Empty list = trustworthy. With `config`, also checks the simulation matches the request."""
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
        if not e.passed:
            continue
        if config is not None:
            problems.extend(_simulation_matches_config(e, config))
        recomputed = score_route(scoring_input(c, e.scenario_results[0]))
        if recomputed != e.score:
            problems.append(f"{rail_id}: score differs from local recomputation")
        assert e.rank is not None
        passing.append((ranking_key(recomputed.total, c), e.rank))
    ranks_in_expected_order = [rank for _, rank in sorted(passing)]
    if ranks_in_expected_order != list(range(1, len(passing) + 1)):
        problems.append("ranking differs from local recomputation")
    return problems


def _simulation_matches_config(e: RouteEvaluation, config: SimulationConfig) -> list[str]:
    expected_ids = [s.scenario_id for s in config.scenarios]
    if [r.scenario.scenario_id for r in e.scenario_results] != expected_ids:
        return [f"{e.rail_id}: scenarios differ from the request"]
    problems = []
    for r, scenario in zip(e.scenario_results, config.scenarios, strict=True):
        if r.scenario != scenario:
            problems.append(f"{e.rail_id}: scenario {scenario.scenario_id} parameters altered")
        if (r.simulation_count, r.seed, r.sla_seconds) != (
            config.trials_per_scenario,
            config.seed,
            config.sla_seconds,
        ):
            problems.append(f"{e.rail_id}: trial count, seed or SLA differs from the request")
    return problems
