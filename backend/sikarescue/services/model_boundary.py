"""The model boundary: the only way transaction data may be prepared for an LLM.

Primary protection is ALLOWLISTING: views are built field-by-field from deterministic
state and never copy recipient identity. `assert_no_recipient_pii` is defence in depth
that scans the final serialised payload for any raw PII value before it leaves.
"""

from __future__ import annotations

from collections.abc import Iterable

from sikarescue.errors import PIILeakError
from sikarescue.models import (
    ModelLegView,
    ModelRouteView,
    ModelTransactionView,
    PaymentTransaction,
    RouteEvaluation,
)
from sikarescue.services.diagnosis import derive_state, summarise_failure
from sikarescue.services.repository import TransactionAggregate

REDACTED = "[REDACTED]"


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
            score=e.score.total if e.score else None,
            rank=e.rank,
        )
        for e in evaluations
    )


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
