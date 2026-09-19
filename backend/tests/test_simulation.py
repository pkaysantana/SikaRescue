"""Pure seeded simulation kernel: reproducible, shardable, well-formed (synthetic data)."""

from __future__ import annotations

import pytest

from sikarescue.compute.scenarios import SCENARIOS
from sikarescue.compute.simulation import TrialRandom, run_trials, simulate_route, summarise
from sikarescue.demo_data.sk10421 import build_simulation_profiles
from sikarescue.models import RailId, ScenarioId

PROFILES = {p.rail_id: p for p in build_simulation_profiles()}
MOMO_B = PROFILES[RailId.MOMO_B]
NORMAL = SCENARIOS[ScenarioId.NORMAL]
KW = {"seed": 10421, "sla_seconds": 120.0}


def _sim(profile=MOMO_B, scenario=NORMAL, trials=2000, **kw):
    return simulate_route(f"rt_{profile.rail_id}", profile, scenario, trials=trials, **(KW | kw))


def test_same_seed_and_input_is_reproducible():
    assert _sim() == _sim()


def test_different_seed_changes_the_draws():
    assert _sim(seed=1) != _sim(seed=2)


def test_trial_randomness_is_counter_based():
    """Draw k of trial t depends only on (key, t, k), never on other trials."""
    a, b = TrialRandom(42, 7), TrialRandom(42, 7)
    assert [a.uniform() for _ in range(5)] == [b.uniform() for _ in range(5)]
    assert TrialRandom(42, 7).uniform() != TrialRandom(42, 8).uniform()


def test_sharded_trials_merge_to_the_single_run_result():
    """Deterministic by design: any split of [0, n) gives the identical result."""
    route, n = "rt_MOMO_B", 3000
    shards = [(1800, 3000), (0, 250), (250, 1800)]
    batches = [
        run_trials(route, MOMO_B, NORMAL, start=start, stop=stop, **KW) for start, stop in shards
    ]
    assert summarise(batches) == _sim(trials=n)


@pytest.mark.parametrize("shards", [[(0, 100), (150, 300)], [(0, 200), (100, 300)]])
def test_summarise_rejects_gaps_and_overlaps(shards):
    batches = [run_trials("rt_MOMO_B", MOMO_B, NORMAL, start=a, stop=b, **KW) for a, b in shards]
    with pytest.raises(ValueError, match="contiguously"):
        summarise(batches)


def test_result_fields_are_populated_and_consistent():
    result = _sim()
    assert result.route_id == "rt_MOMO_B"
    assert result.scenario.scenario_id is ScenarioId.NORMAL
    assert result.simulation_count == 2000
    assert 0 < result.successful_runs <= 2000
    assert result.simulated_success_probability == round(result.successful_runs / 2000, 4)
    assert 0 < result.p50_latency_seconds <= result.p95_latency_seconds
    assert result.recovery_within_sla_probability <= result.simulated_success_probability
    assert (result.seed, result.sla_seconds, result.synthetic) == (10421, 120.0, True)


def test_normal_scenario_lands_near_the_calibrated_reliability():
    """Loose bound: 2,000 trials has ~0.3pp standard error around the ~98.1% calibration."""
    assert 0.97 <= _sim().simulated_success_probability <= 0.995


def test_stress_scenarios_degrade_the_route():
    normal = _sim()
    congested = _sim(scenario=SCENARIOS[ScenarioId.CONGESTION])
    correlated = _sim(scenario=SCENARIOS[ScenarioId.CORRELATED_FAILURE])
    assert congested.p95_latency_seconds > normal.p95_latency_seconds
    assert congested.recovery_within_sla_probability < normal.recovery_within_sla_probability
    assert correlated.simulated_success_probability < normal.simulated_success_probability


def test_every_catalog_scenario_produces_a_valid_result():
    for scenario in SCENARIOS.values():
        result = _sim(scenario=scenario, trials=200)
        assert result.scenario == scenario and result.simulation_count == 200
