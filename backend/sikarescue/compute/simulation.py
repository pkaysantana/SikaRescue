"""Pure, seeded Monte Carlo kernel for SYNTHETIC payout-route reliability.

Counter-based randomness: every trial draws from its own SplitMix64 stream derived from
(seed, route_id, scenario_id, trial_index). A trial's outcome does not depend on which other
trials ran or in which process, so trials can be split into shards (e.g. across Modal
containers) and merged into the same result as a single run.

Reproducibility: success counts are bit-exact on every platform (they only compare
integer-derived uniforms). Latency statistics use math.exp/log/cos, which may differ in the
last ulp between C libraries; percentiles are rounded to 0.1 s, so cross-platform differences
are vanishingly unlikely but not impossible.

All parameters are synthetic; outputs describe this model, not any real rail.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass

from sikarescue.models import OperatingScenario, RouteSimulationProfile, RouteSimulationResult

_MASK64 = (1 << 64) - 1
_GAMMA = 0x9E3779B97F4A7C15
_INV_2_53 = 1.0 / (1 << 53)
_TWO_PI = 2.0 * math.pi


def _mix64(z: int) -> int:
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK64
    return z ^ (z >> 31)


def stream_key(seed: int, route_id: str, scenario_id: str) -> int:
    digest = hashlib.blake2b(f"{seed}|{route_id}|{scenario_id}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big")


class TrialRandom:
    """SplitMix64 stream for ONE trial: draw k of trial t is a pure function of (key, t, k)."""

    __slots__ = ("_state",)

    def __init__(self, key: int, trial_index: int) -> None:
        self._state = _mix64(key ^ _mix64(((trial_index + 1) * _GAMMA) & _MASK64))

    def uniform(self) -> float:
        """Uniform in [0, 1)."""
        self._state = (self._state + _GAMMA) & _MASK64
        return (_mix64(self._state) >> 11) * _INV_2_53

    def normal(self) -> float:
        """Standard normal (Box-Muller)."""
        u1 = 1.0 - self.uniform()  # (0, 1]
        return math.sqrt(-2.0 * math.log(u1)) * math.cos(_TWO_PI * self.uniform())


@dataclass(frozen=True, slots=True)
class _Hop:
    failure: float
    retry_failure: float
    outage: float
    median_latency: float
    sigma: float
    outage_delay: float


def _prepare(profile: RouteSimulationProfile, scenario: OperatingScenario) -> tuple[_Hop, ...]:
    return tuple(
        _Hop(
            failure=min(1.0, hop.failure_probability * scenario.failure_multiplier),
            retry_failure=min(1.0, hop.retry_failure_probability * scenario.failure_multiplier),
            outage=min(1.0, hop.outage_probability + scenario.extra_outage_probability),
            median_latency=hop.median_latency_seconds * scenario.latency_multiplier,
            sigma=hop.latency_sigma,
            outage_delay=hop.outage_delay_seconds * scenario.latency_multiplier,
        )
        for hop in profile.hops
    )


def _run_trial(
    rng: TrialRandom, hops: tuple[_Hop, ...], shock_probability: float, shock_failure: float
) -> tuple[bool, float]:
    """One synthetic execution: returns (recipient credited?, seconds elapsed)."""
    shocked = shock_probability > 0.0 and rng.uniform() < shock_probability
    elapsed = 0.0
    for hop in hops:
        failure = hop.failure
        elapsed += hop.median_latency * math.exp(hop.sigma * rng.normal())
        if rng.uniform() < hop.outage:  # transient outage: wait, retry once
            elapsed += hop.outage_delay
            failure = max(failure, hop.retry_failure)
        if shocked:
            failure = max(failure, shock_failure)
        if rng.uniform() < failure:
            return False, elapsed
    return True, elapsed


@dataclass(frozen=True)
class TrialBatch:
    """Mergeable partial result for trials [start, stop) of one route under one scenario."""

    route_id: str
    scenario: OperatingScenario
    seed: int
    sla_seconds: float
    start: int
    stop: int
    successes: int
    within_sla: int
    success_latencies: tuple[float, ...]


def run_trials(
    route_id: str,
    profile: RouteSimulationProfile,
    scenario: OperatingScenario,
    *,
    seed: int,
    start: int,
    stop: int,
    sla_seconds: float,
) -> TrialBatch:
    if not 0 <= start <= stop:
        raise ValueError("invalid trial range")
    key = stream_key(seed, route_id, scenario.scenario_id.value)
    hops = _prepare(profile, scenario)
    shock_p, shock_fail = scenario.correlated_shock_probability, scenario.shock_failure_probability
    successes = within_sla = 0
    latencies: list[float] = []
    for trial in range(start, stop):
        ok, latency = _run_trial(TrialRandom(key, trial), hops, shock_p, shock_fail)
        if ok:
            successes += 1
            latencies.append(latency)
            if latency <= sla_seconds:
                within_sla += 1
    return TrialBatch(
        route_id=route_id,
        scenario=scenario,
        seed=seed,
        sla_seconds=sla_seconds,
        start=start,
        stop=stop,
        successes=successes,
        within_sla=within_sla,
        success_latencies=tuple(latencies),
    )


def _percentile(sorted_values: list[float], q: float) -> float | None:
    """Nearest-rank percentile, rounded to 0.1 s."""
    if not sorted_values:
        return None
    index = max(0, math.ceil(q * len(sorted_values)) - 1)
    return round(sorted_values[index], 1)


def summarise(batches: Sequence[TrialBatch]) -> RouteSimulationResult:
    """Merge contiguous, non-overlapping batches covering [0, n) into one result."""
    if not batches:
        raise ValueError("no batches to summarise")
    ordered = sorted(batches, key=lambda b: b.start)
    first = ordered[0]
    expected_start = 0
    for b in ordered:
        if (b.route_id, b.scenario, b.seed, b.sla_seconds) != (
            first.route_id,
            first.scenario,
            first.seed,
            first.sla_seconds,
        ):
            raise ValueError("batches describe different simulations")
        if b.start != expected_start:
            raise ValueError("batches must cover [0, n) contiguously without overlap")
        expected_start = b.stop
    count = expected_start
    if count == 0:
        raise ValueError("no trials were run")
    successes = sum(b.successes for b in ordered)
    within_sla = sum(b.within_sla for b in ordered)
    latencies = sorted(lat for b in ordered for lat in b.success_latencies)
    return RouteSimulationResult(
        route_id=first.route_id,
        scenario=first.scenario,
        simulation_count=count,
        successful_runs=successes,
        simulated_success_probability=round(successes / count, 4),
        p50_latency_seconds=_percentile(latencies, 0.50),
        p95_latency_seconds=_percentile(latencies, 0.95),
        recovery_within_sla_probability=round(within_sla / count, 4),
        sla_seconds=first.sla_seconds,
        seed=first.seed,
    )


def simulate_route(
    route_id: str,
    profile: RouteSimulationProfile,
    scenario: OperatingScenario,
    *,
    seed: int,
    trials: int,
    sla_seconds: float,
) -> RouteSimulationResult:
    batch = run_trials(
        route_id, profile, scenario, seed=seed, start=0, stop=trials, sla_seconds=sla_seconds
    )
    return summarise([batch])
