"""Rails, quotes, liquidity and policy decisions (all synthetic demo data)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, computed_field, model_validator

from sikarescue.models.common import DomainModel, EntityId, Money, Probability
from sikarescue.models.enums import EndpointType, FundsLocation, RailId, RailStatus, RailType


class Rail(DomainModel):
    rail_id: RailId
    display_name: str = Field(max_length=64)
    rail_type: RailType
    status: RailStatus
    supported_endpoints: frozenset[EndpointType]
    # External intermediaries between the settlement account and the recipient.
    dependency_count: int = Field(ge=1, le=10)
    # For payout rails: the account the rail pays out from.
    payout_source: FundsLocation | None = None
    synthetic: Literal[True] = True


class RailQuote(DomainModel):
    quote_id: EntityId
    rail_id: RailId
    # Cost to the operator of using this rail for the remaining leg (GBP equivalent).
    incremental_fee: Money
    expected_latency_seconds: int = Field(gt=0, le=86_400)
    # Synthetic, provider-quoted success rate. NOT an empirical measurement.
    quoted_reliability: Probability
    synthetic: Literal[True] = True


class LiquidityStatus(DomainModel):
    rail_id: RailId
    available: Money
    required: Money

    @model_validator(mode="after")
    def _same_currency(self) -> LiquidityStatus:
        if self.available.currency != self.required.currency:
            raise ValueError("liquidity must be compared in one currency")
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sufficient(self) -> bool:
        return self.available.amount >= self.required.amount


class PolicyRuleResult(DomainModel):
    rule_id: str = Field(pattern=r"^POL-\d{3}$")
    passed: bool
    detail: str = Field(max_length=200)


class PolicyDecision(DomainModel):
    rail_id: RailId
    permitted: bool
    policy_version: str
    rule_results: tuple[PolicyRuleResult, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _permitted_matches_rules(self) -> PolicyDecision:
        if self.permitted != all(r.passed for r in self.rule_results):
            raise ValueError("permitted must be true iff every rule passed")
        return self

    @property
    def denials(self) -> tuple[PolicyRuleResult, ...]:
        return tuple(r for r in self.rule_results if not r.passed)
