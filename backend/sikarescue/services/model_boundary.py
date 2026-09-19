"""The model boundary: the only way transaction data may be prepared for an LLM.

Primary protection is ALLOWLISTING: views are built field-by-field from deterministic
state and never copy recipient identity. `assert_no_recipient_pii` is defence in depth
that scans the final serialised payload for any raw PII value before it leaves, and
`release_to_model` is the single gate every agent tool result passes through.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from sikarescue.errors import PIILeakError
from sikarescue.models import (
    AdviceReasonCode,
    AttemptOutcome,
    CompletedEffectView,
    ComputeView,
    DecisionRouteView,
    DomainModel,
    FailureView,
    FeeBearer,
    FundsLocation,
    ModelAuditEntryView,
    ModelAuditSummary,
    ModelLegView,
    ModelPlanView,
    ModelRouteView,
    ModelTransactionView,
    Money,
    ObligationView,
    PaymentTransaction,
    PlanStatus,
    RailStatus,
    RecoveryDecisionContext,
    RecoveryPlan,
    RejectedRouteView,
    RouteEvaluation,
    SelectedPlanView,
    StressResultView,
    TransactionState,
)
from sikarescue.services.diagnosis import derive_state, summarise_failure
from sikarescue.services.repository import TransactionAggregate

REDACTED = "[REDACTED]"

# Stated to the model with every decision packet. Enforced by code, not by the model.
DECISION_INVARIANTS = (
    "Never replay a completed value-moving effect: each succeeds at most once per transaction.",
    "The sender is never debited again; recovery continues from where the funds are now.",
    "Only the outstanding obligation is paid, exactly once, from its current source.",
    "Only the deterministic plan can execute, and only after a human approves its exact hash.",
)


def build_model_transaction_view(aggregate: TransactionAggregate) -> ModelTransactionView:
    instruction = aggregate.instruction
    state = derive_state(aggregate)
    last_failed = next(
        (
            a
            for a in reversed(aggregate.journal.attempts())
            if a.failure is not None and a.rail_id == state.failed_leg
        ),
        None,
    )
    return ModelTransactionView(
        transaction_id=instruction.transaction_id,
        recipient_token=instruction.recipient_token,
        corridor=instruction.corridor,
        send_currency=instruction.send_amount.currency,
        payout_currency=instruction.payout_amount.currency,
        send_amount=f"{instruction.send_amount.amount:.2f}",
        payout_amount=f"{instruction.payout_amount.amount:.2f}",
        endpoint_type=instruction.recipient.endpoint_type,
        recovery_state=state.recovery_state,
        revision=state.revision,
        sender_debited=state.sender_debited,
        recipient_credited=state.recipient_credited,
        funds_location=state.funds_location,
        failed_leg=state.failed_leg,
        last_payout_outcome=state.last_payout_outcome,
        failure_summary=summarise_failure(last_failed.failure) if last_failed else None,
        safe_to_restart_from_origin=state.safe_to_restart_from_origin,
        manual_review_required=state.manual_review_required,
        legs=tuple(
            ModelLegView(rail_id=leg.rail_id, status=leg.status.value, is_recovery=leg.is_recovery)
            for leg in state.legs
        ),
    )


def build_model_route_views(evaluations: Iterable[RouteEvaluation]) -> tuple[ModelRouteView, ...]:
    return tuple(
        ModelRouteView(
            route_id=e.route_id,
            rail_id=e.rail_id,
            hard_constraint_status=e.hard_constraint_status,
            rejection_reasons=e.rejection_reasons,
            incremental_fee_gbp=f"{e.estimated_incremental_cost.amount:.2f}",
            expected_latency_seconds=e.expected_latency_seconds,
            quoted_reliability=e.quoted_reliability,
            simulated_success_probability=e.simulated_success_probability,
            simulated_within_sla_probability=e.simulated_within_sla_probability,
            p95_latency_seconds=e.p95_latency_seconds,
            score=e.score.total if e.score else None,
            rank=e.rank,
        )
        for e in evaluations
    )


# ================================================================ recovery decision context


def _money_text(money: Money) -> str:
    return f"{money.amount:.2f} {money.currency.value}"


def _fee_text(money: Money) -> str:
    return f"{money.amount:.2f}"


def _prob(value: float | None) -> float:
    return round(value or 0.0, 4)


def selected_evaluation(plan: RecoveryPlan) -> RouteEvaluation:
    return next(e for e in plan.evaluations if e.route_id == plan.route_id)


def applicable_reason_codes(
    state: TransactionState, plan: RecoveryPlan
) -> tuple[AdviceReasonCode, ...]:
    """Every reason code that is TRUE for this plan, derived by code (the model may cite)."""
    selected = selected_evaluation(plan)
    passing = [e for e in plan.evaluations if e.passed]
    checks = {
        AdviceReasonCode.SENDER_ALREADY_DEBITED: state.sender_debited,
        AdviceReasonCode.FUNDS_HELD_MID_ROUTE: state.funds_location
        not in (FundsLocation.SENDER_ACCOUNT, FundsLocation.RECIPIENT_ENDPOINT),
        AdviceReasonCode.ORIGINAL_PAYOUT_DEFINITIVELY_FAILED: state.last_payout_outcome
        is AttemptOutcome.DEFINITIVE_FAILED,
        # RecoveryPlan's own validator guarantees it moves exactly the outstanding obligation.
        AdviceReasonCode.ONLY_OUTSTANDING_LEG_EXECUTED: True,
        AdviceReasonCode.POLICY_PERMITTED: plan.eligibility.policy_permitted,
        AdviceReasonCode.RECIPIENT_COMPATIBLE: plan.eligibility.recipient_compatible,
        AdviceReasonCode.LIQUIDITY_SUFFICIENT: plan.eligibility.liquidity_sufficient,
        AdviceReasonCode.RAIL_AVAILABLE: plan.eligibility.rail_status is RailStatus.UP,
        AdviceReasonCode.LOWEST_INCREMENTAL_COST: plan.incremental_fee.amount
        == min(e.estimated_incremental_cost.amount for e in passing),
        AdviceReasonCode.HIGHEST_SIMULATED_SCORE: selected.rank == 1,
        AdviceReasonCode.STRESS_TESTED: len(selected.scenario_results) > 1,
        AdviceReasonCode.FEE_ABSORBED_BY_OPERATOR: plan.fee_bearer is FeeBearer.OPERATOR,
        AdviceReasonCode.HUMAN_APPROVAL_REQUIRED: True,
    }
    return tuple(code for code, holds in checks.items() if holds)


def _decision_route(e: RouteEvaluation) -> DecisionRouteView:
    assert e.score is not None and e.rank is not None  # passing routes are scored and ranked
    p95 = e.p95_latency_seconds
    return DecisionRouteView(
        rail_id=e.rail_id,
        rank=e.rank,
        score=round(e.score.total, 3),
        incremental_fee_gbp=_fee_text(e.estimated_incremental_cost),
        quoted_arrival_seconds=e.expected_latency_seconds,
        quoted_reliability=e.quoted_reliability,
        simulated_reliability=_prob(e.simulated_success_probability),
        simulated_within_sla=_prob(e.simulated_within_sla_probability),
        simulated_p95_arrival_seconds=round(p95, 1) if p95 is not None else None,
        stress=tuple(
            StressResultView(
                scenario=r.scenario.scenario_id,
                reliability=_prob(r.simulated_success_probability),
                within_sla=_prob(r.recovery_within_sla_probability),
            )
            for r in e.scenario_results[1:]
        ),
    )


def build_decision_context(
    aggregate: TransactionAggregate, plan: RecoveryPlan
) -> RecoveryDecisionContext:
    """The compact, evidence-preserving recovery decision packet for one immutable plan."""
    state = derive_state(aggregate)
    journal = aggregate.journal
    failed = next(
        (
            a
            for a in reversed(journal.attempts())
            if a.outcome is not AttemptOutcome.SUCCEEDED and a.rail_id == state.failed_leg
        ),
        None,
    )
    selected = selected_evaluation(plan)
    compute = plan.compute
    obligation = plan.obligation
    return RecoveryDecisionContext(
        transaction_id=plan.transaction_id,
        revision=state.revision,
        funds_location=state.funds_location,
        sender_debited=state.sender_debited,
        safe_to_restart_from_origin=state.safe_to_restart_from_origin,
        completed_effects=tuple(
            CompletedEffectView(operation=e.operation, rail_id=e.rail_id, destination=e.destination)
            for e in journal.effects()
        ),
        outstanding_obligation=ObligationView(
            operation=obligation.operation,
            amount=_money_text(obligation.amount),
            source=obligation.source,
            destination=obligation.destination,
            recipient_token=obligation.recipient_token,
        ),
        failure=(
            FailureView(
                rail_id=failed.rail_id,
                outcome=failed.outcome,
                summary=summarise_failure(failed.failure),
                value_moved=False if failed.outcome is AttemptOutcome.DEFINITIVE_FAILED else None,
            )
            if failed is not None
            else None
        ),
        candidate_routes=tuple(_decision_route(e) for e in plan.evaluations if e.passed),
        rejected_routes=tuple(
            RejectedRouteView(
                rail_id=e.rail_id, reasons=e.rejection_reasons, details=e.rejection_details
            )
            for e in plan.evaluations
            if not e.passed
        ),
        compute=ComputeView(
            backend=compute.backend if compute else "unknown",
            fallback_from=compute.fallback_from if compute else None,
            fallback_reason=compute.fallback_reason if compute else None,
            parallel_jobs=compute.parallel_jobs if compute else 0,
            simulated_outcomes=compute.simulated_trials if compute else 0,
            scenarios=compute.scenarios if compute else (),
            sla_seconds=selected.scenario_results[0].sla_seconds,
        ),
        selected_plan=SelectedPlanView(
            plan_id=plan.plan_id,
            plan_hash_prefix=plan.plan_hash[:12],
            bound_revision=plan.expected_revision,
            status=aggregate.plan_status[plan.plan_id],
            rail_id=plan.rail_id,
            amount=_money_text(plan.amount),
            incremental_fee_gbp=_fee_text(plan.incremental_fee),
            fee_bearer=plan.fee_bearer,
            quoted_arrival_seconds=plan.expected_latency_seconds,
            simulated_reliability=_prob(selected.simulated_success_probability),
        ),
        reason_codes=applicable_reason_codes(state, plan),
        invariants=DECISION_INVARIANTS,
    )


def build_verbose_decision_context(
    aggregate: TransactionAggregate, plan: RecoveryPlan
) -> dict[str, Any]:
    """UNOPTIMISED baseline for A/B measurement only: the same facts, dumped naively.

    Still allowlisted (no recipient PII, no raw provider messages) but full of redundancy:
    whole journal records, the full plan with every evaluation and scenario parameter, and
    the complete audit timeline. Never used by the production agent path.
    """
    journal = aggregate.journal
    state = derive_state(aggregate)
    return {
        "transaction": build_model_transaction_view(aggregate).model_dump(mode="json"),
        "journal_attempts": [
            a.model_dump(mode="json", exclude={"failure"})
            | {"failure_summary": summarise_failure(a.failure)}
            for a in journal.attempts()
        ],
        "journal_effects": [e.model_dump(mode="json") for e in journal.effects()],
        "plan": plan.model_dump(mode="json"),
        "plan_status": aggregate.plan_status[plan.plan_id].value,
        "audit_timeline": [a.model_dump(mode="json") for a in aggregate.audit],
        "reason_codes": [c.value for c in applicable_reason_codes(state, plan)],
        "invariants": list(DECISION_INVARIANTS),
    }


def build_model_plan_view(plan: RecoveryPlan, status: PlanStatus) -> ModelPlanView:
    eligibility = plan.eligibility
    return ModelPlanView(
        plan_id=plan.plan_id,
        plan_hash_prefix=plan.plan_hash[:12],
        transaction_id=plan.transaction_id,
        bound_revision=plan.expected_revision,
        status=status,
        source=plan.source,
        rail_id=plan.rail_id,
        amount=_money_text(plan.amount),
        incremental_fee_gbp=_fee_text(plan.incremental_fee),
        fee_bearer=plan.fee_bearer,
        quoted_arrival_seconds=plan.expected_latency_seconds,
        rail_status=eligibility.rail_status,
        policy_permitted=eligibility.policy_permitted,
        policy_version=eligibility.policy_version,
        liquidity_sufficient=eligibility.liquidity_sufficient,
        recipient_compatible=eligibility.recipient_compatible,
        selection_reasons=plan.selection_reasons,
        supersedes_plan_id=plan.supersedes_plan_id,
    )


def build_model_audit_summary(
    aggregate: TransactionAggregate, limit: int = 40
) -> ModelAuditSummary:
    events = aggregate.audit
    shown = events[-limit:]
    return ModelAuditSummary(
        transaction_id=aggregate.transaction_id,
        total_events=len(events),
        entries=tuple(
            ModelAuditEntryView(
                sequence=e.sequence,
                actor=e.actor,
                event_type=e.event_type,
                state=e.recovery_state,
                summary=e.summary,
            )
            for e in shown
        ),
        truncated=len(shown) < len(events),
    )


# ================================================================ PII gates


def recipient_pii_values(instruction: PaymentTransaction) -> tuple[str, ...]:
    r = instruction.recipient
    return (
        r.name.get_secret_value(),
        r.phone.get_secret_value(),
        r.account_reference.get_secret_value(),
    )


def redacted_recipient_preview(instruction: PaymentTransaction) -> dict[str, dict[str, str]]:
    """For the UI's 'raw vs model boundary' demo panel. The raw side never goes to a model."""
    raw_name, raw_phone, raw_ref = recipient_pii_values(instruction)
    return {
        "raw": {
            "recipient_name": raw_name,
            "recipient_phone": raw_phone,
            "recipient_reference": raw_ref,
        },
        "model_boundary": {
            "recipient_name": REDACTED,
            "recipient_phone": REDACTED,
            "recipient_reference": REDACTED,
            "recipient_token": instruction.recipient_token,
        },
    }


def assert_no_recipient_pii(payload: str, instruction: PaymentTransaction) -> None:
    """Defence in depth: refuse to release a payload containing any raw PII value."""
    lowered = payload.lower()
    for value in recipient_pii_values(instruction):
        variants = {value.lower(), value.replace(" ", "").lower()}
        if any(v and v in lowered for v in variants):
            raise PIILeakError("recipient PII detected in model-bound payload")


def release_to_model[T: DomainModel | dict[str, Any]](
    payload: T, instruction: PaymentTransaction
) -> T:
    """The single gate for data crossing to a model: returns it unchanged iff PII-free."""
    if isinstance(payload, DomainModel):
        serialised = payload.model_dump_json()
    else:
        serialised = json.dumps(payload, default=str)
    assert_no_recipient_pii(serialised, instruction)
    return payload
