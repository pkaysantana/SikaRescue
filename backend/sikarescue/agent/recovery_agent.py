"""RecoveryAgent: ONE Pydantic AI agent that investigates, requests deterministic analysis and
explains the resulting immutable plan as structured `RecoveryAdvice`.

It holds four narrowly scoped tools (see `toolbox`) and no others. Its output is validated
field-by-field against the deterministic plan; on any mismatch the model is asked to correct
exactly those fields (`ModelRetry`), and persistent mismatch fails the run (caller falls back).
Nothing executable is ever derived from its output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic_ai import Agent, ModelRetry, RunContext

from sikarescue import telemetry
from sikarescue.agent.toolbox import RecoveryAgentToolbox
from sikarescue.models import (
    ModelAuditSummary,
    ModelPlanView,
    ModelTransactionView,
    RecoveryAdvice,
    RecoveryDecisionContext,
)

RELIABILITY_TOLERANCE = 0.0005  # the packet states probabilities to 4 dp

INSTRUCTIONS = """\
You are SikaRescue's recovery advisor for failed cross-border payouts. All data is synthetic.
You ADVISE. The deterministic SikaRescue core decides, and a human approves. You cannot
approve, execute or reconcile anything, and you cannot change any amount, fee, route or policy.

Work in this order:
1. inspect_incident: what failed, what already succeeded, where the funds are now.
2. evaluate_recovery_options: runs the deterministic analysis (hard constraints, stress
   simulation, scoring) and seals an immutable plan. You cannot influence its result.
3. Optionally inspect_recovery_plan or get_recovery_audit_summary if you need more detail.
4. Return RecoveryAdvice.

Rules for RecoveryAdvice:
- Copy identifiers, codes and numbers EXACTLY from tool results. Never compute, re-round or
  estimate a value. recommended_route is the selected plan's rail_id.
- reason_codes: cite only codes listed by the analysis. rejected_routes: list every rejected
  route with its exact reasons.
- Never suggest restarting from the sender, re-debiting, or retrying a rejected route.
- Write plainly for an operations approver, at most 3 sentences (under 400 characters) per
  text field. Your inputs contain no recipient personal data; never invent any.
"""


@dataclass
class RecoveryAgentDeps:
    toolbox: RecoveryAgentToolbox
    # Every advice draft rejected by the validator (for the narrative and telemetry).
    rejections: list[list[str]] = field(default_factory=list)


recovery_agent = Agent(
    name="recovery_agent",
    deps_type=RecoveryAgentDeps,
    output_type=RecoveryAdvice,
    instructions=INSTRUCTIONS,
    retries={"tools": 1, "output": 2},
)

AGENT_TOOL_NAMES = (
    "inspect_incident",
    "evaluate_recovery_options",
    "inspect_recovery_plan",
    "get_recovery_audit_summary",
)


def _scoped(ctx: RunContext[RecoveryAgentDeps], transaction_id: str) -> RecoveryAgentToolbox:
    toolbox = ctx.deps.toolbox
    if transaction_id != toolbox.transaction_id:
        raise ModelRetry(
            f"This session is scoped to transaction {toolbox.transaction_id}; use that id."
        )
    return toolbox


@recovery_agent.tool
async def inspect_incident(
    ctx: RunContext[RecoveryAgentDeps], transaction_id: str
) -> ModelTransactionView:
    """Inspect the failed payment: which legs succeeded or failed, where the funds are now,
    and whether restarting from the sender would be safe. Read-only."""
    toolbox = _scoped(ctx, transaction_id)
    with telemetry.span("agent_tool_inspect_incident", transaction_id=transaction_id):
        return await toolbox.inspect_incident()


@recovery_agent.tool
async def evaluate_recovery_options(
    ctx: RunContext[RecoveryAgentDeps], transaction_id: str
) -> RecoveryDecisionContext | dict[str, Any]:
    """Run SikaRescue's deterministic recovery analysis: filter routes by hard constraints,
    stress-test survivors by simulation, score them and seal ONE immutable plan that needs
    human approval. Returns the evidence: completed effects, the outstanding obligation,
    candidate and rejected routes, stress results and the selected plan. Idempotent."""
    toolbox = _scoped(ctx, transaction_id)
    with telemetry.span("agent_tool_evaluate_routes", transaction_id=transaction_id) as span:
        result = await toolbox.evaluate_recovery_options()
        if toolbox.context is not None:
            span.set(
                plan_id=toolbox.context.selected_plan.plan_id,
                selected_route=toolbox.context.selected_plan.rail_id.value,
                rejected_route_count=len(toolbox.context.rejected_routes),
                context_mode=toolbox.context_mode,
            )
        return result


@recovery_agent.tool
async def inspect_recovery_plan(
    ctx: RunContext[RecoveryAgentDeps], transaction_id: str
) -> ModelPlanView:
    """Inspect the immutable plan produced by the analysis: route, amount, fee, eligibility
    and the deterministic selection reasons. Read-only."""
    toolbox = _scoped(ctx, transaction_id)
    with telemetry.span("agent_tool_inspect_plan", transaction_id=transaction_id):
        return await toolbox.inspect_recovery_plan()


@recovery_agent.tool
async def get_recovery_audit_summary(
    ctx: RunContext[RecoveryAgentDeps], transaction_id: str
) -> ModelAuditSummary:
    """Read the sanitised audit timeline of this transaction. Read-only."""
    toolbox = _scoped(ctx, transaction_id)
    with telemetry.span("agent_tool_audit_summary", transaction_id=transaction_id):
        return await toolbox.get_recovery_audit_summary()


def advice_problems(advice: RecoveryAdvice, context: RecoveryDecisionContext) -> list[str]:
    """Every way the advice contradicts the deterministic plan (empty list = consistent)."""
    plan = context.selected_plan
    problems: list[str] = []

    def expect(name: str, got: object, want: object) -> None:
        if got != want:
            problems.append(f"{name} must be {want} (got {got})")

    expect("transaction_id", advice.transaction_id, context.transaction_id)
    expect("plan_id", advice.plan_id, plan.plan_id)
    expect("funds_location", advice.funds_location, context.funds_location)
    expect("recommended_route", advice.recommended_route, plan.rail_id)
    expect("incremental_fee_gbp", advice.incremental_fee_gbp, plan.incremental_fee_gbp)
    expect(
        "estimated_arrival_seconds", advice.estimated_arrival_seconds, plan.quoted_arrival_seconds
    )
    if abs(advice.simulated_reliability - plan.simulated_reliability) > RELIABILITY_TOLERANCE:
        problems.append(
            f"simulated_reliability must be {plan.simulated_reliability} "
            f"(got {advice.simulated_reliability})"
        )
    unsupported = [c.value for c in advice.reason_codes if c not in context.reason_codes]
    if unsupported:
        problems.append(f"reason_codes not supported by the analysis: {', '.join(unsupported)}")
    want = {r.rail_id.value: sorted(x.value for x in r.reasons) for r in context.rejected_routes}
    got = {r.rail_id.value: sorted(x.value for x in r.reasons) for r in advice.rejected_routes}
    if got != want or len(advice.rejected_routes) != len(context.rejected_routes):
        problems.append(f"rejected_routes must be exactly {want} (got {got})")
    return problems


@recovery_agent.output_validator
async def _consistent_with_plan(
    ctx: RunContext[RecoveryAgentDeps], advice: RecoveryAdvice
) -> RecoveryAdvice:
    toolbox = ctx.deps.toolbox
    if toolbox.context is None:
        raise ModelRetry("Call evaluate_recovery_options before returning RecoveryAdvice.")
    # Hard stop, not a retry: PII must never appear in anything shown or logged.
    toolbox.release(advice)
    problems = advice_problems(advice, toolbox.context)
    if problems:
        ctx.deps.rejections.append(problems)
        telemetry.event(
            "recovery_advice_rejected",
            level="warn",
            transaction_id=toolbox.transaction_id,
            problems="; ".join(problems)[:500],
        )
        raise ModelRetry(
            "RecoveryAdvice contradicts the deterministic plan. Fix exactly these fields: "
            + "; ".join(problems)
        )
    return advice
