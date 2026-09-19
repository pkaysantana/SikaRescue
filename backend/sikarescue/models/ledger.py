"""Append-only financial evidence: operation attempts, financial effects, journal entries.

These models only enforce invariants that can be checked from the record itself.
Facts that depend on other records (e.g. "has this effect already been posted?") are
enforced by `services.journal.FinancialJournal`.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from sikarescue.models.common import DomainModel, EntityId, FxRate, Money, TransactionId
from sikarescue.models.enums import (
    EFFECT_KEY_SUFFIX,
    OPERATION_FLOW,
    AttemptOutcome,
    ExecutionStatus,
    FailureStage,
    FundsLocation,
    OperationType,
    RailId,
)

ProviderText = Annotated[str, StringConstraints(max_length=280)]


def effect_key(transaction_id: str, operation: OperationType) -> str:
    """Transaction-wide key of a successful value-moving effect. Unique per transaction."""
    return f"{transaction_id}:{EFFECT_KEY_SUFFIX[operation]}"


def payout_execution_key(transaction_id: str, plan_id: str) -> str:
    """Attempt-level key: one payout execution per immutable recovery plan."""
    return f"{transaction_id}:plan:{plan_id}:payout"


class FailureDetail(DomainModel):
    stage: FailureStage
    http_status: int | None = Field(default=None, ge=100, le=599)
    provider_code: str | None = Field(default=None, max_length=64)
    message: ProviderText


class OperationAttempt(DomainModel):
    """One attempt to perform a logical operation on a rail. Failed attempts move no value."""

    attempt_id: EntityId
    transaction_id: TransactionId
    operation: OperationType
    rail_id: RailId
    source: FundsLocation
    destination: FundsLocation
    amount: Money
    outcome: AttemptOutcome
    idempotency_key: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    plan_id: EntityId | None = None
    provider_reference: str | None = Field(default=None, max_length=64)
    failure: FailureDetail | None = None
    started_at: datetime
    completed_at: datetime

    @model_validator(mode="after")
    def _check_consistency(self) -> OperationAttempt:
        if (self.source, self.destination) != OPERATION_FLOW[self.operation]:
            raise ValueError(f"{self.operation} must flow {OPERATION_FLOW[self.operation]}")
        if not self.amount.is_positive:
            raise ValueError("attempt amount must be positive")
        if self.completed_at < self.started_at:
            raise ValueError("completed_at precedes started_at")
        if self.outcome is AttemptOutcome.SUCCEEDED:
            if self.failure is not None:
                raise ValueError("a succeeded attempt cannot carry failure details")
        elif self.failure is None:
            raise ValueError(f"{self.outcome} attempt requires failure details")
        elif (
            self.outcome is AttemptOutcome.DEFINITIVE_FAILED
            and self.failure.stage is not FailureStage.PRE_ACCEPTANCE
        ):
            # Only a rejection *before* the provider accepted the request proves no value moved.
            raise ValueError("DEFINITIVE_FAILED requires a PRE_ACCEPTANCE failure; use UNKNOWN")
        return self


class FinancialEffect(DomainModel):
    """A successful value-moving effect. At most one per (transaction, operation)."""

    effect_key: str
    transaction_id: TransactionId
    operation: OperationType
    rail_id: RailId
    attempt_id: EntityId
    source: FundsLocation
    destination: FundsLocation
    source_amount: Money
    destination_amount: Money
    fx_rate: FxRate | None = None
    posted_at: datetime

    @model_validator(mode="after")
    def _check_consistency(self) -> FinancialEffect:
        if self.effect_key != effect_key(self.transaction_id, self.operation):
            raise ValueError("effect_key must be the transaction-wide key for this operation")
        if (self.source, self.destination) != OPERATION_FLOW[self.operation]:
            raise ValueError(f"{self.operation} must flow {OPERATION_FLOW[self.operation]}")
        if not (self.source_amount.is_positive and self.destination_amount.is_positive):
            raise ValueError("effect amounts must be positive")
        if self.operation is OperationType.FX_CONVERSION:
            if self.fx_rate is None:
                raise ValueError("FX effect requires fx_rate")
            if self.source_amount.currency == self.destination_amount.currency:
                raise ValueError("FX effect must change currency")
            expected = (self.source_amount.amount * self.fx_rate).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if expected != self.destination_amount.amount:
                raise ValueError("FX destination amount does not match source * rate")
        elif self.fx_rate is not None or self.source_amount != self.destination_amount:
            raise ValueError("non-FX effects must preserve amount and currency")
        return self


# --- Journal entry bodies (discriminated union) -------------------------------------


class EffectPosted(DomainModel):
    kind: Literal["effect_posted"] = "effect_posted"
    effect: FinancialEffect


class AttemptRecorded(DomainModel):
    kind: Literal["attempt_recorded"] = "attempt_recorded"
    attempt: OperationAttempt


class ExecutionStarted(DomainModel):
    kind: Literal["execution_started"] = "execution_started"
    execution_id: EntityId
    execution_key: str
    plan_id: EntityId
    rail_id: RailId


class ExecutionFinished(DomainModel):
    kind: Literal["execution_finished"] = "execution_finished"
    execution_id: EntityId
    plan_id: EntityId
    status: ExecutionStatus
    attempt_id: EntityId


class ProviderCallbackRecorded(DomainModel):
    """Late provider information about an earlier attempt (evidence, never moves value)."""

    kind: Literal["provider_callback"] = "provider_callback"
    rail_id: RailId
    attempt_id: EntityId
    note: ProviderText


class ReconciliationCompleted(DomainModel):
    kind: Literal["reconciliation_completed"] = "reconciliation_completed"
    reconciliation_id: EntityId


JournalBody = Annotated[
    EffectPosted
    | AttemptRecorded
    | ExecutionStarted
    | ExecutionFinished
    | ProviderCallbackRecorded
    | ReconciliationCompleted,
    Field(discriminator="kind"),
]


class JournalEntry(DomainModel):
    sequence: int = Field(ge=1)
    transaction_id: TransactionId
    recorded_at: datetime
    body: JournalBody
