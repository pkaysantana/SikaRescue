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
    FundsCertainty,
    FundsLocation,
    OperationType,
    PositionStatus,
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


class FundsPosition(DomainModel):
    """Where the transaction's value was last PROVEN to be, and what may be done with it.

    Derived from the journal (plus in-flight executions and unclassified provider responses);
    never stored. `last_confirmed_location` is a claim about the journal, not about where the
    money physically is: while `certainty` is UNCERTAIN it may already have moved on.
    """

    transaction_id: TransactionId
    amount: Money  # the value at the last confirmed location
    last_confirmed_location: FundsLocation
    position_status: PositionStatus
    certainty: FundsCertainty
    available_for_automatic_action: bool
    derived_from_effect_ids: tuple[str, ...]  # the journal effects that prove the location
    reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _consistent(self) -> FundsPosition:
        proven = self.position_status in (PositionStatus.AVAILABLE, PositionStatus.FINAL)
        if proven != (self.certainty is FundsCertainty.PROVEN):
            raise ValueError(f"{self.position_status} is inconsistent with {self.certainty}")
        if self.available_for_automatic_action and (
            self.position_status is not PositionStatus.AVAILABLE
        ):
            raise ValueError("only an AVAILABLE, proven position allows automatic action")
        if not proven and not self.reason:
            raise ValueError("an uncertain position must say why")
        return self


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
    # The last location PROVEN by journal effects. Only while `funds_certainty` is PROVEN may
    # it be presented as where the funds are now.
    funds_location: FundsLocation
    funds_certainty: FundsCertainty
    available_for_automatic_action: bool
    uncertainty_reason: str | None = Field(default=None, max_length=200)
    funds_position: FundsPosition
    # A dispatched payout whose provider response is not yet classified (see evidence).
    evidence_pending: bool = False
    failed_leg: RailId | None
    last_payout_outcome: AttemptOutcome | None
    safe_to_restart_from_origin: bool
    manual_review_required: bool
    outstanding_obligation: OutstandingObligation | None
    completed_effect_keys: tuple[str, ...]
    legs: tuple[SettlementLeg, ...]
