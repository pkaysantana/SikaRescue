"""SYNTHETIC reliability-simulation models: rail behaviour profiles, scenarios, results.

Every parameter here is illustrative demo data. Simulated probabilities describe this
synthetic model, NOT empirical measurements of any real payment rail.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from sikarescue.models.common import DomainModel, Probability, RouteId
from sikarescue.models.enums import RailId, ScenarioId


class OperatingScenario(DomainModel):
    """Adjusts every hop of a route for one synthetic operating regime."""

    scenario_id: ScenarioId
    description: str = Field(max_length=120)
    failure_multiplier: float = Field(ge=0, le=20)
    latency_multiplier: float = Field(gt=0, le=20)
    extra_outage_probability: Probability = 0.0
    # Per-trial shared shock that hits every hop at once (correlated failure).
    correlated_shock_probability: Probability = 0.0
    shock_failure_probability: Probability = 0.0
    synthetic: Literal[True] = True


class HopProfile(DomainModel):
    """One external dependency on the payout path (synthetic behaviour)."""

    name: str = Field(pattern=r"^[a-z0-9_]{2,40}$")
    failure_probability: Probability
    median_latency_seconds: float = Field(gt=0, le=3600)
    latency_sigma: float = Field(ge=0, le=2)  # lognormal spread
    outage_probability: Probability  # transient outage: wait, then retry once
    outage_delay_seconds: float = Field(ge=0, le=3600)
    retry_failure_probability: Probability


class RouteSimulationProfile(DomainModel):
    rail_id: RailId
    hops: tuple[HopProfile, ...] = Field(min_length=1, max_length=10)
    synthetic: Literal[True] = True


class SimulationConfig(DomainModel):
    """What to simulate. The FIRST scenario is the primary one used for scoring."""

    seed: int = Field(ge=0, lt=2**63)
    trials_per_scenario: int = Field(ge=1, le=1_000_000)
    scenarios: tuple[OperatingScenario, ...] = Field(min_length=1)
    sla_seconds: float = Field(gt=0, le=86_400)

    @model_validator(mode="after")
    def _unique_scenarios(self) -> SimulationConfig:
        ids = [s.scenario_id for s in self.scenarios]
        if len(ids) != len(set(ids)):
            raise ValueError("scenarios must be unique")
        return self

    @property
    def primary_scenario(self) -> OperatingScenario:
        return self.scenarios[0]


class RouteSimulationResult(DomainModel):
    """Statistics for one route under one scenario."""

    route_id: RouteId
    scenario: OperatingScenario
    simulation_count: int = Field(ge=1)
    successful_runs: int = Field(ge=0)
    simulated_success_probability: Probability
    # Latency to recipient credit, over successful runs only (None if none succeeded).
    p50_latency_seconds: float | None = Field(default=None, ge=0)
    p95_latency_seconds: float | None = Field(default=None, ge=0)
    recovery_within_sla_probability: Probability
    sla_seconds: float = Field(gt=0)
    seed: int = Field(ge=0)
    synthetic: Literal[True] = True

    @model_validator(mode="after")
    def _consistent(self) -> RouteSimulationResult:
        if self.successful_runs > self.simulation_count:
            raise ValueError("successful_runs exceeds simulation_count")
        exact = self.successful_runs / self.simulation_count
        if abs(self.simulated_success_probability - exact) > 1e-4:
            raise ValueError("success probability does not match run counts")
        if self.recovery_within_sla_probability > self.simulated_success_probability + 1e-9:
            raise ValueError("within-SLA runs must be a subset of successful runs")
        has_latency = self.p50_latency_seconds is not None and self.p95_latency_seconds is not None
        if has_latency != (self.successful_runs > 0):
            raise ValueError("latency percentiles exist iff at least one run succeeded")
        if has_latency and self.p95_latency_seconds < self.p50_latency_seconds:  # type: ignore[operator]
            raise ValueError("p95 latency cannot be below p50")
        return self


class ComputeRunSummary(DomainModel):
    """Provenance of one route-evaluation compute run (reproducible from seed + inputs)."""

    backend: str = Field(min_length=1, max_length=32)
    seed: int = Field(ge=0)
    trials_per_scenario: int = Field(ge=1)
    scenarios: tuple[ScenarioId, ...] = Field(min_length=1)
    routes_simulated: int = Field(ge=0)
    simulated_trials: int = Field(ge=0)
    elapsed_seconds: float = Field(ge=0)
    synthetic: Literal[True] = True
