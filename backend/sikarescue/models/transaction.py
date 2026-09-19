"""Payment instruction, recipient details and the derived transaction state."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import Field, SecretStr, model_validator

from sikarescue.models.common import (
    DomainModel,
    EntityId,
    FxRate,
    Money,
    OpaqueToken,
    TransactionId,
)
from sikarescue.models.enums import (
    AttemptOutcome,
    CountryCode,
    EndpointType,
    FundsLocation,
    OperationType,
    RailId,
    RecoveryState,
    SettlementLegStatus,
)


class RecipientDetails(DomainModel):
    """Synthetic recipient PII. SecretStr keeps it masked in repr/dumps by default.

    Never pass this object to a model/LLM: use `ModelTransactionView` instead.
    """

    name: SecretStr
    phone: SecretStr
    account_reference: SecretStr
    endpoint_type: EndpointType


class PaymentTransaction(DomainModel):
    """The immutable payment instruction as submitted by the sender."""

    transaction_id: TransactionId
    created_at: datetime
    origin_country: CountryCode
    destination_country: CountryCode
    send_amount: Money
    fx_rate: FxRate
    payout_amount: Money
    recipient: RecipientDetails
    recipient_token: OpaqueToken
    original_route: tuple[RailId, ...] = Field(min_length=1)
    synthetic: Literal[True] = True

    @model_validator(mode="after")
    def _check_amounts(self) -> PaymentTransaction:
        if not (self.send_amount.is_positive and self.payout_amount.is_positive):
            raise ValueError("amounts must be positive")
        if self.send_amount.currency == self.payout_amount.currency:
            raise ValueError("cross-border corridor must change currency")
        expected = (self.send_amount.amount * self.fx_rate).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if expected != self.payout_amount.amount:
            raise ValueError("payout_amount must equal send_amount * fx_rate")
        return self

    @property
    def corridor(self) -> str:
        return f"{self.origin_country.value}->{self.destination_country.value}"


class SettlementLeg(DomainModel):
    """Derived view of one attempted leg, built from journal attempts."""

    sequence: int = Field(ge=1)
    rail_id: RailId
    operation: OperationType
    source: FundsLocation
    destination: FundsLocation
    status: SettlementLegStatus
    attempt_id: EntityId | None = None
    is_recovery: bool = False
    failure_summary: str | None = None


class OutstandingObligation(DomainModel):
    """The single unfinished value movement owed on a transaction."""

    effect_key: str
    operation: Literal[OperationType.RECIPIENT_CREDIT] = OperationType.RECIPIENT_CREDIT
    amount: Money
    source: FundsLocation
    destination: Literal[FundsLocation.RECIPIENT_ENDPOINT] = FundsLocation.RECIPIENT_ENDPOINT
    recipient_token: OpaqueToken
    endpoint_type: EndpointType


class TransactionState(DomainModel):
    """Deterministically derived from the journal. The LLM never owns or edits this."""

    transaction_id: TransactionId
    revision: int = Field(ge=0)
    recovery_state: RecoveryState
    sender_debited: bool
    fx_completed: bool
    settlement_completed: bool
    recipient_credited: bool
    sender_debit_count: int = Field(ge=0)
    recipient_credit_count: int = Field(ge=0)
    funds_location: FundsLocation
    failed_leg: RailId | None
    last_payout_outcome: AttemptOutcome | None
    safe_to_restart_from_origin: bool
    manual_review_required: bool
    outstanding_obligation: OutstandingObligation | None
    completed_effect_keys: tuple[str, ...]
    legs: tuple[SettlementLeg, ...]
