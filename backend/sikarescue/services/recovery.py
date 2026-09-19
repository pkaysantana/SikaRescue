"""Deterministic recovery service: the only code that changes recovery/financial state.

Later phases expose these methods as agent tools and API endpoints. The agent may call
them and explain results; it never computes, stores or edits financial state itself.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import NoReturn

from sikarescue.errors import (
    ApprovalMismatchError,
    ApprovalRequiredError,
    ExecutionConflictError,
    IllegalTransitionError,
    ManualReviewRequiredError,
    NoEligibleRouteError,
    RecoveryPreconditionError,
    StalePlanError,
    StalePlanningResultError,
)
from sikarescue.models import (
    Actor,
    ApprovalDecision,
    ApprovalRequest,
    AttemptOutcome,
    AuditEvent,
    AuditEventType,
    CandidateRecoveryRoute,
    DomainModel,
    EligibilitySnapshot,
    EndpointType,
    ExecutionFinished,
    ExecutionResult,
    ExecutionStarted,
    ExecutionStatus,
    FailureDetail,
    FailureStage,
    FinancialEffect,
    FundsLocation,
    JournalEntry,
    LiquidityStatus,
    OperationAttempt,
    OperationType,
    OutstandingObligation,
    PlanningSnapshot,
    PlanStatus,
    PolicyDecision,
    Rail,
    RailId,
    RailQuote,
    ReconciliationCheck,
    ReconciliationCompleted,
    ReconciliationResult,
    RecoveryPlan,
    RecoveryState,
    RouteEvaluation,
    TransactionState,
    effect_key,
    new_id,
    payout_execution_key,
    utcnow,
)
from sikarescue.services.diagnosis import (
    check_recovery_preconditions,
    derive_state,
    outstanding_obligation,
    summarise_failure,
)
from sikarescue.services.liquidity import LiquidityBook
from sikarescue.services.payout_gateway import (
    PayoutRequest,
    PayoutResponse,
    SimulatedPayoutGateway,
)
from sikarescue.services.policy import PolicyEngine
from sikarescue.services.rails import RailRegistry
from sikarescue.services.repository import InMemoryTransactionRepository, TransactionAggregate
from sikarescue.services.routes import (
    build_candidate,
    discover_candidates,
    eligibility_of,
    evaluate_routes,
)
from sikarescue.services.state_machine import PLANNABLE_STATES, assert_transition

ENGINE = Actor.RECOVERY_ENGINE


def _evolve[M: DomainModel](model: M, **updates: object) -> M:
    """Frozen-model update that re-runs validation (model_copy(update=...) does not)."""
    return type(model).model_validate(model.model_dump() | updates)


def _ambiguous_response(code: str, message: str) -> PayoutResponse:
    """We cannot prove whether value moved: UNKNOWN, never DEFINITIVE_FAILED."""
    return PayoutResponse(
        outcome=AttemptOutcome.UNKNOWN,
        failure=FailureDetail(
            stage=FailureStage.UNDETERMINED,
            provider_code=code,
            message=f"{message} Provider outcome unknown; manual review required.",
        ),
    )


class RecoveryService:
    def __init__(
        self,
        repository: InMemoryTransactionRepository,
        registry: RailRegistry,
        policy: PolicyEngine,
        liquidity: LiquidityBook,
        gateway: SimulatedPayoutGateway,
        *,
        compute_backend: str = "local",
        payout_timeout_seconds: float = 30.0,
    ):
        self.repository = repository
        self.registry = registry
        self.policy = policy
        self.liquidity = liquidity
        self.gateway = gateway
        self.compute_backend = compute_backend
        self.payout_timeout_seconds = payout_timeout_seconds

    # ===================================================================== read tools

    def get_transaction_state(self, transaction_id: str) -> TransactionState:
        return derive_state(self.repository.get(transaction_id))

    def get_available_recovery_rails(self, transaction_id: str) -> tuple[Rail, ...]:
        aggregate = self.repository.get(transaction_id)
        return self.registry.payout_rails_from(aggregate.journal.funds_location())

    def get_quotes(self, rail_ids: list[RailId]) -> tuple[RailQuote, ...]:
        return tuple(self.registry.quote(r) for r in rail_ids)

    def check_policy(self, transaction_id: str, rail_id: RailId) -> PolicyDecision:
        aggregate, obligation = self._require_obligation(transaction_id)
        rail = self.registry.get(rail_id)
        return self.policy.check(aggregate.instruction, rail, obligation.source, obligation.amount)

    def check_liquidity(self, transaction_id: str, rail_id: RailId) -> LiquidityStatus:
        _, obligation = self._require_obligation(transaction_id)
        return self.liquidity.check(rail_id, obligation.amount)

    def discover_recovery_routes(self, transaction_id: str) -> tuple[CandidateRecoveryRoute, ...]:
        aggregate, obligation = self._require_obligation(transaction_id)
        return discover_candidates(
            aggregate, obligation, self.registry, self.policy, self.liquidity
        )

    def evaluate_recovery_routes(self, transaction_id: str) -> tuple[RouteEvaluation, ...]:
        return evaluate_routes(self.discover_recovery_routes(transaction_id), self.compute_backend)

    def get_plan(self, plan_id: str) -> RecoveryPlan:
        return self.repository.get_by_plan(plan_id)[1]

    def get_plan_status(self, plan_id: str) -> PlanStatus:
        aggregate, _ = self.repository.get_by_plan(plan_id)
        return aggregate.plan_status[plan_id]

    def get_approval_request(self, plan_id: str) -> ApprovalRequest:
        aggregate, _ = self.repository.get_by_plan(plan_id)
        return aggregate.approval_requests[plan_id]

    def get_audit_timeline(self, transaction_id: str) -> tuple[AuditEvent, ...]:
        return tuple(self.repository.get(transaction_id).audit)

    def get_journal(self, transaction_id: str) -> tuple[JournalEntry, ...]:
        return self.repository.get(transaction_id).journal.entries

    # ===================================================================== planning

    async def create_recovery_plan(self, transaction_id: str) -> RecoveryPlan:
        """Diagnose, discover, filter, score and produce ONE immutable plan needing approval.

        Idempotent while the current plan is still fresh: returns it instead of a new one.
        Route evaluation runs WITHOUT the transaction lock (see `_plan_from_snapshot`).
        """
        aggregate = self.repository.get(transaction_id)
        async with aggregate.lock:
            current = aggregate.current_plan
            if (
                current is not None
                and aggregate.state in (RecoveryState.AWAITING_APPROVAL, RecoveryState.APPROVED)
                and aggregate.plan_status[current.plan_id]
                in (PlanStatus.PENDING_APPROVAL, PlanStatus.APPROVED)
            ):
                reasons = self._staleness(aggregate, current)
                if not reasons:
                    return current
                self._mark_stale(aggregate, current, reasons)
            snapshot = self._capture_planning_snapshot(aggregate)
        return await self._plan_from_snapshot(aggregate, snapshot)

    async def request_recovery_approval(self, plan_id: str) -> ApprovalRequest:
        """Every plan requires approval; the request is created with the plan."""
        return self.get_approval_request(plan_id)

    # Planning runs in three phases so slow (future: remote) compute never holds the lock:
    #   A. under the lock:   capture a PlanningSnapshot       (_capture_planning_snapshot)
    #   B. without the lock: evaluate routes on the snapshot  (_evaluate_snapshot)
    #   C. under the lock:   re-verify, then persist the plan (_commit_plan)

    def _capture_planning_snapshot(self, aggregate: TransactionAggregate) -> PlanningSnapshot:
        """Phase A (caller holds the lock). May escalate to MANUAL_REVIEW and raise."""
        if aggregate.state not in PLANNABLE_STATES:
            raise IllegalTransitionError(f"cannot plan recovery while {aggregate.state}")
        if aggregate.state is RecoveryState.FAILED:
            aggregate.transition(
                RecoveryState.DIAGNOSING,
                actor=ENGINE,
                reason="incident received; reconstructing state from journal",
            )

        state = derive_state(aggregate)
        aggregate.record_audit(
            AuditEventType.DIAGNOSIS_COMPLETED,
            ENGINE,
            f"Funds at {state.funds_location}; failed leg {state.failed_leg}; "
            f"sender debited={state.sender_debited}; restart from origin is "
            f"{'safe' if state.safe_to_restart_from_origin else 'NOT safe'}",
            funds_location=state.funds_location.value,
            failed_leg=state.failed_leg.value if state.failed_leg else None,
            sender_debited=state.sender_debited,
            safe_to_restart_from_origin=state.safe_to_restart_from_origin,
        )
        if state.manual_review_required:
            self._escalate(aggregate, "payout outcome UNKNOWN; reconcile with provider first")
            raise ManualReviewRequiredError(
                "an attempt has an UNKNOWN outcome; no further value may move until reconciled"
            )
        violations = check_recovery_preconditions(aggregate)
        if violations:
            self._escalate(aggregate, "; ".join(violations))
            raise RecoveryPreconditionError(violations)
        obligation = state.outstanding_obligation
        assert obligation is not None  # guaranteed by the preconditions above

        candidates = discover_candidates(
            aggregate, obligation, self.registry, self.policy, self.liquidity
        )
        aggregate.record_audit(
            AuditEventType.ROUTES_DISCOVERED,
            ENGINE,
            f"{len(candidates)} candidate payout routes from {obligation.source}",
            candidate_count=len(candidates),
            rails=",".join(c.rail.rail_id.value for c in candidates),
        )
        return PlanningSnapshot(
            transaction_id=aggregate.transaction_id,
            revision=aggregate.revision,
            recovery_state=aggregate.state,
            funds_location=state.funds_location,
            obligation=obligation,
            superseded_plan_id=aggregate.current_plan_id,
            candidates=candidates,
            captured_at=utcnow(),
        )

    async def _evaluate_snapshot(self, snapshot: PlanningSnapshot) -> tuple[RouteEvaluation, ...]:
        """Phase B: pure evaluation of an immutable snapshot. Never holds the lock.

        This is the seam where the compute backend (local / Modal) plugs in.
        """
        return evaluate_routes(snapshot.candidates, self.compute_backend)

    async def _plan_from_snapshot(
        self, aggregate: TransactionAggregate, snapshot: PlanningSnapshot
    ) -> RecoveryPlan:
        evaluations = await self._evaluate_snapshot(snapshot)
        async with aggregate.lock:
            return self._commit_plan(aggregate, snapshot, evaluations)

    def _commit_plan(
        self,
        aggregate: TransactionAggregate,
        snapshot: PlanningSnapshot,
        evaluations: tuple[RouteEvaluation, ...],
    ) -> RecoveryPlan:
        """Phase C (caller holds the lock): persist only if the snapshot is still true."""
        if aggregate.current_plan_id != snapshot.superseded_plan_id:
            winner = aggregate.current_plan
            if (
                winner is not None
                and aggregate.plan_status[winner.plan_id] is PlanStatus.PENDING_APPROVAL
                and not self._staleness(aggregate, winner)
            ):
                return winner  # a concurrent planning run already committed a fresh plan
            self._discard_planning_result(aggregate, ["another plan was committed meanwhile"])
        drift = self._snapshot_drift(aggregate, snapshot)
        if drift:
            self._discard_planning_result(aggregate, drift)

        passing = [e for e in evaluations if e.passed]
        rejected = [e for e in evaluations if not e.passed]
        aggregate.record_audit(
            AuditEventType.ROUTES_EVALUATED,
            ENGINE,
            f"{len(rejected)} rejected by hard constraints, {len(passing)} scored",
            evaluated=len(passing),
            rejected=len(rejected),
            compute_backend=self.compute_backend,
            **{
                f"rejected_{e.rail_id.value}": ",".join(r.value for r in e.rejection_reasons)
                for e in rejected
            },
        )
        if not passing:
            self._escalate(aggregate, "no recovery route satisfies every hard constraint")
            raise NoEligibleRouteError("no eligible recovery route")

        best = passing[0]
        candidate = next(c for c in snapshot.candidates if c.route_id == best.route_id)
        # The world outside the journal (rail status, policy, liquidity, quotes) may also
        # have moved while we computed: the selected route must still look exactly the same.
        live = build_candidate(
            aggregate,
            snapshot.obligation,
            candidate.rail.rail_id,
            self.registry,
            self.policy,
            self.liquidity,
        )
        if eligibility_of(live) != eligibility_of(candidate) or live.quote != candidate.quote:
            self._discard_planning_result(
                aggregate, [f"{candidate.rail.rail_id} eligibility or quote changed"]
            )

        obligation = snapshot.obligation
        plan = RecoveryPlan.build(
            plan_id=new_id("plan"),
            transaction_id=snapshot.transaction_id,
            expected_revision=snapshot.revision,
            obligation=obligation,
            source=obligation.source,
            rail_id=candidate.rail.rail_id,
            route_id=candidate.route_id,
            amount=obligation.amount,
            incremental_fee=candidate.quote.incremental_fee,
            expected_latency_seconds=candidate.quote.expected_latency_seconds,
            eligibility=eligibility_of(candidate),
            supersedes_plan_id=snapshot.superseded_plan_id,
            selection_reasons=self._selection_reasons(
                candidate, best, passing, obligation.endpoint_type
            ),
            evaluations=evaluations,
        )
        aggregate.plans[plan.plan_id] = plan
        aggregate.plan_status[plan.plan_id] = PlanStatus.PENDING_APPROVAL
        aggregate.current_plan_id = plan.plan_id
        self.repository.index_plan(plan)
        aggregate.record_audit(
            AuditEventType.RECOVERY_PLAN_CREATED,
            ENGINE,
            f"Plan {plan.plan_id}: {plan.source} -> {plan.rail_id} -> recipient, "
            f"fee {plan.incremental_fee} (operator), ~{plan.expected_latency_seconds}s",
            plan_id=plan.plan_id,
            rail_id=plan.rail_id.value,
            plan_hash=plan.plan_hash[:12],
            supersedes_plan_id=plan.supersedes_plan_id,
        )
        request = ApprovalRequest(
            request_id=new_id("apr"),
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            transaction_id=plan.transaction_id,
            transaction_revision=plan.expected_revision,
            summary=(
                f"Pay {plan.amount} from {plan.source} to the recipient via {plan.rail_id}. "
                f"Incremental fee {plan.incremental_fee} is absorbed by the operator. "
                "The sender is not debited again."
            ),
            requested_at=utcnow(),
        )
        aggregate.approval_requests[plan.plan_id] = request
        aggregate.record_audit(
            AuditEventType.APPROVAL_REQUESTED,
            ENGINE,
            f"Human approval requested for plan {plan.plan_id}",
            plan_id=plan.plan_id,
        )
        # The only path into AWAITING_APPROVAL: always with a freshly minted plan id.
        aggregate.transition(
            RecoveryState.AWAITING_APPROVAL,
            actor=ENGINE,
            reason=f"plan {plan.plan_id} requires human approval",
        )
        return plan

    @staticmethod
    def _snapshot_drift(aggregate: TransactionAggregate, snapshot: PlanningSnapshot) -> list[str]:
        """Repository facts that changed since the snapshot (empty list = still valid)."""
        reasons: list[str] = []
        if aggregate.revision != snapshot.revision:
            reasons.append(
                f"transaction revision changed ({snapshot.revision} -> {aggregate.revision})"
            )
        if aggregate.state is not snapshot.recovery_state:
            reasons.append(f"state changed ({snapshot.recovery_state} -> {aggregate.state})")
        if aggregate.journal.has_effect(OperationType.RECIPIENT_CREDIT):
            reasons.append("a recipient credit was recorded")
        location = aggregate.journal.funds_location()
        if location is not snapshot.funds_location:
            reasons.append(f"funds moved ({snapshot.funds_location} -> {location})")
        if outstanding_obligation(aggregate) != snapshot.obligation:
            reasons.append("outstanding obligation changed")
        reasons.extend(check_recovery_preconditions(aggregate))
        return list(dict.fromkeys(reasons))

    @staticmethod
    def _discard_planning_result(aggregate: TransactionAggregate, reasons: list[str]) -> NoReturn:
        aggregate.record_audit(
            AuditEventType.PLANNING_RESULT_DISCARDED,
            ENGINE,
            f"Route evaluation discarded, nothing persisted: {'; '.join(reasons)}"[:280],
        )
        raise StalePlanningResultError(reasons)

    @staticmethod
    def _selection_reasons(
        candidate: CandidateRecoveryRoute,
        best: RouteEvaluation,
        passing: list[RouteEvaluation],
        endpoint: EndpointType,
    ) -> tuple[str, ...]:
        fee = candidate.quote.incremental_fee
        liq = candidate.liquidity
        rules = ", ".join(r.rule_id for r in candidate.policy.rule_results)
        reasons = [
            f"policy permitted ({rules} passed, {candidate.policy.policy_version})",
            f"recipient compatible ({endpoint} endpoint)",
            f"sufficient liquidity (available {liq.available} >= required {liq.required})",
            f"rail status {candidate.rail.status}",
        ]
        if fee.amount == min(p.estimated_incremental_cost.amount for p in passing):
            reasons.append(f"lowest incremental cost among {len(passing)} eligible routes ({fee})")
        assert best.score is not None
        reasons.append(
            f"highest synthetic score {best.score.total:.3f} of {len(passing)} eligible routes"
        )
        reasons.append(
            f"pays out from {candidate.source}; sender is not debited again; "
            "fee absorbed by operator"
        )
        return tuple(reasons)

    # ===================================================================== freshness

    def _staleness(self, aggregate: TransactionAggregate, plan: RecoveryPlan) -> list[str]:
        """Why this plan no longer matches reality (empty list = still current)."""
        reasons: list[str] = []
        if plan.expected_revision != aggregate.revision:
            reasons.append(
                f"transaction revision changed ({plan.expected_revision} -> {aggregate.revision})"
            )
        reasons.extend(check_recovery_preconditions(aggregate, plan))
        obligation = outstanding_obligation(aggregate)
        if obligation is not None:
            candidate = build_candidate(
                aggregate, obligation, plan.rail_id, self.registry, self.policy, self.liquidity
            )
            fresh = eligibility_of(candidate)
            for field in EligibilitySnapshot.model_fields:
                before, after = getattr(plan.eligibility, field), getattr(fresh, field)
                if before != after:
                    reasons.append(f"{plan.rail_id} {field} changed ({before} -> {after})")
            if candidate.quote.incremental_fee != plan.incremental_fee:
                reasons.append(
                    f"{plan.rail_id} fee changed "
                    f"({plan.incremental_fee} -> {candidate.quote.incremental_fee})"
                )
        return reasons

    def _mark_stale(
        self, aggregate: TransactionAggregate, plan: RecoveryPlan, reasons: list[str]
    ) -> None:
        aggregate.plan_status[plan.plan_id] = PlanStatus.STALE
        aggregate.record_audit(
            AuditEventType.PLAN_MARKED_STALE,
            ENGINE,
            f"Plan {plan.plan_id} is stale: {'; '.join(reasons)}"[:280],
            plan_id=plan.plan_id,
        )

    def _mark_stale_and_snapshot(
        self, aggregate: TransactionAggregate, plan: RecoveryPlan, reasons: list[str]
    ) -> PlanningSnapshot | None:
        """Caller holds the lock. Returns None if the transaction had to be escalated."""
        self._mark_stale(aggregate, plan, reasons)
        try:
            return self._capture_planning_snapshot(aggregate)
        except (ManualReviewRequiredError, RecoveryPreconditionError):
            return None

    async def _raise_stale(
        self,
        aggregate: TransactionAggregate,
        plan: RecoveryPlan,
        reasons: list[str],
        snapshot: PlanningSnapshot | None,
    ) -> NoReturn:
        """Caller must NOT hold the lock: evaluation of the replacement runs lock-free."""
        replacement: str | None = None
        if snapshot is not None:
            # Never silently substitute: the replacement is a NEW plan needing fresh approval.
            with suppress(NoEligibleRouteError, StalePlanningResultError):
                replacement = (await self._plan_from_snapshot(aggregate, snapshot)).plan_id
        raise StalePlanError(plan.plan_id, reasons, replacement)

    def _escalate(self, aggregate: TransactionAggregate, reason: str) -> None:
        aggregate.record_audit(
            AuditEventType.MANUAL_REVIEW_REQUIRED, ENGINE, f"Manual review: {reason}"[:280]
        )
        aggregate.transition(RecoveryState.MANUAL_REVIEW, actor=ENGINE, reason=reason[:200])

    # ===================================================================== approval

    async def approve_recovery(
        self, plan_id: str, *, plan_hash: str, approver: str, comment: str | None = None
    ) -> ApprovalDecision:
        """Approve EXACTLY this plan (id + content hash) at the current revision."""
        aggregate, plan = self.repository.get_by_plan(plan_id)
        async with aggregate.lock:
            existing = aggregate.approvals.get(plan_id)
            if existing is not None:
                if existing.approved and existing.plan_hash == plan_hash:
                    return existing
                raise ApprovalMismatchError(f"plan {plan_id} was already decided")
            self._require_pending(aggregate, plan, plan_hash)
            reasons = self._staleness(aggregate, plan)
            if not reasons:
                return self._grant_approval(aggregate, plan, approver, comment)
            snapshot = self._mark_stale_and_snapshot(aggregate, plan, reasons)
        await self._raise_stale(aggregate, plan, reasons, snapshot)

    @staticmethod
    def _grant_approval(
        aggregate: TransactionAggregate, plan: RecoveryPlan, approver: str, comment: str | None
    ) -> ApprovalDecision:
        decision = ApprovalDecision(
            decision_id=new_id("dec"),
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            transaction_id=plan.transaction_id,
            transaction_revision=aggregate.revision,
            approver=approver,
            approved=True,
            comment=comment,
            decided_at=utcnow(),
        )
        aggregate.approvals[plan.plan_id] = decision
        aggregate.plan_status[plan.plan_id] = PlanStatus.APPROVED
        aggregate.record_audit(
            AuditEventType.APPROVAL_GRANTED,
            Actor.HUMAN,
            f"{approver} approved plan {plan.plan_id} ({plan.rail_id})",
            plan_id=plan.plan_id,
            approver=approver,
            plan_hash=plan.plan_hash[:12],
            transaction_revision=decision.transaction_revision,
        )
        aggregate.transition(
            RecoveryState.APPROVED, actor=Actor.HUMAN, reason=f"plan {plan.plan_id} approved"
        )
        return decision

    async def reject_recovery(
        self, plan_id: str, *, plan_hash: str, approver: str, comment: str | None = None
    ) -> ApprovalDecision:
        aggregate, plan = self.repository.get_by_plan(plan_id)
        async with aggregate.lock:
            if plan_id in aggregate.approvals:
                raise ApprovalMismatchError(f"plan {plan_id} was already decided")
            self._require_pending(aggregate, plan, plan_hash)
            decision = ApprovalDecision(
                decision_id=new_id("dec"),
                plan_id=plan.plan_id,
                plan_hash=plan.plan_hash,
                transaction_id=plan.transaction_id,
                transaction_revision=aggregate.revision,
                approver=approver,
                approved=False,
                comment=comment,
                decided_at=utcnow(),
            )
            aggregate.approvals[plan_id] = decision
            aggregate.plan_status[plan_id] = PlanStatus.REJECTED
            aggregate.record_audit(
                AuditEventType.APPROVAL_DENIED,
                Actor.HUMAN,
                f"{approver} declined plan {plan_id}",
                plan_id=plan_id,
                approver=approver,
            )
            self._escalate(aggregate, f"operator declined automated recovery plan {plan_id}")
            return decision

    @staticmethod
    def _require_pending(
        aggregate: TransactionAggregate, plan: RecoveryPlan, plan_hash: str
    ) -> None:
        if plan_hash != plan.plan_hash:
            raise ApprovalMismatchError("decision does not match this plan's content hash")
        status = aggregate.plan_status[plan.plan_id]
        if aggregate.current_plan_id != plan.plan_id or status is not PlanStatus.PENDING_APPROVAL:
            raise ApprovalMismatchError(
                f"plan {plan.plan_id} is {status}; only the current pending plan can be decided"
            )
        if aggregate.state is not RecoveryState.AWAITING_APPROVAL:
            raise IllegalTransitionError(f"cannot decide a plan while {aggregate.state}")

    # ===================================================================== execution

    async def execute_recovery(self, plan_id: str) -> ExecutionResult:
        """Pay the outstanding obligation via the approved plan's rail, exactly once.

        1. Admission (under the transaction lock): verify approval, freshness and every
           precondition, then atomically record the execution and enter RECOVERY_EXECUTING.
        2. The slow external payout call runs WITHOUT the lock.
        3. Finalisation (under the lock) for the matching execution id only.
        Repeat calls for the same plan return the recorded execution (replayed=True).
        If we cannot prove whether value moved (cancellation, timeout, transport error) the
        outcome is UNKNOWN -> MANUAL_REVIEW: never DEFINITIVE_FAILED, never retried.
        """
        aggregate, plan = self.repository.get_by_plan(plan_id)
        key = payout_execution_key(plan.transaction_id, plan.plan_id)

        async with aggregate.lock:
            existing = aggregate.executions.get(key)
            if existing is not None:
                aggregate.record_audit(
                    AuditEventType.EXECUTION_REPLAYED,
                    ENGINE,
                    f"Duplicate execute for plan {plan_id}; returning existing execution",
                    execution_id=existing.execution_id,
                )
                return _evolve(existing, replayed=True)
            stale_reasons = self._admit(aggregate, plan)
            if stale_reasons:
                snapshot = self._mark_stale_and_snapshot(aggregate, plan, stale_reasons)
            else:
                execution, request = self._start_execution(aggregate, plan, key)
        if stale_reasons:
            await self._raise_stale(aggregate, plan, stale_reasons, snapshot)

        response = await self._dispatch_payout(aggregate, plan, execution, request)
        return await self._finalise_locked(aggregate, plan, execution, response)

    def _admit(self, aggregate: TransactionAggregate, plan: RecoveryPlan) -> list[str]:
        """Caller holds the lock. Raises if not authorised; returns staleness reasons."""
        status = aggregate.plan_status[plan.plan_id]
        if (
            aggregate.state is not RecoveryState.APPROVED
            or aggregate.current_plan_id != plan.plan_id
            or status is not PlanStatus.APPROVED
        ):
            raise ApprovalRequiredError(
                f"plan {plan.plan_id} is {status} while transaction is {aggregate.state}; "
                "only the current, approved plan can execute"
            )
        approval = aggregate.approvals.get(plan.plan_id)
        if approval is None or not approval.approved:
            raise ApprovalRequiredError(f"plan {plan.plan_id} has no approval")
        if (
            approval.plan_hash != plan.plan_hash
            or approval.transaction_revision != plan.expected_revision
        ):
            raise ApprovalMismatchError("approval does not authorise this exact plan/revision")
        if aggregate.has_execution_in_progress():
            raise ExecutionConflictError("another payout execution is already in flight")
        reasons = self._staleness(aggregate, plan)
        if not reasons:
            # Last check before any write: admission is all-or-nothing.
            assert_transition(aggregate.state, RecoveryState.RECOVERY_EXECUTING)
        return reasons

    @staticmethod
    def _start_execution(
        aggregate: TransactionAggregate, plan: RecoveryPlan, key: str
    ) -> tuple[ExecutionResult, PayoutRequest]:
        """Caller holds the lock and has admitted the plan."""
        execution = ExecutionResult(
            execution_id=new_id("exe"),
            execution_key=key,
            plan_id=plan.plan_id,
            transaction_id=plan.transaction_id,
            rail_id=plan.rail_id,
            status=ExecutionStatus.IN_PROGRESS,
            started_at=utcnow(),
        )
        aggregate.executions[key] = execution
        aggregate.journal.record(
            ExecutionStarted(
                execution_id=execution.execution_id,
                execution_key=key,
                plan_id=plan.plan_id,
                rail_id=plan.rail_id,
            )
        )
        aggregate.plan_status[plan.plan_id] = PlanStatus.EXECUTING
        aggregate.record_audit(
            AuditEventType.EXECUTION_STARTED,
            ENGINE,
            f"Executing remaining leg only: {plan.source} -> {plan.rail_id} -> recipient",
            execution_id=execution.execution_id,
            plan_id=plan.plan_id,
        )
        aggregate.transition(
            RecoveryState.RECOVERY_EXECUTING,
            actor=ENGINE,
            reason=f"admitted execution {execution.execution_id}",
        )
        request = PayoutRequest(
            idempotency_key=key,
            transaction_id=plan.transaction_id,
            rail_id=plan.rail_id,
            source=plan.source,
            amount=plan.amount,
            recipient_token=plan.obligation.recipient_token,
        )
        return execution, request

    async def _dispatch_payout(
        self,
        aggregate: TransactionAggregate,
        plan: RecoveryPlan,
        execution: ExecutionResult,
        request: PayoutRequest,
    ) -> PayoutResponse:
        """Call the provider without the lock. Anything short of a provider answer is UNKNOWN."""
        try:
            return await asyncio.wait_for(
                self.gateway.submit(request), timeout=self.payout_timeout_seconds
            )
        except asyncio.CancelledError:
            # The request may already have reached the provider. Record UNKNOWN before
            # propagating; shielded so a second cancellation cannot leave it IN_PROGRESS.
            ambiguous = _ambiguous_response(
                "CANCELLED_AFTER_DISPATCH", "Payout call cancelled after dispatch."
            )
            await asyncio.shield(self._finalise_locked(aggregate, plan, execution, ambiguous))
            raise
        except TimeoutError:
            return _ambiguous_response(
                "GATEWAY_TIMEOUT", f"No provider response within {self.payout_timeout_seconds}s."
            )
        except Exception as exc:
            return _ambiguous_response("TRANSPORT_ERROR", f"{type(exc).__name__} after dispatch.")

    async def _finalise_locked(
        self,
        aggregate: TransactionAggregate,
        plan: RecoveryPlan,
        execution: ExecutionResult,
        response: PayoutResponse,
    ) -> ExecutionResult:
        async with aggregate.lock:
            return self._finalise(aggregate, plan, execution, response)

    def _finalise(
        self,
        aggregate: TransactionAggregate,
        plan: RecoveryPlan,
        execution: ExecutionResult,
        response: PayoutResponse,
    ) -> ExecutionResult:
        current = aggregate.executions.get(execution.execution_key)
        if (
            current is None
            or current.execution_id != execution.execution_id
            or current.status is not ExecutionStatus.IN_PROGRESS
        ):
            raise ExecutionConflictError("execution record changed while payout was in flight")
        now = utcnow()
        attempt = OperationAttempt(
            attempt_id=new_id("att"),
            transaction_id=plan.transaction_id,
            operation=OperationType.RECIPIENT_CREDIT,
            rail_id=plan.rail_id,
            source=plan.source,
            destination=FundsLocation.RECIPIENT_ENDPOINT,
            amount=plan.amount,
            outcome=response.outcome,
            idempotency_key=execution.execution_key,
            plan_id=plan.plan_id,
            provider_reference=response.provider_reference,
            failure=response.failure,
            started_at=execution.started_at,
            completed_at=now,
        )
        aggregate.journal.record_attempt(attempt)

        credit_key: str | None = None
        if response.outcome is AttemptOutcome.SUCCEEDED:
            effect = FinancialEffect(
                effect_key=effect_key(plan.transaction_id, OperationType.RECIPIENT_CREDIT),
                transaction_id=plan.transaction_id,
                operation=OperationType.RECIPIENT_CREDIT,
                rail_id=plan.rail_id,
                attempt_id=attempt.attempt_id,
                source=plan.source,
                destination=FundsLocation.RECIPIENT_ENDPOINT,
                source_amount=plan.amount,
                destination_amount=plan.amount,
                posted_at=now,
            )
            aggregate.journal.post_effect(effect)  # structurally at most once per transaction
            self.liquidity.consume(plan.rail_id, plan.amount)
            credit_key = effect.effect_key
            status, plan_status, target = (
                ExecutionStatus.SUCCEEDED,
                PlanStatus.EXECUTED,
                RecoveryState.RECOVERED,
            )
            event, summary = (
                AuditEventType.EXECUTION_SUCCEEDED,
                f"Recipient credited {plan.amount} via {plan.rail_id}; sender not debited again",
            )
        elif response.outcome is AttemptOutcome.DEFINITIVE_FAILED:
            status, plan_status, target = (
                ExecutionStatus.FAILED,
                PlanStatus.FAILED,
                RecoveryState.RECOVERY_FAILED,
            )
            event, summary = (
                AuditEventType.EXECUTION_FAILED,
                f"{plan.rail_id} definitively rejected the payout; no value moved",
            )
        else:
            status, plan_status, target = (
                ExecutionStatus.OUTCOME_UNKNOWN,
                PlanStatus.OUTCOME_UNKNOWN,
                RecoveryState.MANUAL_REVIEW,
            )
            event, summary = (
                AuditEventType.EXECUTION_OUTCOME_UNKNOWN,
                f"{plan.rail_id} outcome UNKNOWN; halting value movement for manual review",
            )

        aggregate.journal.record(
            ExecutionFinished(
                execution_id=execution.execution_id,
                plan_id=plan.plan_id,
                status=status,
                attempt_id=attempt.attempt_id,
            )
        )
        result = _evolve(
            current,
            status=status,
            attempt_id=attempt.attempt_id,
            recipient_credit_effect_key=credit_key,
            finished_at=now,
            detail=summarise_failure(response.failure) or "recipient credited",
        )
        aggregate.executions[execution.execution_key] = result
        aggregate.plan_status[plan.plan_id] = plan_status
        aggregate.record_audit(
            event, Actor.RAIL, summary, execution_id=execution.execution_id, plan_id=plan.plan_id
        )
        aggregate.transition(target, actor=ENGINE, reason=f"execution {status}")
        return result

    # ===================================================================== reconciliation

    async def reconcile_transaction(self, transaction_id: str) -> ReconciliationResult:
        """Prove the transaction is complete and correct; only then enter RECONCILED."""
        aggregate = self.repository.get(transaction_id)
        async with aggregate.lock:
            if aggregate.reconciliation is not None and aggregate.reconciliation.reconciled:
                return aggregate.reconciliation
            journal = aggregate.journal
            instruction = aggregate.instruction
            state = derive_state(aggregate)
            debit = journal.effect(OperationType.SENDER_DEBIT)
            credit = journal.effect(OperationType.RECIPIENT_CREDIT)
            checks = [
                ReconciliationCheck(
                    name="single_sender_debit",
                    passed=state.sender_debit_count == 1,
                    detail=f"{state.sender_debit_count} sender debit effect(s)",
                ),
                ReconciliationCheck(
                    name="sender_debit_amount",
                    passed=debit is not None and debit.source_amount == instruction.send_amount,
                    detail=f"expected {instruction.send_amount}",
                ),
                ReconciliationCheck(
                    name="single_fx_and_settlement",
                    passed=journal.count_effects(OperationType.FX_CONVERSION) == 1
                    and journal.count_effects(OperationType.GH_SETTLEMENT) == 1,
                    detail="FX and GH settlement each posted exactly once",
                ),
                ReconciliationCheck(
                    name="single_recipient_credit",
                    passed=state.recipient_credit_count == 1,
                    detail=f"{state.recipient_credit_count} recipient credit effect(s)",
                ),
                ReconciliationCheck(
                    name="recipient_credit_amount",
                    passed=credit is not None
                    and credit.destination_amount == instruction.payout_amount,
                    detail=f"expected {instruction.payout_amount}",
                ),
                ReconciliationCheck(
                    name="funds_at_recipient",
                    passed=state.funds_location is FundsLocation.RECIPIENT_ENDPOINT,
                    detail=f"funds at {state.funds_location}",
                ),
                ReconciliationCheck(
                    name="no_open_or_unknown_execution",
                    passed=not aggregate.has_execution_in_progress()
                    and not state.manual_review_required,
                    detail="no in-flight or UNKNOWN-outcome payout",
                ),
                ReconciliationCheck(
                    name="recovered_state",
                    passed=aggregate.state is RecoveryState.RECOVERED,
                    detail=f"state {aggregate.state}",
                ),
            ]
            reconciled = all(c.passed for c in checks)
            result = ReconciliationResult(
                reconciliation_id=new_id("rec"),
                transaction_id=transaction_id,
                reconciled=reconciled,
                final_state=RecoveryState.RECONCILED if reconciled else aggregate.state,
                checks=tuple(checks),
                sender_debit_count=state.sender_debit_count,
                recipient_credit_count=state.recipient_credit_count,
                duplicate_sender_debits=max(0, state.sender_debit_count - 1),
                funds_location=state.funds_location,
                reconciled_at=utcnow(),
            )
            if not reconciled:
                failed = ",".join(c.name for c in checks if not c.passed)
                aggregate.record_audit(
                    AuditEventType.RECONCILIATION_FAILED,
                    ENGINE,
                    f"Reconciliation not possible yet: {failed}"[:280],
                    failed_checks=failed,
                )
                return result
            journal.record(ReconciliationCompleted(reconciliation_id=result.reconciliation_id))
            aggregate.reconciliation = result
            aggregate.record_audit(
                AuditEventType.RECONCILED,
                ENGINE,
                "Ledger reconciled: 1 sender debit, 1 recipient credit, 0 duplicates",
                duplicate_sender_debits=result.duplicate_sender_debits,
            )
            aggregate.transition(
                RecoveryState.RECONCILED, actor=ENGINE, reason="all reconciliation checks passed"
            )
            return result

    # ===================================================================== helpers

    def _require_obligation(
        self, transaction_id: str
    ) -> tuple[TransactionAggregate, OutstandingObligation]:
        aggregate = self.repository.get(transaction_id)
        obligation = outstanding_obligation(aggregate)
        if obligation is None:
            raise RecoveryPreconditionError(["transaction has no outstanding payout obligation"])
        return aggregate, obligation
