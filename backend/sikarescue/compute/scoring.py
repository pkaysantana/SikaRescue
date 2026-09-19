"""Pure, deterministic route scoring. Shared by every compute backend.

SYNTHETIC HACKATHON SCORING MODEL: weights and reference bounds are illustrative, not a
real production routing or treasury policy. Only routes that already passed every hard
constraint are ever scored.

Inputs (see `compute.evaluation.scoring_input`):
  reliability   <- SIMULATED success probability (primary scenario)
  latency       <- SIMULATED p95 latency to recipient credit (tail-aware)
  cost          <- quoted incremental fee (deterministic money, never simulated)
  route quality <- number of external dependencies on the payout path

Each component is normalised to [0, 1] against FIXED reference bounds (not min-max over
the candidate set), so adding or removing a route never changes another route's score.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sikarescue.models import ScoreBreakdown

WEIGHTS = {"reliability": 0.40, "cost": 0.25, "latency": 0.20, "route_quality": 0.15}
RELIABILITY_FLOOR = 0.90  # reliability <= floor scores 0; 1.0 scores 1
COST_CEILING_GBP = Decimal("1.00")  # incremental fee >= ceiling scores 0
LATENCY_CEILING_SECONDS = 300.0  # latency >= ceiling scores 0
QUALITY_PENALTY_PER_EXTRA_HOP = 0.25  # each intermediary beyond the first


@dataclass(frozen=True)
class ScoringInput:
    route_id: str
    reliability: float
    incremental_fee_gbp: Decimal
    latency_seconds: float
    dependency_count: int


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def score_route(inp: ScoringInput) -> ScoreBreakdown:
    reliability = _clamp((inp.reliability - RELIABILITY_FLOOR) / (1.0 - RELIABILITY_FLOOR))
    cost = _clamp(float(1 - inp.incremental_fee_gbp / COST_CEILING_GBP))
    latency = _clamp(1.0 - inp.latency_seconds / LATENCY_CEILING_SECONDS)
    quality = _clamp(1.0 - QUALITY_PENALTY_PER_EXTRA_HOP * (inp.dependency_count - 1))
    total = (
        WEIGHTS["reliability"] * reliability
        + WEIGHTS["cost"] * cost
        + WEIGHTS["latency"] * latency
        + WEIGHTS["route_quality"] * quality
    )
    return ScoreBreakdown(
        reliability=round(reliability, 4),
        cost=round(cost, 4),
        latency=round(latency, 4),
        route_quality=round(quality, 4),
        total=round(total, 4),
    )
