"""Catalog of SYNTHETIC operating scenarios (illustrative stress regimes, not measurements)."""

from __future__ import annotations

from sikarescue.models import OperatingScenario, ScenarioId, SimulationConfig

SCENARIOS: dict[ScenarioId, OperatingScenario] = {
    s.scenario_id: s
    for s in (
        OperatingScenario(
            scenario_id=ScenarioId.NORMAL,
            description="Baseline synthetic operating conditions",
            failure_multiplier=1.0,
            latency_multiplier=1.0,
        ),
        OperatingScenario(
            scenario_id=ScenarioId.CONGESTION,
            description="Peak-hour congestion: slower, slightly less reliable",
            failure_multiplier=1.3,
            latency_multiplier=1.6,
            extra_outage_probability=0.02,
        ),
        OperatingScenario(
            scenario_id=ScenarioId.RAIL_DEGRADATION,
            description="The payout rail degrades: failures and outages rise",
            failure_multiplier=2.5,
            latency_multiplier=1.1,
            extra_outage_probability=0.05,
        ),
        OperatingScenario(
            scenario_id=ScenarioId.RAIL_OUTAGE,
            description="Intermittent rail outages: frequent wait-and-retry cycles",
            failure_multiplier=1.0,
            latency_multiplier=1.0,
            extra_outage_probability=0.30,
        ),
        OperatingScenario(
            scenario_id=ScenarioId.LATENCY_SPIKE,
            description="Provider latency spike",
            failure_multiplier=1.0,
            latency_multiplier=2.5,
        ),
        OperatingScenario(
            scenario_id=ScenarioId.REGIONAL_DISRUPTION,
            description="Regional network disruption in the destination market",
            failure_multiplier=1.5,
            latency_multiplier=1.4,
            extra_outage_probability=0.12,
        ),
        OperatingScenario(
            scenario_id=ScenarioId.CORRELATED_FAILURE,
            description="Shared-dependency shock hits every hop of a route at once",
            failure_multiplier=1.0,
            latency_multiplier=1.0,
            correlated_shock_probability=0.04,
            shock_failure_probability=0.7,
        ),
    )
}

# Workload presets. The first scenario is always the primary (scoring) scenario.
#   quick  - small local subset for fast interactive runs and tests
#   stress - every route stress-tested across six adverse regimes (Modal fan-out)
LOCAL_SCENARIOS = (ScenarioId.NORMAL, ScenarioId.CONGESTION, ScenarioId.CORRELATED_FAILURE)
STRESS_SCENARIOS = (
    ScenarioId.NORMAL,
    ScenarioId.CONGESTION,
    ScenarioId.RAIL_DEGRADATION,
    ScenarioId.RAIL_OUTAGE,
    ScenarioId.LATENCY_SPIKE,
    ScenarioId.CORRELATED_FAILURE,
)
DEFAULT_SEED = 10421
DEFAULT_TRIALS_PER_SCENARIO = 2000
STRESS_TRIALS_PER_SCENARIO = 50_000
WORKLOADS: dict[str, tuple[tuple[ScenarioId, ...], int]] = {
    "quick": (LOCAL_SCENARIOS, DEFAULT_TRIALS_PER_SCENARIO),
    "stress": (STRESS_SCENARIOS, STRESS_TRIALS_PER_SCENARIO),
}
# Synthetic recovery SLA: recipient credited within this many seconds of execution.
DEFAULT_SLA_SECONDS = 120.0


def workload_config(
    workload: str, *, seed: int = DEFAULT_SEED, trials_per_scenario: int | None = None
) -> SimulationConfig:
    scenarios, default_trials = WORKLOADS[workload]
    return simulation_config(
        seed=seed, trials_per_scenario=trials_per_scenario or default_trials, scenarios=scenarios
    )


def simulation_config(
    *,
    seed: int = DEFAULT_SEED,
    trials_per_scenario: int = DEFAULT_TRIALS_PER_SCENARIO,
    scenarios: tuple[ScenarioId, ...] = LOCAL_SCENARIOS,
    sla_seconds: float = DEFAULT_SLA_SECONDS,
) -> SimulationConfig:
    """Build a config. The first scenario is the primary one used for scoring."""
    return SimulationConfig(
        seed=seed,
        trials_per_scenario=trials_per_scenario,
        scenarios=tuple(SCENARIOS[s] for s in scenarios),
        sla_seconds=sla_seconds,
    )
