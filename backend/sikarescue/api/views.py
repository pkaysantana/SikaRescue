"""Read-only presentation of the demo session for the web UI.

Every value here is taken from the deterministic domain objects (journal-derived state, the
immutable plan, the advisory outcome, the audit timeline); nothing is computed that the core
did not decide. Money and percentages are formatted server-side so the frontend never does
financial arithmetic. Recipient identity is never included: only the opaque token.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from sikarescue import telemetry
from sikarescue.agent.advisor import AdvisoryOutcome, Orchestrator
from sikarescue.agent.evidence import EvidenceExtraction
from sikarescue.api.control_views import (
    CounterfactualView,
    EffectNodeView,
    FrontierView,
    FundsPositionView,
    IncidentView,
    PreviewView,
    build_counterfactual_view,
    build_frontier_view,
    build_funds_position_view,
    build_graph_view,
    build_incident_view,
    build_preview_view,
)
from sikarescue.api.formatting import money, pct
from sikarescue.demo_data.incidents import SCENARIO_LABELS, IncidentScenario
from sikarescue.demo_data.sk10421 import SCENARIO_ID, DemoWorld
from sikarescue.models import (
    AuditEventType,
    ExecutionResult,
    FundsCertainty,
    FundsLocation,
    OperationType,
    RailId,
    ReconciliationResult,
    RecoveryState,
    RouteEvaluation,
    TransactionState,
)
from sikarescue.services.repository import TransactionAggregate


class View(BaseModel):
    model_config = ConfigDict(frozen=True)


NextAction = Literal["classify", "analyse", "approve", "execute", "done", "manual_review"]

_OPERATION_LABELS = {
    OperationType.SENDER_DEBIT: "Sender debit",
    OperationType.FX_CONVERSION: "GBP → GHS FX",
    OperationType.GH_SETTLEMENT: "Ghana settlement",
    OperationType.RECIPIENT_CREDIT: "Payout",
}
_COUNTRY_NAMES = {"GB": "United Kingdom", "GH": "Ghana"}
_ENDPOINT_LABELS = {"MOBILE_MONEY": "Mobile Money", "BANK_ACCOUNT": "Bank account"}
_SCENARIO_LABELS = {
    "NORMAL": "Normal",
    "CONGESTION": "Congestion",
    "RAIL_DEGRADATION": "Rail degradation",
    "RAIL_OUTAGE": "Rail outage",
    "LATENCY_SPIKE": "Latency spike",
    "REGIONAL_DISRUPTION": "Regional disruption",
    "CORRELATED_FAILURE": "Correlated failure",
}


# ------------------------------------------------------------------ transaction + journey


class TransactionSummary(View):
    scenario_id: str  # the synthetic template, e.g. SK-10421
    payment_instance_id: str  # the authoritative id every key binds to, e.g. SK-10421-3fa9c2d1
    send_amount: str
    payout_amount: str
    fx_rate: str
    origin: str
    destination: str
    recipient_rail: str
    recipient_token: str


class JourneyLeg(View):
    label: str
    rail_id: str
    rail_name: str
    status: str  # SUCCESS | FAILED | UNKNOWN | IN_PROGRESS
    detail: str | None
    destination: str
    is_recovery: bool
    funds_here: bool  # the LAST CONFIRMED funds location (see Diagnosis.funds_certainty)


class Diagnosis(View):
    state: RecoveryState
    funds_location: str  # last location proven by the journal
    funds_certainty: FundsCertainty
    funds_label: str  # "Funds are here" (PROVEN) | "Last confirmed here" (UNCERTAIN)
    available_for_automatic_action: bool
    uncertainty_reason: str | None
    funds_at_recipient: bool
    sender_debited: bool
    sender_debit_count: int
    recipient_credited: bool
    recipient_credit_count: int
    duplicate_sender_debits: int
    safe_to_restart_from_origin: bool
    failed_leg: str | None
    outstanding: str | None


# ------------------------------------------------------------------ analysis


class StressResult(View):
    scenario: str
    success: str
    within_sla: str


class RouteOption(View):
    rail_id: str
    rail_name: str
    eligible: bool
    selected: bool
    rank: int | None
    rejection_reasons: list[str]
    rejection_details: list[str]
    fee: str
    quoted_arrival_seconds: int
    quoted_reliability: str
    simulated_success: str | None
    within_sla: str | None
    p95_arrival_seconds: float | None
    score: str | None
    stress: list[StressResult]


class ComputeSummary(View):
    backend: str
    configured_backend: str
    fallback_from: str | None
    fallback_reason: str | None
    simulated_outcomes: int
    routes_simulated: int
    scenarios: list[str]
    trials_per_scenario: int
    parallel_jobs: int
    elapsed_seconds: float
    remote_compute_seconds: float | None
    function_ref: str | None
    rejected_routes_simulated: int


class PlanSummary(View):
    plan_id: str
    plan_hash: str
    plan_hash_short: str
    bound_revision: int
    status: str
    source: str
    rail_id: str
    rail_name: str
    amount: str
    incremental_fee: str
    fee_bearer: str
    quoted_arrival_seconds: int
    selection_reasons: list[str]
    approval_summary: str


class AdviceCard(View):
    orchestrator: Orchestrator
    ai_used: bool
    label: str
    fallback_reason: str | None
    # Only set when a model call actually produced the advice.
    model: str | None
    provider_model: str | None  # as reported by the provider on the actual responses
    gateway_route: str | None
    via_gateway: bool
    trace_id: str | None  # only when the trace was exported to Logfire
    tool_calls: list[str]
    incident_summary: str
    why_origin_retry_is_unsafe: str
    recommended_route: str
    reason_codes: list[str]
    stress_summary: str
    operator_message: str


class Analysis(View):
    routes: list[RouteOption]
    compute: ComputeSummary
    plan: PlanSummary
    advice: AdviceCard


# ------------------------------------------------------------------ approval / execution / proof


class ApprovalSummary(View):
    approver: str
    plan_id: str
    plan_hash_short: str
    decided_at: datetime


class ExecutionSummary(View):
    execution_id: str
    status: str
    rail_id: str
    rail_name: str
    detail: str | None
    started_at: datetime
    finished_at: datetime | None


class ReconciliationCheckView(View):
    name: str
    passed: bool
    detail: str


class ReconciliationSummary(View):
    reconciled: bool
    checks: list[ReconciliationCheckView]
    sender_debit_count: int
    recipient_credit_count: int
    duplicate_sender_debits: int
    funds_location: str


class StateTransition(View):
    at: datetime
    from_state: str
    to_state: str
    actor: str


class TelemetryInfo(View):
    enabled: bool
    exporting: bool
    detail: str


class ScenarioOption(View):
    id: str
    label: str


class ScenarioInfo(View):
    current: str | None  # None: a pre-classified payment (no provider incident)
    options: list[ScenarioOption]


class DemoView(View):
    state: RecoveryState
    next_action: NextAction
    notice: str | None  # why the flow is where it is, when that is not obvious
    scenario: ScenarioInfo
    incident: IncidentView | None
    funds_position: FundsPositionView
    effect_graph: list[EffectNodeView]
    frontier: FrontierView
    preview: PreviewView | None  # current vs proposed ledger for the visible plan
    counterfactual: CounterfactualView
    configured_compute_backend: str
    agent_mode: str
    transaction: TransactionSummary
    journey: list[JourneyLeg]
    diagnosis: Diagnosis
    analysis: Analysis | None
    approval: ApprovalSummary | None
    execution: ExecutionSummary | None
    reconciliation: ReconciliationSummary | None
    transitions: list[StateTransition]
    payout_calls: int  # provider calls for THIS payment instance
    telemetry: TelemetryInfo
    resets: int
    retired_instance_ids: list[str]  # instances that reached the provider; never reused


# ------------------------------------------------------------------ builders


def _next_action(state: RecoveryState, plan_visible: bool, evidence_pending: bool) -> NextAction:
    if evidence_pending:
        return "classify"  # nothing may be planned until the provider response is classified
    if state in (RecoveryState.FAILED, RecoveryState.DIAGNOSING, RecoveryState.RECOVERY_FAILED):
        return "analyse"
    if state in (RecoveryState.AWAITING_APPROVAL, RecoveryState.APPROVED) and not plan_visible:
        return "analyse"  # e.g. a stale plan was replaced: the replacement must be reviewed
    if state is RecoveryState.AWAITING_APPROVAL:
        return "approve"
    if state in (RecoveryState.APPROVED, RecoveryState.RECOVERY_EXECUTING):
        return "execute"
    if state in (RecoveryState.RECOVERED, RecoveryState.RECONCILED):
        return "done"
    return "manual_review"


def _notice(
    aggregate: TransactionAggregate, state: TransactionState, plan_visible: bool
) -> str | None:
    if state.evidence_pending:
        return (
            "MOMO_A answered the payout, but the response is not classified yet, so the funds "
            "position is unproven. Classify the provider evidence before anything is planned."
        )
    current = aggregate.current_plan
    awaiting = aggregate.state in (RecoveryState.AWAITING_APPROVAL, RecoveryState.APPROVED)
    if awaiting and not plan_visible and current is not None:
        stale = next(
            (
                e.summary
                for e in reversed(aggregate.audit)
                if e.event_type is AuditEventType.PLAN_MARKED_STALE
                and e.data.get("plan_id") == current.supersedes_plan_id
            ),
            None,
        )
        return (
            f"{stale or 'The previous plan is no longer current'}. A replacement plan "
            f"({current.plan_id}) was prepared. Analyse it, then approve it: the earlier "
            "approval does not carry over."
        )
    if aggregate.state is RecoveryState.RECOVERY_FAILED:
        return (
            "The recovery payout was definitively rejected before acceptance, so no value "
            "moved. Analyse again to plan a different route; the failed rail is excluded."
        )
    if state.funds_certainty is FundsCertainty.UNCERTAIN:
        reason = state.uncertainty_reason or "the funds position is uncertain"
        return (
            f"{_sentence(reason)}. Automatic action is disabled; an operator must confirm "
            "the outcome with the provider. The payout is never retried automatically."
        )
    return None


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def _rail_name(world: DemoWorld, rail_id: str) -> str:
    return world.registry.get(RailId(rail_id)).display_name.replace("->", "→")


def _journey(world: DemoWorld) -> list[JourneyLeg]:
    state = world.service.get_transaction_state(world.transaction_id)
    legs = list(state.legs)
    # The last CONFIRMED location: the destination of the last leg that provably moved value.
    moved = [i for i, leg in enumerate(legs) if leg.status.value == "SUCCESS"]
    funds_index = moved[-1] if moved else None
    return [
        JourneyLeg(
            label=_OPERATION_LABELS[leg.operation],
            rail_id=leg.rail_id.value,
            rail_name=_rail_name(world, leg.rail_id.value),
            status=leg.status.value,
            detail=leg.failure_summary,
            destination=leg.destination.value,
            is_recovery=leg.is_recovery,
            funds_here=i == funds_index and leg.destination.value == state.funds_location.value,
        )
        for i, leg in enumerate(legs)
    ]


def _route(world: DemoWorld, e: RouteEvaluation, selected_route: str) -> RouteOption:
    return RouteOption(
        rail_id=e.rail_id.value,
        rail_name=_rail_name(world, e.rail_id.value),
        eligible=e.passed,
        selected=e.route_id == selected_route,
        rank=e.rank,
        rejection_reasons=[r.value for r in e.rejection_reasons],
        rejection_details=list(e.rejection_details),
        fee=money(e.estimated_incremental_cost),
        quoted_arrival_seconds=e.expected_latency_seconds,
        quoted_reliability=pct(e.quoted_reliability) or "",
        simulated_success=pct(e.simulated_success_probability),
        within_sla=pct(e.simulated_within_sla_probability),
        p95_arrival_seconds=round(e.p95_latency_seconds, 1) if e.p95_latency_seconds else None,
        score=f"{e.score.total:.3f}" if e.score else None,
        stress=[
            StressResult(
                scenario=_SCENARIO_LABELS.get(
                    r.scenario.scenario_id.value, r.scenario.scenario_id.value
                ),
                success=pct(r.simulated_success_probability) or "",
                within_sla=pct(r.recovery_within_sla_probability) or "",
            )
            for r in e.scenario_results[1:]
        ],
    )


def _analysis(world: DemoWorld, advisory: AdvisoryOutcome, exporting: bool) -> Analysis:
    plan = advisory.plan
    aggregate = world.repository.get(world.transaction_id)
    compute = plan.compute
    assert compute is not None
    advice = advisory.advice
    used = advisory.orchestrator is Orchestrator.PYDANTIC_AI
    label = {
        Orchestrator.PYDANTIC_AI: "Pydantic AI",
        Orchestrator.DETERMINISTIC_FALLBACK: "Deterministic fallback — AI provider unavailable",
        Orchestrator.DETERMINISTIC: "Deterministic explanation — AI not enabled",
    }[advisory.orchestrator]
    return Analysis(
        routes=[_route(world, e, plan.route_id) for e in plan.evaluations],
        compute=ComputeSummary(
            backend=compute.backend,
            configured_backend=world.service.compute_backend,
            fallback_from=compute.fallback_from,
            fallback_reason=compute.fallback_reason,
            simulated_outcomes=compute.simulated_trials,
            routes_simulated=compute.routes_simulated,
            scenarios=[_SCENARIO_LABELS.get(s.value, s.value) for s in compute.scenarios],
            trials_per_scenario=compute.trials_per_scenario,
            parallel_jobs=compute.parallel_jobs,
            elapsed_seconds=compute.elapsed_seconds,
            remote_compute_seconds=compute.remote_compute_seconds,
            function_ref=compute.function_ref,
            rejected_routes_simulated=sum(
                len(e.scenario_results) for e in plan.evaluations if not e.passed
            ),
        ),
        plan=PlanSummary(
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            plan_hash_short=plan.plan_hash[:12],
            bound_revision=plan.expected_revision,
            status=aggregate.plan_status[plan.plan_id].value,
            source=plan.source.value,
            rail_id=plan.rail_id.value,
            rail_name=_rail_name(world, plan.rail_id.value),
            amount=money(plan.amount),
            incremental_fee=money(plan.incremental_fee),
            fee_bearer=plan.fee_bearer.value,
            quoted_arrival_seconds=plan.expected_latency_seconds,
            selection_reasons=list(plan.selection_reasons),
            approval_summary=aggregate.approval_requests[plan.plan_id].summary,
        ),
        advice=AdviceCard(
            orchestrator=advisory.orchestrator,
            ai_used=used,
            label=label,
            fallback_reason=advisory.fallback_reason,
            model=advisory.model if used else None,
            provider_model=advisory.provider_model if used else None,
            gateway_route=advisory.gateway_route if used and advisory.via_gateway else None,
            via_gateway=used and advisory.via_gateway,
            trace_id=advisory.trace_id if exporting else None,
            tool_calls=list(advisory.tool_calls) if used else [],
            incident_summary=advice.incident_summary,
            why_origin_retry_is_unsafe=advice.why_origin_retry_is_unsafe,
            recommended_route=advice.recommended_route.value,
            reason_codes=[c.value for c in advice.reason_codes],
            stress_summary=advice.stress_summary,
            operator_message=advice.operator_message,
        ),
    )


def _execution(world: DemoWorld, execution: ExecutionResult) -> ExecutionSummary:
    return ExecutionSummary(
        execution_id=execution.execution_id,
        status=execution.status.value,
        rail_id=execution.rail_id.value,
        rail_name=_rail_name(world, execution.rail_id.value),
        detail=execution.detail,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
    )


def _reconciliation(result: ReconciliationResult) -> ReconciliationSummary:
    return ReconciliationSummary(
        reconciled=result.reconciled,
        checks=[
            ReconciliationCheckView(name=c.name, passed=c.passed, detail=c.detail)
            for c in result.checks
        ],
        sender_debit_count=result.sender_debit_count,
        recipient_credit_count=result.recipient_credit_count,
        duplicate_sender_debits=result.duplicate_sender_debits,
        funds_location=result.funds_location.value,
    )


def build_view(
    world: DemoWorld,
    advisory: AdvisoryOutcome | None,
    *,
    agent_mode: str,
    resets: int,
    retired_instance_ids: list[str] | tuple[str, ...] = (),
    scenario: IncidentScenario | None = None,
    extraction: EvidenceExtraction | None = None,
) -> DemoView:
    aggregate = world.repository.get(world.transaction_id)
    instruction = aggregate.instruction
    state = world.service.get_transaction_state(world.transaction_id)
    obligation = state.outstanding_obligation
    status = telemetry.status()
    plan = advisory.plan if advisory else None
    # Only the plan this session analysed is shown; a stale or superseded plan is not.
    if plan is not None and aggregate.current_plan_id != plan.plan_id:
        advisory, plan = None, None
    approval = aggregate.approvals.get(plan.plan_id) if plan else None
    execution = next(
        (e for e in aggregate.executions.values() if plan and e.plan_id == plan.plan_id), None
    )
    proven = state.funds_certainty is FundsCertainty.PROVEN
    names = {rail.rail_id: _rail_name(world, rail.rail_id.value) for rail in world.registry.all()}
    service = world.service
    frontier = service.get_safe_action_frontier(world.transaction_id)
    preview = (
        build_preview_view(service.preview_recovery(plan.plan_id), names)
        if plan is not None
        and aggregate.plan_status[plan.plan_id].value in ("PENDING_APPROVAL", "APPROVED")
        else None
    )
    return DemoView(
        state=aggregate.state,
        next_action=_next_action(aggregate.state, plan is not None, state.evidence_pending),
        notice=_notice(aggregate, state, plan is not None),
        scenario=ScenarioInfo(
            current=scenario.value if scenario and aggregate.incident else None,
            options=[
                ScenarioOption(id=s.value, label=SCENARIO_LABELS[s]) for s in IncidentScenario
            ],
        ),
        incident=build_incident_view(
            aggregate.incident,
            extraction,
            aggregate.incident_classification,
            exporting=status.exporting,
        ),
        funds_position=build_funds_position_view(frontier),
        effect_graph=build_graph_view(service.get_effect_graph(world.transaction_id), names),
        frontier=build_frontier_view(frontier, names),
        preview=preview,
        counterfactual=build_counterfactual_view(
            service.compare_naive_retry(world.transaction_id), names
        ),
        configured_compute_backend=world.service.compute_backend,
        agent_mode=agent_mode,
        transaction=TransactionSummary(
            scenario_id=SCENARIO_ID,
            payment_instance_id=instruction.transaction_id,
            send_amount=money(instruction.send_amount),
            payout_amount=money(instruction.payout_amount),
            fx_rate=f"{instruction.fx_rate:.2f}",
            origin=_COUNTRY_NAMES[instruction.origin_country.value],
            destination=_COUNTRY_NAMES[instruction.destination_country.value],
            recipient_rail=_ENDPOINT_LABELS[instruction.recipient.endpoint_type.value],
            recipient_token=instruction.recipient_token,
        ),
        journey=_journey(world),
        diagnosis=Diagnosis(
            state=aggregate.state,
            funds_location=state.funds_location.value,
            funds_certainty=state.funds_certainty,
            funds_label="Funds are here" if proven else "Last confirmed here",
            available_for_automatic_action=state.available_for_automatic_action,
            uncertainty_reason=state.uncertainty_reason,
            funds_at_recipient=state.funds_location is FundsLocation.RECIPIENT_ENDPOINT,
            sender_debited=state.sender_debited,
            sender_debit_count=state.sender_debit_count,
            recipient_credited=state.recipient_credited,
            recipient_credit_count=state.recipient_credit_count,
            duplicate_sender_debits=max(0, state.sender_debit_count - 1),
            safe_to_restart_from_origin=state.safe_to_restart_from_origin,
            failed_leg=state.failed_leg.value if state.failed_leg else None,
            outstanding=money(obligation.amount) if obligation else None,
        ),
        analysis=_analysis(world, advisory, status.exporting) if advisory else None,
        approval=(
            ApprovalSummary(
                approver=approval.approver,
                plan_id=approval.plan_id,
                plan_hash_short=approval.plan_hash[:12],
                decided_at=approval.decided_at,
            )
            if approval and approval.approved
            else None
        ),
        execution=_execution(world, execution) if execution else None,
        reconciliation=_reconciliation(aggregate.reconciliation)
        if aggregate.reconciliation
        else None,
        transitions=[
            StateTransition(
                at=e.timestamp,
                from_state=str(e.data.get("from_state")),
                to_state=str(e.data.get("to_state")),
                actor=e.actor.value,
            )
            for e in aggregate.audit
            if e.event_type is AuditEventType.STATE_TRANSITION
        ],
        payout_calls=sum(
            1 for r in world.gateway.requests if r.transaction_id == world.transaction_id
        ),
        telemetry=TelemetryInfo(
            enabled=status.enabled, exporting=status.exporting, detail=status.detail
        ),
        resets=resets,
        retired_instance_ids=list(retired_instance_ids),
    )
