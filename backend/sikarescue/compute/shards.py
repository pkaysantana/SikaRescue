"""Shard protocol for distributed simulation (used by the Modal backend). Pure: no Modal import.

A shard is one surviving route x one scenario x one contiguous trial range. Only simulation
inputs cross the boundary (the synthetic rail profile, scenario, seed, trial range) - never
policy, liquidity, money or transaction state. Results come back as strictly validated
records; `merge_shards` rejects anything malformed or inconsistent with what was requested.
"""

from __future__ import annotations

import math
import platform
import struct
import time
from collections import defaultdict
from collections.abc import Sequence
from typing import Annotated

from pydantic import Field, StringConstraints, ValidationError, model_validator

from sikarescue.compute.simulation import TrialBatch, run_trials, summarise
from sikarescue.errors import ComputeIntegrityError
from sikarescue.models import (
    CandidateRecoveryRoute,
    DomainModel,
    OperatingScenario,
    RouteSimulationProfile,
    RouteSimulationResult,
    ScenarioId,
    SimulationConfig,
)
from sikarescue.models.common import RouteId

MODAL_APP_NAME = "sikarescue-compute"
MODAL_FUNCTION_NAME = "simulate_shard"

ShardId = Annotated[str, StringConstraints(pattern=r"^rt_[A-Z0-9_]+\|[A-Z_]+\|\d{1,4}$")]
_LATENCY_FORMAT = "<{n}d"  # little-endian float64, independent of either side's byte order


class ShardSpec(DomainModel):
    shard_id: ShardId
    route_id: RouteId
    profile: RouteSimulationProfile
    scenario: OperatingScenario
    seed: int = Field(ge=0)
    start: int = Field(ge=0)
    stop: int = Field(gt=0)
    sla_seconds: float = Field(gt=0)

    @model_validator(mode="after")
    def _non_empty(self) -> ShardSpec:
        if self.stop <= self.start:
            raise ValueError("a shard must contain at least one trial")
        return self


class ShardResult(DomainModel):
    shard_id: ShardId
    route_id: RouteId
    scenario_id: ScenarioId
    start: int = Field(ge=0)
    stop: int = Field(gt=0)
    successes: int = Field(ge=0)
    within_sla: int = Field(ge=0)
    latencies_f64le: bytes  # latency of every successful trial, packed
    compute_seconds: float = Field(ge=0)
    runtime: str = Field(max_length=80)

    @model_validator(mode="after")
    def _consistent(self) -> ShardResult:
        if self.successes > self.stop - self.start or self.within_sla > self.successes:
            raise ValueError("counts are inconsistent with the trial range")
        if len(self.latencies_f64le) != 8 * self.successes:
            raise ValueError("one latency is required per successful trial")
        return self


def _ranges(trials: int, shards: int) -> list[tuple[int, int]]:
    shards = max(1, min(shards, trials))
    base, extra = divmod(trials, shards)
    ranges, start = [], 0
    for i in range(shards):
        stop = start + base + (1 if i < extra else 0)
        ranges.append((start, stop))
        start = stop
    return ranges


def plan_shards(
    survivors: Sequence[CandidateRecoveryRoute], config: SimulationConfig, shards_per_scenario: int
) -> tuple[ShardSpec, ...]:
    """Only routes that already passed every hard constraint are ever sent for simulation."""
    return tuple(
        ShardSpec(
            shard_id=f"{c.route_id}|{scenario.scenario_id}|{index}",
            route_id=c.route_id,
            profile=c.simulation_profile,
            scenario=scenario,
            seed=config.seed,
            start=start,
            stop=stop,
            sla_seconds=config.sla_seconds,
        )
        for c in survivors
        for scenario in config.scenarios
        for index, (start, stop) in enumerate(
            _ranges(config.trials_per_scenario, shards_per_scenario)
        )
    )


def run_shard(spec: ShardSpec) -> ShardResult:
    """Executed remotely (or locally in tests): the only thing a compute worker does."""
    started = time.perf_counter()
    batch = run_trials(
        spec.route_id,
        spec.profile,
        spec.scenario,
        seed=spec.seed,
        start=spec.start,
        stop=spec.stop,
        sla_seconds=spec.sla_seconds,
    )
    latencies = batch.success_latencies
    return ShardResult(
        shard_id=spec.shard_id,
        route_id=spec.route_id,
        scenario_id=spec.scenario.scenario_id,
        start=spec.start,
        stop=spec.stop,
        successes=batch.successes,
        within_sla=batch.within_sla,
        latencies_f64le=struct.pack(_LATENCY_FORMAT.format(n=len(latencies)), *latencies),
        compute_seconds=round(time.perf_counter() - started, 4),
        runtime=f"{platform.python_implementation()} {platform.python_version()} "
        f"{platform.system()} {platform.machine()}",
    )


def merge_shards(
    specs: Sequence[ShardSpec], raw_results: Sequence[object], config: SimulationConfig
) -> tuple[dict[str, tuple[RouteSimulationResult, ...]], float]:
    """Validate every shard result against its spec, then merge per route x scenario.

    Returns (results per route in config scenario order, summed remote compute seconds).
    Raises ComputeIntegrityError on anything malformed, missing, extra or inconsistent.
    """
    if len(raw_results) != len(specs):
        problem = f"expected {len(specs)} shard results, got {len(raw_results)}"
        raise ComputeIntegrityError([problem])
    batches: dict[tuple[str, ScenarioId], list[TrialBatch]] = defaultdict(list)
    compute_seconds = 0.0
    for spec, raw in zip(specs, raw_results, strict=True):
        try:
            result = ShardResult.model_validate(raw)
        except ValidationError as exc:
            problem = f"malformed shard result for {spec.shard_id}: {exc.error_count()} error(s)"
            raise ComputeIntegrityError([problem]) from exc
        identity = (result.shard_id, result.route_id, result.scenario_id, result.start, result.stop)
        if identity != (
            spec.shard_id,
            spec.route_id,
            spec.scenario.scenario_id,
            spec.start,
            spec.stop,
        ):
            raise ComputeIntegrityError([f"shard result does not match request {spec.shard_id}"])
        latencies = struct.unpack(
            _LATENCY_FORMAT.format(n=result.successes), result.latencies_f64le
        )
        if not all(math.isfinite(x) and x >= 0 for x in latencies):
            raise ComputeIntegrityError([f"non-finite or negative latency in {spec.shard_id}"])
        if sum(1 for x in latencies if x <= spec.sla_seconds) != result.within_sla:
            raise ComputeIntegrityError([f"within-SLA count inconsistent in {spec.shard_id}"])
        compute_seconds += result.compute_seconds
        batches[(spec.route_id, spec.scenario.scenario_id)].append(
            TrialBatch(
                route_id=spec.route_id,
                scenario=spec.scenario,
                seed=spec.seed,
                sla_seconds=spec.sla_seconds,
                start=spec.start,
                stop=spec.stop,
                successes=result.successes,
                within_sla=result.within_sla,
                success_latencies=latencies,
            )
        )
    route_ids = list(dict.fromkeys(spec.route_id for spec in specs))
    try:
        merged = {
            route_id: tuple(
                summarise(batches[(route_id, scenario.scenario_id)])
                for scenario in config.scenarios
            )
            for route_id in route_ids
        }
    except ValueError as exc:  # gaps/overlaps/mismatched batches
        raise ComputeIntegrityError([f"shard coverage invalid: {exc}"]) from exc
    return merged, round(compute_seconds, 4)
