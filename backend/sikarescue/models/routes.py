"""Candidate recovery routes and their deterministic evaluations."""

from __future__ import annotations

from pydantic import Field, model_validator

from sikarescue.models.common import DomainModel, Money, Probability, RouteId, TransactionId
from sikarescue.models.enums import FundsLocation, HardConstraintStatus, RailId, RejectionReason
from sikarescue.models.rails import LiquidityStatus, PolicyDecision, Rail, RailQuote
from sikarescue.models.simulation import RouteSimulationProfile, RouteSimulationResult


def route_id_for(rail_id: RailId) -> str:
    return f"rt_{rail_id.value}"


class CandidateRecoveryRoute(DomainModel):
    """A way to satisfy the outstanding obligation: source account -> payout rail -> recipient.

    Self-contained: carries everything route evaluation needs (incl. the synthetic
    simulation profile), so it can be evaluated in another process without the repository.
    """

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
    simulation_profile: RouteSimulationProfile

    @model_validator(mode="after")
    def _single_rail(self) -> CandidateRecoveryRoute:
        rail_id = self.rail.rail_id
        described = {
            self.quote.rail_id,
            self.policy.rail_id,
            self.liquidity.rail_id,
            self.simulation_profile.rail_id,
        }
        if described != {rail_id}:
            raise ValueError("quote, policy, liquidity and profile must describe the route's rail")
        if self.route_id != route_id_for(rail_id):
            raise ValueError("route_id must be derived from the rail id")
        if len(self.simulation_profile.hops) != self.rail.dependency_count:
            raise ValueError("simulation profile must model one hop per rail dependency")
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
    # Primary-scenario simulation statistics (copied from scenario_results[0]).
    # Always None for rejected routes: hard-rejected routes are never simulated.
    simulated_success_probability: Probability | None = None
    p50_latency_seconds: float | None = Field(default=None, ge=0)
    p95_latency_seconds: float | None = Field(default=None, ge=0)
    simulated_within_sla_probability: Probability | None = None
    scenario_results: tuple[RouteSimulationResult, ...] = ()
    score: ScoreBreakdown | None = None
    rank: int | None = Field(default=None, ge=1)
    compute_backend: str = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _check_status(self) -> RouteEvaluation:
        simulated_fields = (
            self.simulated_success_probability,
            self.p50_latency_seconds,
            self.p95_latency_seconds,
            self.simulated_within_sla_probability,
        )
        if self.hard_constraint_status is HardConstraintStatus.REJECTED:
            if not self.rejection_reasons:
                raise ValueError("a rejected route must state at least one rejection reason")
            if self.score is not None or self.rank is not None:
                raise ValueError("rejected routes are never scored or ranked")
            if self.scenario_results or any(v is not None for v in simulated_fields):
                raise ValueError("rejected routes are never simulated")
            return self

        if self.rejection_reasons:
            raise ValueError("a passing route cannot carry rejection reasons")
        if self.score is None or self.rank is None:
            raise ValueError("a passing route must be scored and ranked")
        if not self.scenario_results:
            raise ValueError("a passing route must carry simulation results")
        if any(r.route_id != self.route_id for r in self.scenario_results):
            raise ValueError("simulation results belong to another route")
        ids = [r.scenario.scenario_id for r in self.scenario_results]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate scenario results")
        primary = self.scenario_results[0]
        expected = (
            primary.simulated_success_probability,
            primary.p50_latency_seconds,
            primary.p95_latency_seconds,
            primary.recovery_within_sla_probability,
        )
        if simulated_fields != expected:
            raise ValueError("primary simulated fields must match the primary scenario result")
        return self

    @property
    def passed(self) -> bool:
        return self.hard_constraint_status is HardConstraintStatus.PASSED
