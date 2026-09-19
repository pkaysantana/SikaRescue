"""Reconciliation succeeds only once the single outstanding obligation is satisfied (11, P)."""

from __future__ import annotations

from sikarescue.models import AttemptOutcome, FundsLocation, RailId, RecoveryState

from helpers import plan_and_approve


async def test_reconciliation_refused_before_recovery(world, txn_id):  # P
    result = await world.service.reconcile_transaction(txn_id)
    assert not result.reconciled
    failed = {c.name for c in result.checks if not c.passed}
    assert {"single_recipient_credit", "funds_at_recipient", "recovered_state"} <= failed
    assert result.final_state is RecoveryState.FAILED
    assert world.repository.get(txn_id).state is RecoveryState.FAILED


async def test_reconciliation_after_recovery_reaches_reconciled(world, txn_id):  # 11
    plan = await plan_and_approve(world)
    await world.service.execute_recovery(plan.plan_id)
    result = await world.service.reconcile_transaction(txn_id)
    assert result.reconciled
    assert all(c.passed for c in result.checks)
    assert result.final_state is RecoveryState.RECONCILED
    assert world.repository.get(txn_id).state is RecoveryState.RECONCILED
    assert (result.sender_debit_count, result.recipient_credit_count) == (1, 1)
    assert result.duplicate_sender_debits == 0
    assert result.funds_location is FundsLocation.RECIPIENT_ENDPOINT

    again = await world.service.reconcile_transaction(txn_id)
    assert again.reconciliation_id == result.reconciliation_id


async def test_reconciliation_refused_while_outcome_unknown(world, txn_id):  # P
    world.gateway.script(RailId.MOMO_B, AttemptOutcome.UNKNOWN)
    plan = await plan_and_approve(world)
    await world.service.execute_recovery(plan.plan_id)
    result = await world.service.reconcile_transaction(txn_id)
    assert not result.reconciled
    assert "no_open_or_unknown_execution" in {c.name for c in result.checks if not c.passed}
    assert world.repository.get(txn_id).state is RecoveryState.MANUAL_REVIEW
