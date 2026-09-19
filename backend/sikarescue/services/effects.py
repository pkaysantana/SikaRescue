"""The ONE definition of what executing a recovery plan records.

Execution (`RecoveryService._finalise`) and the ledger preview both build the payout attempt
and the recipient-credit effect here, so the preview is not an approximation: it is the
exact records execution would append, posted into a throwaway fork of the journal.
"""

from __future__ import annotations

from datetime import datetime

from sikarescue.models import (
    AttemptOutcome,
    FailureDetail,
    FinancialEffect,
    FundsLocation,
    OperationAttempt,
    OperationType,
    RecoveryPlan,
    effect_key,
)


def payout_attempt(
    plan: RecoveryPlan,
    *,
    attempt_id: str,
    execution_key: str,
    outcome: AttemptOutcome,
    provider_reference: str | None,
    failure: FailureDetail | None,
    started_at: datetime,
    completed_at: datetime,
) -> OperationAttempt:
    return OperationAttempt(
        attempt_id=attempt_id,
        transaction_id=plan.transaction_id,
        operation=OperationType.RECIPIENT_CREDIT,
        rail_id=plan.rail_id,
        source=plan.source,
        destination=FundsLocation.RECIPIENT_ENDPOINT,
        amount=plan.amount,
        outcome=outcome,
        idempotency_key=execution_key,
        plan_id=plan.plan_id,
        provider_reference=provider_reference,
        failure=failure,
        started_at=started_at,
        completed_at=completed_at,
    )


def recipient_credit_effect(
    plan: RecoveryPlan, *, attempt_id: str, posted_at: datetime
) -> FinancialEffect:
    return FinancialEffect(
        effect_key=effect_key(plan.transaction_id, OperationType.RECIPIENT_CREDIT),
        transaction_id=plan.transaction_id,
        operation=OperationType.RECIPIENT_CREDIT,
        rail_id=plan.rail_id,
        attempt_id=attempt_id,
        source=plan.source,
        destination=FundsLocation.RECIPIENT_ENDPOINT,
        source_amount=plan.amount,
        destination_amount=plan.amount,
        posted_at=posted_at,
    )
