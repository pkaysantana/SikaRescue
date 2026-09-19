"""Allowlisted, model-facing views. The ONLY transaction data an LLM may ever see.

Built field-by-field in `services.model_boundary`. They contain opaque tokens instead of
recipient identity, and sanitised failure summaries instead of raw provider payloads.
`extra="forbid"` (inherited from DomainModel) rejects any attempt to add a PII field.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from sikarescue.models.common import (
    DomainModel,
    EntityId,
    OpaqueToken,
    Probability,
    RouteId,
    TransactionId,
)
from sikarescue.models.enums import (
    Actor,
    AdviceReasonCode,
    AttemptOutcome,
    AuditEventType,
    Currency,
    EndpointType,
    FeeBearer,
    FundsLocation,
    HardConstraintStatus,
    OperationType,
    PlanStatus,
    RailId,
    RailStatus,
    RecoveryState,
    RejectionReason,
    ScenarioId,
)

MoneyText = Annotated[str, StringConstraints(pattern=r"^\d+\.\d{2} [A-Z]{3}$")]  # "1830.00 GHS"
GbpText = Annotated[str, StringConstraints(pattern=r"^\d+\.\d{2}$")]  # "0.18" (GBP)
HashPrefix = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{12}$")]


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


# ------------------------------------------------ recovery decision context (compact packet)


class CompletedEffectView(DomainModel):
    """An immutable value movement that already succeeded. It must never be replayed."""

    operation: OperationType
    rail_id: RailId
    destination: FundsLocation


class ObligationView(DomainModel):
    operation: OperationType
    amount: MoneyText
    source: FundsLocation
    destination: FundsLocation
    recipient_token: OpaqueToken


class FailureView(DomainModel):
    rail_id: RailId
    outcome: AttemptOutcome
    summary: str | None = Field(default=None, max_length=120)
    value_moved: bool | None  # None = cannot be determined (UNKNOWN outcome)


class StressResultView(DomainModel):
    scenario: ScenarioId
    reliability: Probability
    within_sla: Probability


class DecisionRouteView(DomainModel):
    """A route that passed every hard constraint, with its deterministic evaluation."""

    rail_id: RailId
    rank: int
    score: float
    incremental_fee_gbp: GbpText
    quoted_arrival_seconds: int
    quoted_reliability: Probability
    simulated_reliability: Probability
    simulated_within_sla: Probability
    simulated_p95_arrival_seconds: float | None
    stress: tuple[StressResultView, ...]


class RejectedRouteView(DomainModel):
    """A route removed by hard constraints BEFORE simulation or scoring."""

    rail_id: RailId
    reasons: tuple[RejectionReason, ...]
    details: tuple[str, ...]


class ComputeView(DomainModel):
    backend: str
    fallback_from: str | None
    fallback_reason: str | None
    parallel_jobs: int
    simulated_outcomes: int
    scenarios: tuple[ScenarioId, ...]
    sla_seconds: float


class SelectedPlanView(DomainModel):
    plan_id: EntityId
    plan_hash_prefix: HashPrefix
    bound_revision: int
    status: PlanStatus
    rail_id: RailId
    amount: MoneyText
    incremental_fee_gbp: GbpText
    fee_bearer: FeeBearer
    quoted_arrival_seconds: int
    simulated_reliability: Probability
    approval_required: Literal[True] = True


class RecoveryDecisionContext(DomainModel):
    """Compact, evidence-preserving packet: everything needed to explain the decision.

    Deterministic and sanitised. It drops redundancy (raw journals, per-scenario parameters,
    audit prose, repeated evaluations) but keeps every fact a recommendation depends on.
    """

    transaction_id: TransactionId
    revision: int
    funds_location: FundsLocation
    sender_debited: bool
    safe_to_restart_from_origin: bool
    completed_effects: tuple[CompletedEffectView, ...]
    outstanding_obligation: ObligationView
    failure: FailureView | None
    candidate_routes: tuple[DecisionRouteView, ...]
    rejected_routes: tuple[RejectedRouteView, ...]
    compute: ComputeView
    selected_plan: SelectedPlanView
    reason_codes: tuple[AdviceReasonCode, ...]
    invariants: tuple[str, ...]


# ------------------------------------------------ plan + audit views


class ModelPlanView(DomainModel):
    plan_id: EntityId
    plan_hash_prefix: HashPrefix
    transaction_id: TransactionId
    bound_revision: int
    status: PlanStatus
    source: FundsLocation
    rail_id: RailId
    amount: MoneyText
    incremental_fee_gbp: GbpText
    fee_bearer: FeeBearer
    quoted_arrival_seconds: int
    rail_status: RailStatus
    policy_permitted: bool
    policy_version: str
    liquidity_sufficient: bool
    recipient_compatible: bool
    selection_reasons: tuple[str, ...]
    supersedes_plan_id: EntityId | None
    approval_required: Literal[True] = True


class ModelAuditEntryView(DomainModel):
    sequence: int
    actor: Actor
    event_type: AuditEventType
    state: RecoveryState
    summary: str = Field(max_length=280)


class ModelAuditSummary(DomainModel):
    transaction_id: TransactionId
    total_events: int
    entries: tuple[ModelAuditEntryView, ...]
    truncated: bool
