"""Recovery plans, approvals, execution results and reconciliation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Any

from pydantic import Field, StringConstraints, model_validator

from sikarescue.models.common import (
    DomainModel,
    EntityId,
    Money,
    RouteId,
    Sha256Hex,
    TransactionId,
    utcnow,
)
from sikarescue.models.enums import (
    ExecutionStatus,
    FeeBearer,
    FundsLocation,
    RailId,
    RailStatus,
    RecoveryState,
)
from sikarescue.models.routes import CandidateRecoveryRoute, RouteEvaluation, route_id_for
from sikarescue.models.transaction import OutstandingObligation

ApproverId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9._@-]{2,64}$")]

# Every field that changes *what would be executed*. Changing any of these changes the
# plan hash, so an approval bound to the old hash can never authorise the new content.
HASHED_PLAN_FIELDS = frozenset(
    {
        "plan_id",
        "transaction_id",
        "expected_revision",
        "obligation",
        "source",
        "rail_id",
        "route_id",
        "amount",
        "incremental_fee",
        "fee_bearer",
        "expected_latency_seconds",
        "eligibility",
        "supersedes_plan_id",
    }
)


class PlanningSnapshot(DomainModel):
    """Everything route evaluation needs, captured under the transaction lock.

    Evaluation runs on this snapshot WITHOUT the lock (it may become slow/remote compute).
    Before a plan is persisted, the service re-checks the live transaction against it and
    discards the result if anything relevant changed.
    """

    transaction_id: TransactionId
    revision: int = Field(ge=0)
    recovery_state: RecoveryState
    funds_location: FundsLocation
    obligation: OutstandingObligation
    superseded_plan_id: EntityId | None
    candidates: tuple[CandidateRecoveryRoute, ...]
    captured_at: datetime

    @model_validator(mode="after")
    def _consistent(self) -> PlanningSnapshot:
        if self.funds_location is not self.obligation.source:
            raise ValueError("obligation must be sourced from the snapshot funds location")
        for c in self.candidates:
            if (c.transaction_id, c.source, c.amount) != (
                self.transaction_id,
                self.funds_location,
                self.obligation.amount,
            ):
                raise ValueError("every candidate must move the snapshot obligation")
        return self


class EligibilitySnapshot(DomainModel):
    """Deterministic eligibility facts captured when the plan was created."""

    rail_status: RailStatus
    policy_permitted: bool
    policy_version: str
    liquidity_sufficient: bool
    recipient_compatible: bool

    @property
    def all_clear(self) -> bool:
        return (
            self.rail_status is not RailStatus.DOWN
            and self.policy_permitted
            and self.liquidity_sufficient
            and self.recipient_compatible
        )


class RecoveryPlan(DomainModel):
    """An immutable, executable plan: pay the outstanding obligation via exactly one rail.

    Validators here only check the plan's *internal* consistency. Whether the plan is still
    current (revision, funds location, eligibility) is checked by the recovery service at
    approval and execution time.
    """

    plan_id: EntityId
    plan_hash: Sha256Hex
    transaction_id: TransactionId
    expected_revision: int = Field(ge=0)
    obligation: OutstandingObligation
    source: FundsLocation
    rail_id: RailId
    route_id: RouteId
    amount: Money
    incremental_fee: Money
    fee_bearer: FeeBearer = FeeBearer.OPERATOR
    expected_latency_seconds: int = Field(gt=0)
    eligibility: EligibilitySnapshot
    supersedes_plan_id: EntityId | None = None
    selection_reasons: tuple[str, ...] = ()
    evaluations: tuple[RouteEvaluation, ...] = Field(min_length=1)
    created_at: datetime = Field(default_factory=utcnow)

    @classmethod
    def build(cls, **fields: Any) -> RecoveryPlan:
        draft = cls.model_construct(plan_hash="0" * 64, **fields)
        return cls(plan_hash=draft.content_hash(), **fields)

    def content_hash(self) -> str:
        payload = self.model_dump(mode="json", include=set(HASHED_PLAN_FIELDS))
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    @model_validator(mode="after")
    def _check_integrity(self) -> RecoveryPlan:
        if self.plan_hash != self.content_hash():
            raise ValueError("plan_hash does not match plan content")
        if self.source is FundsLocation.SENDER_ACCOUNT:
            raise ValueError("a recovery plan may never restart from the sender")
        if self.source != self.obligation.source or self.amount != self.obligation.amount:
            raise ValueError("plan must move exactly the outstanding obligation from its source")
        if self.route_id != route_id_for(self.rail_id):
            raise ValueError("route_id does not match rail_id")
        if not self.eligibility.all_clear:
            raise ValueError("cannot build a plan on an ineligible route")
        selected = [e for e in self.evaluations if e.route_id == self.route_id]
        if len(selected) != 1 or not selected[0].passed:
            raise ValueError("selected route must appear exactly once as a passing evaluation")
        return self


class ApprovalRequest(DomainModel):
    request_id: EntityId
    plan_id: EntityId
    plan_hash: Sha256Hex
    transaction_id: TransactionId
    transaction_revision: int = Field(ge=0)
    summary: str = Field(max_length=500)
    requested_at: datetime


class ApprovalDecision(DomainModel):
    """A human decision about exactly one immutable plan (id + hash + revision)."""

    decision_id: EntityId
    plan_id: EntityId
    plan_hash: Sha256Hex
    transaction_id: TransactionId
    transaction_revision: int = Field(ge=0)
    approver: ApproverId
    approved: bool
    comment: str | None = Field(default=None, max_length=280)
    decided_at: datetime


class ExecutionResult(DomainModel):
    execution_id: EntityId
    execution_key: str
    plan_id: EntityId
    transaction_id: TransactionId
    rail_id: RailId
    status: ExecutionStatus
    attempt_id: EntityId | None = None
    recipient_credit_effect_key: str | None = None
    started_at: datetime
    finished_at: datetime | None = None
    # True when this call observed an existing execution instead of starting a new one.
    replayed: bool = False
    detail: str | None = Field(default=None, max_length=280)

    @model_validator(mode="after")
    def _check_status(self) -> ExecutionResult:
        succeeded = self.status is ExecutionStatus.SUCCEEDED
        if succeeded != (self.recipient_credit_effect_key is not None):
            raise ValueError("only a succeeded execution references a recipient-credit effect")
        in_progress = self.status is ExecutionStatus.IN_PROGRESS
        if in_progress != (self.finished_at is None):
            raise ValueError("finished_at must be set iff the execution has finished")
        return self


class ReconciliationCheck(DomainModel):
    name: str = Field(max_length=64)
    passed: bool
    detail: str = Field(max_length=200)


class ReconciliationResult(DomainModel):
    reconciliation_id: EntityId
    transaction_id: TransactionId
    reconciled: bool
    final_state: RecoveryState
    checks: tuple[ReconciliationCheck, ...] = Field(min_length=1)
    sender_debit_count: int = Field(ge=0)
    recipient_credit_count: int = Field(ge=0)
    duplicate_sender_debits: int = Field(ge=0)
    funds_location: FundsLocation
    reconciled_at: datetime

    @model_validator(mode="after")
    def _reconciled_matches_checks(self) -> ReconciliationResult:
        if self.reconciled != all(c.passed for c in self.checks):
            raise ValueError("reconciled must be true iff every check passed")
        return self
