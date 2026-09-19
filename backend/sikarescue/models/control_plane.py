"""Derived control-plane artifacts: effect graph, safe action frontier, ledger preview and the
naive-retry counterfactual.

Every model here is DERIVED from authoritative state (the journal, the immutable plan, the
aggregate's executions and incidents) by pure functions. None is stored, none is a second
source of truth, and none can authorise or move value.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from sikarescue.models.common import DomainModel, EntityId, Money, Sha256Hex, TransactionId
from sikarescue.models.enums import (
    AttemptOutcome,
    FundsLocation,
    HardConstraintStatus,
    OperationType,
    PositionStatus,
    RailId,
    RejectionReason,
)
from sikarescue.models.transaction import FundsPosition

# ------------------------------------------------------------------ financial effect graph


class EffectNodeKind(StrEnum):
    PAYMENT_INTENT = "PAYMENT_INTENT"
    SENDER_DEBIT = "SENDER_DEBIT"
    FX_CONVERSION = "FX_CONVERSION"
    GH_SETTLEMENT = "GH_SETTLEMENT"
    RECIPIENT_PAYOUT = "RECIPIENT_PAYOUT"


class EffectNodeStatus(StrEnum):
    OPEN = "OPEN"  # intent: not yet fulfilled
    FULFILLED = "FULFILLED"  # intent: the recipient was credited
    COMPLETE = "COMPLETE"  # effect posted to the journal
    PENDING = "PENDING"  # not attempted yet
    FAILED = "FAILED"  # only definitive failures so far: no value moved
    AWAITING_EVIDENCE = "AWAITING_EVIDENCE"  # a response arrived but is not classified
    IN_FLIGHT = "IN_FLIGHT"  # an execution is in progress
    UNKNOWN = "UNKNOWN"  # an attempt's outcome is UNKNOWN: value may have moved


class AttemptEvidence(DomainModel):
    attempt_id: EntityId
    rail_id: RailId
    outcome: AttemptOutcome
    is_recovery: bool
    plan_id: EntityId | None = None
    provider_reference: str | None = None
    failure_summary: str | None = None


class EffectNode(DomainModel):
    node_id: str = Field(max_length=96)  # the journal effect key (or `{txn}:intent`)
    kind: EffectNodeKind
    operation: OperationType | None
    status: EffectNodeStatus
    source: FundsLocation
    destination: FundsLocation
    source_amount: Money | None = None  # from the posted effect (or the instruction, intent)
    destination_amount: Money | None = None
    fx_rate: Decimal | None = None
    rail_id: RailId | None = None  # the rail of the POSTED effect
    posted_attempt_id: EntityId | None = None
    attempts: tuple[AttemptEvidence, ...] = ()
    outstanding: Money | None = None  # still owed on this node (the open obligation)
    depends_on: tuple[str, ...] = ()


class FinancialEffectGraph(DomainModel):
    transaction_id: TransactionId
    revision: int = Field(ge=0)
    nodes: tuple[EffectNode, ...]

    @property
    def edges(self) -> tuple[tuple[str, str], ...]:
        return tuple((dep, n.node_id) for n in self.nodes for dep in n.depends_on)

    def node(self, kind: EffectNodeKind) -> EffectNode:
        return next(n for n in self.nodes if n.kind is kind)


# ------------------------------------------------------------------ safe action frontier


class FrontierActionKind(StrEnum):
    PAYOUT = "PAYOUT"  # the only kind that moves value, and only via an approved plan
    CLASSIFY_PROVIDER_EVIDENCE = "CLASSIFY_PROVIDER_EVIDENCE"
    WAIT_FOR_PROVIDER_EVIDENCE = "WAIT_FOR_PROVIDER_EVIDENCE"
    QUERY_PROVIDER = "QUERY_PROVIDER"  # demo abstraction: no provider query is implemented
    MANUAL_REVIEW = "MANUAL_REVIEW"
    NO_ACTION_REQUIRED = "NO_ACTION_REQUIRED"


FrontierBasis = Literal["PLAN", "CANDIDATES", "PENDING_EVIDENCE", "UNCERTAIN_FUNDS", "SETTLED"]


class FrontierAction(DomainModel):
    action_id: str = Field(max_length=64)
    kind: FrontierActionKind
    label: str = Field(max_length=120)
    rail_id: RailId | None = None
    moves_value: bool = False
    hard_constraint_status: HardConstraintStatus | None = None
    rejection_reasons: tuple[RejectionReason, ...] = ()
    rejection_details: tuple[str, ...] = ()
    simulated: bool = False
    simulated_reliability: float | None = None
    score: float | None = None
    rank: int | None = Field(default=None, ge=1)
    selected: bool = False
    implemented: bool = True  # False for demo abstractions (e.g. QUERY_PROVIDER)
    note: str | None = Field(default=None, max_length=240)

    @model_validator(mode="after")
    def _consistent(self) -> FrontierAction:
        if self.moves_value != (self.kind is FrontierActionKind.PAYOUT):
            raise ValueError("only PAYOUT actions move value")
        if self.kind is FrontierActionKind.PAYOUT and self.rail_id is None:
            raise ValueError("a PAYOUT action names its rail")
        if self.selected and (
            self.hard_constraint_status is not HardConstraintStatus.PASSED or self.rank != 1
        ):
            raise ValueError("only the rank-1 eligible action can be selected")
        return self


class FrontierCounts(DomainModel):
    candidates: int = Field(ge=0)
    rejected_before_simulation: int = Field(ge=0)
    eligible: int = Field(ge=0)
    simulated: int = Field(ge=0)
    selected: int = Field(ge=0, le=1)


class SafeActionFrontier(DomainModel):
    """candidates -> hard constraints -> eligible -> simulation -> ranking -> selected plan."""

    transaction_id: TransactionId
    revision: int = Field(ge=0)
    basis: FrontierBasis
    funds_position: FundsPosition
    payout_actions_permitted: bool
    actions: tuple[FrontierAction, ...]
    counts: FrontierCounts
    plan_id: EntityId | None = None
    plan_hash: Sha256Hex | None = None
    reason: str = Field(max_length=240)

    @model_validator(mode="after")
    def _no_payout_without_proven_funds(self) -> SafeActionFrontier:
        payouts = [a for a in self.actions if a.kind is FrontierActionKind.PAYOUT]
        if payouts and not self.payout_actions_permitted:
            raise ValueError("a frontier that forbids payouts cannot contain a payout action")
        if self.payout_actions_permitted and not (
            self.funds_position.available_for_automatic_action
        ):
            raise ValueError("payout actions require an AVAILABLE, proven funds position")
        counts = self.counts
        rejected = [a for a in payouts if a.hard_constraint_status is HardConstraintStatus.REJECTED]
        if (
            counts.candidates != len(payouts)
            or counts.rejected_before_simulation != len(rejected)
            or counts.eligible != len(payouts) - len(rejected)
            or counts.simulated != sum(a.simulated for a in payouts)
            or counts.selected != sum(a.selected for a in payouts)
            or any(a.simulated for a in rejected)
        ):
            raise ValueError("frontier counts do not match its actions")
        return self


# ------------------------------------------------------------------ ledger preview


class LocationHolding(DomainModel):
    location: FundsLocation
    amount: Money


class LedgerSnapshot(DomainModel):
    revision: int = Field(ge=0)
    sender_debit_count: int = Field(ge=0)
    fx_count: int = Field(ge=0)
    settlement_count: int = Field(ge=0)
    recipient_credit_count: int = Field(ge=0)
    duplicate_sender_debits: int = Field(ge=0)
    holdings: tuple[LocationHolding, ...]  # value this payment holds at each location
    funds_location: FundsLocation
    outstanding: Money | None


class LedgerChange(DomainModel):
    label: str = Field(max_length=60)
    current: str = Field(max_length=60)
    proposed: str = Field(max_length=60)
    changed: bool


class LedgerPreview(DomainModel):
    """CURRENT vs PROPOSED post-execution ledger for one plan. Never mutates anything.

    The proposed state is produced by posting the plan's payout effect - built by the same
    code execution uses - into a throwaway fork of the journal, so every journal invariant
    (no duplicate effect, location and value continuity) is checked on the projection too.
    """

    transaction_id: TransactionId
    plan_id: EntityId
    plan_hash: Sha256Hex
    bound_revision: int = Field(ge=0)
    valid: bool
    invalidation_reasons: tuple[str, ...] = ()
    current: LedgerSnapshot
    proposed: LedgerSnapshot | None = None
    proposed_effect_key: str | None = None
    proposed_rail: RailId | None = None
    changes: tuple[LedgerChange, ...] = ()

    @model_validator(mode="after")
    def _consistent(self) -> LedgerPreview:
        if self.valid != (self.proposed is not None) or self.valid == bool(
            self.invalidation_reasons
        ):
            raise ValueError("a valid preview has a projection and no invalidation reasons")
        return self


# ------------------------------------------------------------------ naive retry counterfactual


class CounterfactualStep(DomainModel):
    operation: OperationType
    rail_id: RailId | None
    source: FundsLocation
    destination: FundsLocation
    amount: Money
    already_completed: bool
    risk: str | None = Field(default=None, max_length=160)


class RetryCounterfactual(DomainModel):
    """Dry run: restart from origin vs SikaRescue. Nothing is executed or recorded."""

    transaction_id: TransactionId
    revision: int = Field(ge=0)
    naive_steps: tuple[CounterfactualStep, ...]
    naive_repeated_effects: int = Field(ge=0)
    naive_extra_sender_debit: Money | None
    duplicate_recipient_credit_risk: bool
    sikarescue_steps: tuple[CounterfactualStep, ...]
    sikarescue_repeated_effects: int = Field(ge=0)
    sikarescue_action: str = Field(max_length=200)
    position_status: PositionStatus
