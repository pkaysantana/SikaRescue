"""Candidate recovery routes and their deterministic evaluations."""

from __future__ import annotations

from pydantic import Field, model_validator

from sikarescue.models.common import DomainModel, Money, Probability, RouteId, TransactionId
from sikarescue.models.enums import FundsLocation, HardConstraintStatus, RailId, RejectionReason
from sikarescue.models.rails import LiquidityStatus, PolicyDecision, Rail, RailQuote


def route_id_for(rail_id: RailId) -> str:
    return f"rt_{rail_id.value}"


class CandidateRecoveryRoute(DomainModel):
    """A way to satisfy the outstanding obligation: source account -> payout rail -> recipient."""

    route_id: RouteId
    transaction_id: TransactionId
    rail: Rail
    source: FundsLocation
    amount: Money
    quote: RailQuote
    policy: PolicyDecision
    liquidity: LiquidityStatus
    recipient_compatible: bool
    is_original_rail: bool
    failed_earlier_for_transaction: bool = False

    @model_validator(mode="after")
    def _single_rail(self) -> CandidateRecoveryRoute:
        rail_id = self.rail.rail_id
        if {self.quote.rail_id, self.policy.rail_id, self.liquidity.rail_id} != {rail_id}:
            raise ValueError("quote, policy and liquidity must all describe the route's rail")
        if self.route_id != route_id_for(rail_id):
            raise ValueError("route_id must be derived from the rail id")
        return self


class ScoreBreakdown(DomainModel):
    """Synthetic hackathon scoring model. Each component is normalised to [0, 1]."""

    reliability: Probability
    cost: Probability
    latency: Probability
    route_quality: Probability
    total: Probability


class RouteEvaluation(DomainModel):
    route_id: RouteId
    rail_id: RailId
    hard_constraint_status: HardConstraintStatus
    rejection_reasons: tuple[RejectionReason, ...] = ()
    rejection_details: tuple[str, ...] = ()
    estimated_incremental_cost: Money
    expected_latency_seconds: int = Field(gt=0)
    quoted_reliability: Probability
    # Filled by the simulation backend (Phase 3+); None when not simulated.
    simulated_success_probability: Probability | None = None
    p50_latency_seconds: float | None = Field(default=None, gt=0)
    p95_latency_seconds: float | None = Field(default=None, gt=0)
    score: ScoreBreakdown | None = None
    rank: int | None = Field(default=None, ge=1)
    compute_backend: str = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _check_status(self) -> RouteEvaluation:
        if self.hard_constraint_status is HardConstraintStatus.REJECTED:
            if not self.rejection_reasons:
                raise ValueError("a rejected route must state at least one rejection reason")
            if self.score is not None or self.rank is not None:
                raise ValueError("rejected routes are never scored or ranked")
        else:
            if self.rejection_reasons:
                raise ValueError("a passing route cannot carry rejection reasons")
            if self.score is None or self.rank is None:
                raise ValueError("a passing route must be scored and ranked")
        if (
            self.p50_latency_seconds is not None
            and self.p95_latency_seconds is not None
            and self.p95_latency_seconds < self.p50_latency_seconds
        ):
            raise ValueError("p95 latency cannot be below p50")
        return self

    @property
    def passed(self) -> bool:
        return self.hard_constraint_status is HardConstraintStatus.PASSED
