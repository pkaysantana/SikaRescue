"""Allowlisted, model-facing views. The ONLY transaction data an LLM may ever see.

Built field-by-field in `services.model_boundary`. They contain opaque tokens instead of
recipient identity, and sanitised failure summaries instead of raw provider payloads.
`extra="forbid"` (inherited from DomainModel) rejects any attempt to add a PII field.
"""

from __future__ import annotations

from pydantic import Field

from sikarescue.models.common import DomainModel, OpaqueToken, Probability, RouteId, TransactionId
from sikarescue.models.enums import (
    AttemptOutcome,
    Currency,
    EndpointType,
    FundsLocation,
    HardConstraintStatus,
    RailId,
    RecoveryState,
    RejectionReason,
)


class ModelLegView(DomainModel):
    rail_id: RailId
    status: str
    is_recovery: bool


class ModelTransactionView(DomainModel):
    transaction_id: TransactionId
    recipient_token: OpaqueToken
    corridor: str = Field(pattern=r"^[A-Z]{2}->[A-Z]{2}$")
    send_currency: Currency
    payout_currency: Currency
    send_amount: str
    payout_amount: str
    endpoint_type: EndpointType
    recovery_state: RecoveryState
    revision: int
    sender_debited: bool
    recipient_credited: bool
    funds_location: FundsLocation
    failed_leg: RailId | None
    last_payout_outcome: AttemptOutcome | None
    failure_summary: str | None = Field(default=None, max_length=120)
    safe_to_restart_from_origin: bool
    manual_review_required: bool
    legs: tuple[ModelLegView, ...]


class ModelRouteView(DomainModel):
    route_id: RouteId
    rail_id: RailId
    hard_constraint_status: HardConstraintStatus
    rejection_reasons: tuple[RejectionReason, ...]
    incremental_fee_gbp: str
    expected_latency_seconds: int
    quoted_reliability: Probability
    simulated_success_probability: Probability | None
    simulated_within_sla_probability: Probability | None
    p95_latency_seconds: float | None
    score: float | None
    rank: int | None
