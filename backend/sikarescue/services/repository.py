"""In-memory transaction aggregates and repository.

SINGLE-PROCESS DEMO ONLY: each aggregate owns an `asyncio.Lock` that serialises commands
for that transaction inside one event loop. Run the API with ONE worker. A real deployment
would need a database transaction / row lock or a distributed lock instead.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from sikarescue.errors import NotFoundError
from sikarescue.models import (
    Actor,
    ApprovalDecision,
    ApprovalRequest,
    AuditEvent,
    AuditEventType,
    ExecutionResult,
    ExecutionStatus,
    PaymentTransaction,
    PlanStatus,
    ReconciliationResult,
    RecoveryPlan,
    RecoveryState,
    new_id,
    utcnow,
)
from sikarescue.models.audit import AuditValue
from sikarescue.services.journal import FinancialJournal
from sikarescue.services.state_machine import assert_transition


@dataclass(eq=False)
class TransactionAggregate:
    instruction: PaymentTransaction
    journal: FinancialJournal
    state: RecoveryState = RecoveryState.FAILED
    plans: dict[str, RecoveryPlan] = field(default_factory=dict)
    plan_status: dict[str, PlanStatus] = field(default_factory=dict)
    current_plan_id: str | None = None
    approval_requests: dict[str, ApprovalRequest] = field(default_factory=dict)
    approvals: dict[str, ApprovalDecision] = field(default_factory=dict)
    # Keyed by the attempt-level execution key `{txn}:plan:{plan_id}:payout`.
    executions: dict[str, ExecutionResult] = field(default_factory=dict)
    reconciliation: ReconciliationResult | None = None
    audit: list[AuditEvent] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def transaction_id(self) -> str:
        return self.instruction.transaction_id

    @property
    def revision(self) -> int:
        return self.journal.revision

    @property
    def current_plan(self) -> RecoveryPlan | None:
        return self.plans.get(self.current_plan_id) if self.current_plan_id else None

    def has_execution_in_progress(self) -> bool:
        return any(e.status is ExecutionStatus.IN_PROGRESS for e in self.executions.values())

    def record_audit(
        self, event_type: AuditEventType, actor: Actor, summary: str, **data: AuditValue
    ) -> AuditEvent:
        event = AuditEvent(
            event_id=new_id("evt"),
            transaction_id=self.transaction_id,
            sequence=len(self.audit) + 1,
            timestamp=utcnow(),
            event_type=event_type,
            actor=actor,
            summary=summary,
            revision=self.revision,
            recovery_state=self.state,
            data=data,
        )
        self.audit.append(event)
        return event

    def transition(self, target: RecoveryState, *, actor: Actor, reason: str) -> None:
        assert_transition(self.state, target)
        previous, self.state = self.state, target
        self.record_audit(
            AuditEventType.STATE_TRANSITION,
            actor,
            f"{previous} -> {target}: {reason}",
            from_state=previous.value,
            to_state=target.value,
        )


class InMemoryTransactionRepository:
    def __init__(self) -> None:
        self._transactions: dict[str, TransactionAggregate] = {}
        self._plan_owner: dict[str, str] = {}

    def add(self, aggregate: TransactionAggregate) -> None:
        self._transactions[aggregate.transaction_id] = aggregate

    def get(self, transaction_id: str) -> TransactionAggregate:
        try:
            return self._transactions[transaction_id]
        except KeyError:
            raise NotFoundError(f"unknown transaction {transaction_id}") from None

    def index_plan(self, plan: RecoveryPlan) -> None:
        self._plan_owner[plan.plan_id] = plan.transaction_id

    def get_by_plan(self, plan_id: str) -> tuple[TransactionAggregate, RecoveryPlan]:
        transaction_id = self._plan_owner.get(plan_id)
        if transaction_id is None:
            raise NotFoundError(f"unknown recovery plan {plan_id}")
        aggregate = self.get(transaction_id)
        return aggregate, aggregate.plans[plan_id]

    def transaction_ids(self) -> list[str]:
        return list(self._transactions)
