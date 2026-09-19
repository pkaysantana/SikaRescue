"""RecoveryAdvice: the structured explanation shown to the human approver.

Produced either by the Pydantic AI agent (validated field-by-field against the deterministic
plan) or by deterministic code. It is DISPLAY-ONLY: nothing executable is ever derived from
it. The plan that can be approved and executed always comes from the recovery service.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from sikarescue.models.common import DomainModel, EntityId, Probability, TransactionId
from sikarescue.models.enums import AdviceReasonCode, FundsLocation, RailId, RejectionReason

Narrative = Annotated[str, StringConstraints(min_length=1, max_length=600)]


class RejectedRouteAdvice(DomainModel):
    rail_id: RailId = Field(description="A rejected route, copied from rejected_routes.")
    reasons: tuple[RejectionReason, ...] = Field(
        min_length=1, description="Its deterministic rejection reasons, copied exactly."
    )
    explanation: Annotated[str, StringConstraints(min_length=1, max_length=240)] = Field(
        description="One sentence in plain English explaining why this route was rejected."
    )


class RecoveryAdvice(DomainModel):
    """Advice for the human approver. Copy identifiers and numbers EXACTLY from tool results."""

    transaction_id: TransactionId
    plan_id: EntityId = Field(description="The deterministic plan's id (selected_plan.plan_id).")
    incident_summary: Narrative = Field(description="What failed and what already succeeded.")
    funds_location: FundsLocation = Field(description="Where the funds are now.")
    why_origin_retry_is_unsafe: Narrative = Field(
        description="Why restarting the payment from the sender would be unsafe."
    )
    recommended_route: RailId = Field(description="MUST equal the deterministic plan's rail_id.")
    reason_codes: tuple[AdviceReasonCode, ...] = Field(
        min_length=1, description="A subset of the reason_codes supplied by the analysis."
    )
    rejected_routes: tuple[RejectedRouteAdvice, ...] = Field(
        description="Every rejected route, each with its exact deterministic reasons."
    )
    incremental_fee_gbp: Annotated[str, StringConstraints(pattern=r"^\d+\.\d{2}$")] = Field(
        description="The selected plan's incremental_fee_gbp, copied exactly, e.g. '0.18'."
    )
    estimated_arrival_seconds: int = Field(
        gt=0, description="The selected plan's quoted_arrival_seconds, copied exactly."
    )
    simulated_reliability: Probability = Field(
        description="The selected plan's simulated_reliability, copied exactly."
    )
    stress_summary: Narrative = Field(
        description="How the recommended route held up across the simulated stress scenarios."
    )
    approval_required: Literal[True] = Field(
        default=True, description="Always true: a human must approve the exact plan."
    )
    operator_message: Narrative = Field(
        description="At most three sentences for the approver: the action and why it is safe."
    )
