"""Systemic rail-outage analysis results. Every figure is recomputed locally from verified
allocations; nothing here is taken on trust from a compute worker."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from sikarescue.models.common import DomainModel, EntityId, Money, Sha256Hex
from sikarescue.models.enums import EndpointType, RailId, RailStatus, RejectionReason


class UnservedReason(StrEnum):
    POLICY_LIMIT = "POLICY_LIMIT"  # over the corridor's per-payout limit on every rail
    NO_ELIGIBLE_RAIL = "NO_ELIGIBLE_RAIL"  # no available, permitted, compatible rail
    LIQUIDITY_EXHAUSTED = "LIQUIDITY_EXHAUSTED"
    CAPACITY_EXHAUSTED = "CAPACITY_EXHAUSTED"


class OutageScenario(DomainModel):
    scenario_id: str = Field(pattern=r"^[A-Z_]{3,40}$")
    label: str = Field(max_length=60)
    description: str = Field(max_length=200)
    down_rails: tuple[RailId, ...]  # unavailable in this scenario (always incl. the failed rail)
    liquidity_factor: dict[RailId, float] = Field(default_factory=dict)
    capacity_factor: dict[RailId, float] = Field(default_factory=dict)


class FleetRail(DomainModel):
    """A payout rail's synthetic fleet-level treasury position and throughput."""

    rail_id: RailId
    liquidity: Money  # prefunded GHS available to recoveries during the outage window
    capacity: int = Field(ge=0)  # payouts the rail can accept during the outage window


class OutageRailConstraint(DomainModel):
    """Rail-level hard constraints in one scenario, decided locally before any compute."""

    rail_id: RailId
    status: RailStatus
    policy_permitted: bool
    policy_detail: str = Field(max_length=200)
    supported_endpoints: tuple[EndpointType, ...]
    liquidity: Money
    capacity: int = Field(ge=0)
    fee: Money
    eligible: bool
    rejection_reasons: tuple[RejectionReason, ...] = ()


class OutageRailAllocation(DomainModel):
    rail_id: RailId
    obligations: int = Field(ge=0)
    amount: Money
    liquidity: Money
    liquidity_utilisation: float = Field(ge=0, le=1)
    capacity: int = Field(ge=0)
    capacity_utilisation: float = Field(ge=0, le=1)
    incremental_fee: Money


class UnservedBucket(DomainModel):
    reason: UnservedReason
    obligations: int = Field(ge=0)
    amount: Money


class OutageScenarioResult(DomainModel):
    scenario: OutageScenario
    rails: tuple[OutageRailConstraint, ...]
    affected_obligations: int = Field(ge=0)
    affected_amount: Money
    recoverable_obligations: int = Field(ge=0)
    recoverable_amount: Money
    unserved_obligations: int = Field(ge=0)
    unserved_amount: Money
    unserved: tuple[UnservedBucket, ...]
    allocations: tuple[OutageRailAllocation, ...]
    aggregate_incremental_fee: Money
    compute_seconds: float = Field(ge=0)  # the allocation kernel, where it actually ran
    checks: tuple[str, ...]  # local verification checks that passed

    @model_validator(mode="after")
    def _conserved(self) -> OutageScenarioResult:
        if self.recoverable_obligations + self.unserved_obligations != self.affected_obligations:
            raise ValueError("every affected obligation is either recoverable or unserved")
        if self.recoverable_amount.amount + self.unserved_amount.amount != (
            self.affected_amount.amount
        ):
            raise ValueError("recoverable + unserved value must equal the affected value")
        if sum(a.obligations for a in self.allocations) != self.recoverable_obligations:
            raise ValueError("per-rail allocations must add up to the recoverable total")
        if sum(b.obligations for b in self.unserved) != self.unserved_obligations:
            raise ValueError("unserved buckets must add up to the unserved total")
        return self


class OutagePortfolio(DomainModel):
    seed: int = Field(ge=0)
    size: int = Field(gt=0)
    digest: Sha256Hex
    total_amount: Money
    mobile_money: int = Field(ge=0)
    bank_account: int = Field(ge=0)
    over_policy_limit: int = Field(ge=0)
    oldest_minutes: int = Field(ge=0)


class OutageAnalysis(DomainModel):
    analysis_id: EntityId
    failed_rail: RailId
    corridor: str
    portfolio: OutagePortfolio
    scenarios: tuple[OutageScenarioResult, ...]
    backend: str  # where the allocation kernel actually ran
    configured_backend: str
    fallback_from: str | None = None
    fallback_reason: str | None = None
    parallel_jobs: int = Field(ge=1)
    wall_seconds: float = Field(ge=0)  # constraints + compute + verification, end to end
    compute_wall_seconds: float = Field(ge=0)  # waiting on the backend
    kernel_seconds: float = Field(ge=0)  # sum of kernel time across all scenarios
    verification_seconds: float = Field(ge=0)
    function_ref: str | None = None
    generated_at: datetime
    synthetic: bool = True
