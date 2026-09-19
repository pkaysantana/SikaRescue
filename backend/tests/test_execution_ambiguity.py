"""Cancelled / timed-out payouts are UNKNOWN -> MANUAL_REVIEW and never retried.

DEFINITIVE_FAILED (provably no value moved) -> a new plan may be considered.
UNKNOWN (value may have moved)              -> unsafe to retry automatically.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from sikarescue.demo_data.sk10421 import PAYOUT_AMOUNT, build_demo_world
from sikarescue.errors import IllegalTransitionError, JournalIntegrityError
from sikarescue.models import (
    AttemptOutcome,
    ExecutionStatus,
    FailureStage,
    FinancialEffect,
    FundsLocation,
    OperationType,
    PlanStatus,
    RailId,
    RecoveryState,
    effect_key,
    payout_execution_key,
)

from helpers import plan_and_approve

SLOW_PROVIDER_SECONDS = 5.0


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.001)


async def _cancelled_mid_payout():
    world = build_demo_world(payout_latency_seconds=SLOW_PROVIDER_SECONDS)
    plan = await plan_and_approve(world)
    task = asyncio.create_task(world.service.execute_recovery(plan.plan_id))
    await _wait_until(lambda: world.gateway.requests)  # the request has been dispatched
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return world, plan, "CANCELLED_AFTER_DISPATCH"


async def _timed_out_after_dispatch():
    world = build_demo_world(
        payout_latency_seconds=SLOW_PROVIDER_SECONDS, payout_timeout_seconds=0.05
    )
    plan = await plan_and_approve(world)
    result = await world.service.execute_recovery(plan.plan_id)
    assert result.status is ExecutionStatus.OUTCOME_UNKNOWN
    assert "GATEWAY_TIMEOUT" in result.detail
    return world, plan, "GATEWAY_TIMEOUT"


SCENARIOS = {"cancelled": _cancelled_mid_payout, "timed_out": _timed_out_after_dispatch}


@pytest.fixture(params=list(SCENARIOS))
async def ambiguous(request):
    return await SCENARIOS[request.param]()


async def test_ambiguous_payout_is_recorded_as_unknown(ambiguous, txn_id):
    world, plan, provider_code = ambiguous
    aggregate = world.repository.get(txn_id)
    execution = aggregate.executions[payout_execution_key(txn_id, plan.plan_id)]
    assert execution.status is ExecutionStatus.OUTCOME_UNKNOWN  # never left IN_PROGRESS
    assert execution.finished_at is not None
    [attempt] = [a for a in aggregate.journal.attempts() if a.plan_id == plan.plan_id]
    assert attempt.outcome is AttemptOutcome.UNKNOWN  # never DEFINITIVE_FAILED
    assert attempt.failure.stage is FailureStage.UNDETERMINED
    assert attempt.failure.provider_code == provider_code
    assert world.service.get_plan_status(plan.plan_id) is PlanStatus.OUTCOME_UNKNOWN
    assert aggregate.state is RecoveryState.MANUAL_REVIEW
    assert not aggregate.has_execution_in_progress()


async def test_retry_after_ambiguous_outcome_cannot_start_a_new_payout(ambiguous, txn_id):
    world, plan, _ = ambiguous
    submitted = len(world.gateway.requests)
    original = world.repository.get(txn_id).executions[payout_execution_key(txn_id, plan.plan_id)]

    again = await world.service.execute_recovery(plan.plan_id)
    assert again.replayed
    assert again.execution_id == original.execution_id
    assert again.status is ExecutionStatus.OUTCOME_UNKNOWN
    with pytest.raises(IllegalTransitionError):  # no autonomous rerouting either
        await world.service.create_recovery_plan(txn_id)
    assert len(world.gateway.requests) == submitted == 1
    assert world.gateway.submissions_for(RailId.BANK_MOMO_BRIDGE) == 0


async def test_unknown_outcome_never_creates_a_recipient_credit(ambiguous, txn_id):
    world, plan, _ = ambiguous
    aggregate = world.repository.get(txn_id)
    journal = aggregate.journal
    assert not journal.has_effect(OperationType.RECIPIENT_CREDIT)
    assert journal.funds_location() is FundsLocation.GH_SETTLEMENT_ACCOUNT
    assert world.gateway.value_movements == []

    # Only future, explicit reconciliation tooling may resolve it: the journal refuses a
    # credit backed by the UNKNOWN attempt.
    [attempt] = [a for a in journal.attempts() if a.plan_id == plan.plan_id]
    source, destination = FundsLocation.GH_SETTLEMENT_ACCOUNT, FundsLocation.RECIPIENT_ENDPOINT
    with pytest.raises(JournalIntegrityError, match="SUCCEEDED attempt"):
        journal.post_effect(
            FinancialEffect(
                effect_key=effect_key(txn_id, OperationType.RECIPIENT_CREDIT),
                transaction_id=txn_id,
                operation=OperationType.RECIPIENT_CREDIT,
                rail_id=plan.rail_id,
                attempt_id=attempt.attempt_id,
                source=source,
                destination=destination,
                source_amount=PAYOUT_AMOUNT,
                destination_amount=PAYOUT_AMOUNT,
                posted_at=datetime(2026, 9, 19, 12, 0, tzinfo=UTC),
            )
        )
    reconciliation = await world.service.reconcile_transaction(txn_id)
    assert not reconciliation.reconciled


async def test_definitive_failure_contrast_allows_a_new_plan(world, txn_id):
    world.gateway.script(RailId.MOMO_B, AttemptOutcome.DEFINITIVE_FAILED)
    plan = await plan_and_approve(world)
    result = await world.service.execute_recovery(plan.plan_id)
    assert result.status is ExecutionStatus.FAILED
    assert world.repository.get(txn_id).state is RecoveryState.RECOVERY_FAILED
    replacement = await world.service.create_recovery_plan(txn_id)  # safe: nothing moved
    assert replacement.plan_id != plan.plan_id
