"""Derive transaction state from journal evidence, and check recovery preconditions."""

from __future__ import annotations

from sikarescue.models import (
    AttemptOutcome,
    ExecutionStatus,
    FailureDetail,
    FundsCertainty,
    FundsLocation,
    FundsPosition,
    OperationType,
    OutstandingObligation,
    PositionStatus,
    RecoveryPlan,
    SettlementLeg,
    SettlementLegStatus,
    TransactionState,
    effect_key,
)
from sikarescue.models.enums import OPERATION_FLOW
from sikarescue.services.repository import TransactionAggregate

UPSTREAM_OPERATIONS = (
    OperationType.SENDER_DEBIT,
    OperationType.FX_CONVERSION,
    OperationType.GH_SETTLEMENT,
)

_LEG_STATUS = {
    AttemptOutcome.SUCCEEDED: SettlementLegStatus.SUCCESS,
    AttemptOutcome.DEFINITIVE_FAILED: SettlementLegStatus.FAILED,
    AttemptOutcome.UNKNOWN: SettlementLegStatus.UNKNOWN,
}


def summarise_failure(failure: FailureDetail | None) -> str | None:
    """Sanitised one-liner: status + stage + provider code. Never the raw provider message."""
    if failure is None:
        return None
    status = f"HTTP {failure.http_status}" if failure.http_status else "no HTTP status"
    code = f" {failure.provider_code}" if failure.provider_code else ""
    return f"{status}{code} ({failure.stage.value.lower().replace('_', '-')})"


def unresolved_unknown_attempts(aggregate: TransactionAggregate) -> list[str]:
    """Attempts whose outcome is UNKNOWN and whose effect has not been confirmed."""
    journal = aggregate.journal
    return [
        a.attempt_id
        for a in journal.attempts()
        if a.outcome is AttemptOutcome.UNKNOWN and not journal.has_effect(a.operation)
    ]


def derive_legs(aggregate: TransactionAggregate) -> tuple[SettlementLeg, ...]:
    original = set(aggregate.instruction.original_route)
    legs = [
        SettlementLeg(
            sequence=i,
            rail_id=a.rail_id,
            operation=a.operation,
            source=a.source,
            destination=a.destination,
            status=_LEG_STATUS[a.outcome],
            attempt_id=a.attempt_id,
            is_recovery=a.plan_id is not None or a.rail_id not in original,
            failure_summary=summarise_failure(a.failure),
        )
        for i, a in enumerate(aggregate.journal.attempts(), start=1)
    ]
    for response in aggregate.journal.pending_provider_responses():
        source, destination = OPERATION_FLOW[response.operation]
        legs.append(
            SettlementLeg(
                sequence=len(legs) + 1,
                rail_id=response.rail_id,
                operation=response.operation,
                source=source,
                destination=destination,
                status=SettlementLegStatus.AWAITING_EVIDENCE,
                attempt_id=response.attempt_id,
                is_recovery=response.rail_id not in original,
                failure_summary="provider response received; not yet classified",
            )
        )
    for execution in aggregate.executions.values():
        if execution.status is ExecutionStatus.IN_PROGRESS:
            plan = aggregate.plans[execution.plan_id]
            legs.append(
                SettlementLeg(
                    sequence=len(legs) + 1,
                    rail_id=plan.rail_id,
                    operation=OperationType.RECIPIENT_CREDIT,
                    source=plan.source,
                    destination=FundsLocation.RECIPIENT_ENDPOINT,
                    status=SettlementLegStatus.IN_PROGRESS,
                    is_recovery=True,
                )
            )
    return tuple(legs)


def outstanding_obligation(aggregate: TransactionAggregate) -> OutstandingObligation | None:
    """The recipient credit still owed, if every upstream effect already succeeded.

    Recovering earlier legs (e.g. a failed FX) is out of scope for this demo corridor.
    """
    journal = aggregate.journal
    if journal.has_effect(OperationType.RECIPIENT_CREDIT):
        return None
    if not all(journal.has_effect(op) for op in UPSTREAM_OPERATIONS):
        return None
    settlement = journal.effect(OperationType.GH_SETTLEMENT)
    assert settlement is not None
    instruction = aggregate.instruction
    return OutstandingObligation(
        effect_key=effect_key(instruction.transaction_id, OperationType.RECIPIENT_CREDIT),
        amount=settlement.destination_amount,
        source=journal.funds_location(),
        recipient_token=instruction.recipient_token,
        endpoint_type=instruction.recipient.endpoint_type,
    )


IN_FLIGHT_REASON = "a payout is in flight; its outcome is not yet known"
UNCLASSIFIED_REASON = (
    "a payout response is not yet classified; the recipient may already have been credited"
)
UNKNOWN_REASON = "a payout outcome is UNKNOWN; the recipient may already have been credited"


def derive_funds_position(aggregate: TransactionAggregate) -> FundsPosition:
    """The one derivation of funds certainty. Never stored, never edited."""
    journal = aggregate.journal
    effects = journal.effects()
    if journal.has_effect(OperationType.RECIPIENT_CREDIT):
        status, reason = PositionStatus.FINAL, None
    elif aggregate.has_execution_in_progress():
        status, reason = PositionStatus.IN_FLIGHT, IN_FLIGHT_REASON
    elif journal.pending_provider_responses():
        status, reason = PositionStatus.IN_FLIGHT, UNCLASSIFIED_REASON
    elif unresolved_unknown_attempts(aggregate):
        status, reason = PositionStatus.UNCERTAIN, UNKNOWN_REASON
    else:
        status, reason = PositionStatus.AVAILABLE, None
    proven = status in (PositionStatus.AVAILABLE, PositionStatus.FINAL)
    return FundsPosition(
        transaction_id=aggregate.transaction_id,
        amount=effects[-1].destination_amount if effects else aggregate.instruction.send_amount,
        last_confirmed_location=journal.funds_location(),
        position_status=status,
        certainty=FundsCertainty.PROVEN if proven else FundsCertainty.UNCERTAIN,
        available_for_automatic_action=status is PositionStatus.AVAILABLE
        and outstanding_obligation(aggregate) is not None,
        derived_from_effect_ids=tuple(e.effect_key for e in effects),
        reason=reason,
    )


def derive_state(aggregate: TransactionAggregate) -> TransactionState:
    journal = aggregate.journal
    payout_attempts = [
        a for a in journal.attempts() if a.operation is OperationType.RECIPIENT_CREDIT
    ]
    last_payout = payout_attempts[-1] if payout_attempts else None
    recipient_credited = journal.has_effect(OperationType.RECIPIENT_CREDIT)
    failed_leg = (
        last_payout.rail_id
        if last_payout is not None
        and last_payout.outcome is not AttemptOutcome.SUCCEEDED
        and not recipient_credited
        else None
    )
    obligation = outstanding_obligation(aggregate)
    unknown = unresolved_unknown_attempts(aggregate)
    position = derive_funds_position(aggregate)
    return TransactionState(
        transaction_id=aggregate.transaction_id,
        revision=aggregate.revision,
        recovery_state=aggregate.state,
        sender_debited=journal.has_effect(OperationType.SENDER_DEBIT),
        fx_completed=journal.has_effect(OperationType.FX_CONVERSION),
        settlement_completed=journal.has_effect(OperationType.GH_SETTLEMENT),
        recipient_credited=recipient_credited,
        sender_debit_count=journal.count_effects(OperationType.SENDER_DEBIT),
        recipient_credit_count=journal.count_effects(OperationType.RECIPIENT_CREDIT),
        funds_location=position.last_confirmed_location,
        funds_certainty=position.certainty,
        available_for_automatic_action=position.available_for_automatic_action,
        uncertainty_reason=position.reason,
        funds_position=position,
        evidence_pending=bool(journal.pending_provider_responses()),
        failed_leg=failed_leg,
        last_payout_outcome=last_payout.outcome if last_payout else None,
        # Once any value has moved, restarting from the origin would double-charge.
        safe_to_restart_from_origin=not journal.effects(),
        manual_review_required=bool(unknown),
        outstanding_obligation=obligation,
        completed_effect_keys=tuple(e.effect_key for e in journal.effects()),
        legs=derive_legs(aggregate),
    )


def check_recovery_preconditions(
    aggregate: TransactionAggregate, plan: RecoveryPlan | None = None
) -> list[str]:
    """Deterministic facts that must hold at plan creation AND at execution admission.

    Returns human-readable violations; empty list means recovery may proceed.
    """
    journal = aggregate.journal
    violations: list[str] = []
    location = journal.funds_location()
    if location is not FundsLocation.GH_SETTLEMENT_ACCOUNT:
        violations.append(f"funds are at {location}, not GH_SETTLEMENT_ACCOUNT")
    for op in UPSTREAM_OPERATIONS:
        if not journal.has_effect(op):
            violations.append(f"upstream effect {op} has not succeeded")
    if journal.has_effect(OperationType.RECIPIENT_CREDIT):
        violations.append("recipient has already been credited")
    unknown = unresolved_unknown_attempts(aggregate)
    if unknown:
        violations.append(f"attempt(s) with UNKNOWN outcome need reconciliation: {unknown}")
    for response in journal.pending_provider_responses():
        violations.append(
            f"{response.rail_id} response for attempt {response.attempt_id} is not yet classified"
        )
    if plan is not None:
        if plan.transaction_id != aggregate.transaction_id:
            violations.append("plan belongs to another transaction")
        if plan.obligation != outstanding_obligation(aggregate):
            violations.append("plan does not target the currently outstanding obligation")
        if plan.source is not location:
            violations.append(f"plan sources funds from {plan.source}, funds are at {location}")
    return violations
