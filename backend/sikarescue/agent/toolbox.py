"""The complete set of capabilities the model can reach, bound to ONE transaction.

Every operation is read-only, or runs the deterministic planning pipeline (idempotent, no
financial effect; its plan still needs human approval of its exact hash). Every result is an
allowlisted DTO released through the PII gate. By design there is no way to approve, execute,
reconcile or write the journal from here.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from sikarescue.models import (
    ModelAuditSummary,
    ModelPlanView,
    ModelTransactionView,
    RecoveryDecisionContext,
    RecoveryPlan,
)
from sikarescue.services.model_boundary import (
    build_decision_context,
    build_model_audit_summary,
    build_model_plan_view,
    build_model_transaction_view,
    build_verbose_decision_context,
    release_to_model,
)
from sikarescue.services.recovery import RecoveryService
from sikarescue.services.repository import TransactionAggregate

ContextMode = Literal["compact", "verbose"]


class RecoveryAgentToolbox:
    def __init__(
        self,
        service: RecoveryService,
        transaction_id: str,
        *,
        context_mode: ContextMode = "compact",
    ) -> None:
        self._service = service
        self.transaction_id = transaction_id
        self.context_mode: ContextMode = context_mode
        self._planning: asyncio.Future[RecoveryPlan] | None = None
        # Deterministic facts of the evaluated plan: what the agent's advice is checked against.
        self.context: RecoveryDecisionContext | None = None
        self.calls: list[str] = []

    # ------------------------------------------------------------------ orchestrator-only

    async def ensure_plan(self) -> RecoveryPlan:
        """Run (once) or join the deterministic planning pipeline.

        Shielded: if the agent run is cancelled (e.g. its timeout fires) mid-analysis, the
        deterministic planning still completes and the fallback path reuses its result.
        """
        if self._planning is None:
            self._planning = asyncio.ensure_future(
                self._service.create_recovery_plan(self.transaction_id)
            )
        return await asyncio.shield(self._planning)

    def planning_error(self) -> BaseException | None:
        task = self._planning
        if task is None or not task.done() or task.cancelled():
            return None
        return task.exception()

    def decision_context(self, plan: RecoveryPlan) -> RecoveryDecisionContext:
        return build_decision_context(self._aggregate(), plan)

    def _aggregate(self) -> TransactionAggregate:
        return self._service.repository.get(self.transaction_id)

    def release[T: Any](self, payload: T) -> T:
        """The PII gate for anything crossing to (or coming back from) the model."""
        return release_to_model(payload, self._aggregate().instruction)

    # ------------------------------------------------------------------ model-reachable

    async def inspect_incident(self) -> ModelTransactionView:
        self.calls.append("inspect_incident")
        return self.release(build_model_transaction_view(self._aggregate()))

    async def evaluate_recovery_options(self) -> RecoveryDecisionContext | dict[str, Any]:
        self.calls.append("evaluate_recovery_options")
        plan = await self.ensure_plan()
        self.context = self.decision_context(plan)
        if self.context_mode == "verbose":  # unoptimised A/B baseline only
            return self.release(build_verbose_decision_context(self._aggregate(), plan))
        return self.release(self.context)

    async def inspect_recovery_plan(self) -> ModelPlanView:
        self.calls.append("inspect_recovery_plan")
        plan = await self.ensure_plan()
        status = self._aggregate().plan_status[plan.plan_id]
        return self.release(build_model_plan_view(plan, status))

    async def get_recovery_audit_summary(self) -> ModelAuditSummary:
        self.calls.append("get_recovery_audit_summary")
        return self.release(build_model_audit_summary(self._aggregate()))
