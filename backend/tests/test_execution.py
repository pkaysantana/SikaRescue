"""Execution: approval-gated, exactly-once, atomic admission, no silent rerouting."""

from __future__ import annotations

import asyncio

import pytest

from sikarescue.demo_data.sk10421 import PAYOUT_AMOUNT, build_demo_world
from sikarescue.errors import (
    ApprovalRequiredError,
    IllegalTransitionError,
    ManualReviewRequiredError,
    StalePlanError,
)
from sikarescue.models import (
    AttemptOutcome,
    ExecutionStatus,
    FundsLocation,
    OperationType,
    PlanStatus,
    RailId,
    RailStatus,
    RailType,
    RecoveryState,
    RejectionReason,
)

from helpers import plan_and_approve


async def test_execution_blocked_before_approval(world, txn_id):  # 7
    plan = await world.service.create_recovery_plan(txn_id)
    with pytest.raises(ApprovalRequiredError):
        await world.service.execute_recovery(plan.plan_id)
    assert world.gateway.requests == []
    aggregate = world.repository.get(txn_id)
    assert aggregate.executions == {}  # refused admission writes nothing
    assert aggregate.revision == plan.expected_revision  # no journal entry either
    assert aggregate.state is RecoveryState.AWAITING_APPROVAL


async def test_successful_recovery_credits_recipient_exactly_once(world, txn_id):  # 10, O
    before = world.liquidity.balance(RailId.MOMO_B).amount
    plan = await plan_and_approve(world)
    result = await world.service.execute_recovery(plan.plan_id)

    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.recipient_credit_effect_key == "SK-10421:recipient_credit"
    state = world.service.get_transaction_state(txn_id)
    assert state.recovery_state is RecoveryState.RECOVERED
    assert state.recipient_credited and state.recipient_credit_count == 1
    assert state.sender_debit_count == 1  # sender NOT debited again
    assert state.funds_location is FundsLocation.RECIPIENT_ENDPOINT
    assert len(world.gateway.value_movements) == 1
    assert world.liquidity.balance(RailId.MOMO_B).amount == before - PAYOUT_AMOUNT.amount
    assert world.service.get_plan_status(plan.plan_id) is PlanStatus.EXECUTED


async def test_same_plan_execution_is_idempotent(world, txn_id):  # E
    plan = await plan_and_approve(world)
    first = await world.service.execute_recovery(plan.plan_id)
    second = await world.service.execute_recovery(plan.plan_id)
    assert (first.replayed, second.replayed) == (False, True)
    assert second.execution_id == first.execution_id
    assert second.execution_key == f"{txn_id}:plan:{plan.plan_id}:payout"
    assert len(world.gateway.requests) == 1
    journal = world.repository.get(txn_id).journal
    assert journal.count_effects(OperationType.RECIPIENT_CREDIT) == 1


async def test_concurrent_double_execution_admits_only_one(txn_id):  # L
    world = build_demo_world(payout_latency_seconds=0.05)
    plan = await plan_and_approve(world)
    r1, r2 = await asyncio.gather(
        world.service.execute_recovery(plan.plan_id),
        world.service.execute_recovery(plan.plan_id),
    )
    assert r1.execution_id == r2.execution_id
    assert sorted([r1.replayed, r2.replayed]) == [False, True]
    assert len(world.gateway.requests) == 1
    assert len(world.gateway.value_movements) == 1
    state = world.service.get_transaction_state(txn_id)
    assert state.recipient_credit_count == 1
    assert state.recovery_state is RecoveryState.RECOVERED


async def test_different_recovery_attempt_requires_new_plan(world, txn_id):  # F
    world.gateway.script(RailId.MOMO_B, AttemptOutcome.DEFINITIVE_FAILED)
    plan_a = await plan_and_approve(world)
    failed = await world.service.execute_recovery(plan_a.plan_id)
    assert failed.status is ExecutionStatus.FAILED
    state = world.service.get_transaction_state(txn_id)
    assert state.recovery_state is RecoveryState.RECOVERY_FAILED
    assert state.funds_location is FundsLocation.GH_SETTLEMENT_ACCOUNT
    assert not state.recipient_credited

    # Re-executing the failed plan does not submit another payout.
    again = await world.service.execute_recovery(plan_a.plan_id)
    assert again.replayed and again.status is ExecutionStatus.FAILED
    assert world.gateway.submissions_for(RailId.MOMO_B) == 1

    plan_b = await world.service.create_recovery_plan(txn_id)
    assert plan_b.plan_id != plan_a.plan_id
    assert plan_b.supersedes_plan_id == plan_a.plan_id
    assert plan_b.rail_id is RailId.BANK_MOMO_BRIDGE
    momo_b = next(e for e in plan_b.evaluations if e.rail_id is RailId.MOMO_B)
    assert RejectionReason.FAILED_EARLIER_FOR_TRANSACTION in momo_b.rejection_reasons
    with pytest.raises(ApprovalRequiredError):  # the new plan needs its own approval
        await world.service.execute_recovery(plan_b.plan_id)

    await world.service.approve_recovery(
        plan_b.plan_id, plan_hash=plan_b.plan_hash, approver="ops.demo"
    )
    result = await world.service.execute_recovery(plan_b.plan_id)
    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.execution_key != failed.execution_key
    state = world.service.get_transaction_state(txn_id)
    assert (state.sender_debit_count, state.recipient_credit_count) == (1, 1)


async def test_unknown_payout_outcome_enters_manual_review(world, txn_id):  # J
    world.gateway.script(RailId.MOMO_B, AttemptOutcome.UNKNOWN)
    plan = await plan_and_approve(world)
    result = await world.service.execute_recovery(plan.plan_id)
    assert result.status is ExecutionStatus.OUTCOME_UNKNOWN
    state = world.service.get_transaction_state(txn_id)
    assert state.recovery_state is RecoveryState.MANUAL_REVIEW
    assert state.manual_review_required and not state.recipient_credited
    with pytest.raises(IllegalTransitionError):  # no automatic routing around it
        await world.service.create_recovery_plan(txn_id)
    assert world.gateway.submissions_for(RailId.BANK_MOMO_BRIDGE) == 0


async def test_seeded_unknown_momo_a_cannot_auto_recover(txn_id):  # J
    world = build_demo_world(momo_a_outcome=AttemptOutcome.UNKNOWN)
    with pytest.raises(ManualReviewRequiredError):
        await world.service.create_recovery_plan(txn_id)
    aggregate = world.repository.get(txn_id)
    assert aggregate.state is RecoveryState.MANUAL_REVIEW
    assert aggregate.plans == {}
    assert world.gateway.requests == []


async def test_transport_error_is_treated_as_unknown(world, txn_id, monkeypatch):
    async def broken_submit(request):
        raise ConnectionError("synthetic network failure")

    plan = await plan_and_approve(world)
    monkeypatch.setattr(world.gateway, "submit", broken_submit)
    result = await world.service.execute_recovery(plan.plan_id)
    assert result.status is ExecutionStatus.OUTCOME_UNKNOWN
    assert world.repository.get(txn_id).state is RecoveryState.MANUAL_REVIEW


async def test_policy_change_at_execution_makes_plan_stale(world, txn_id):  # M
    plan = await plan_and_approve(world)
    assert plan.rail_id is RailId.MOMO_B
    world.policy.set_allowed_rail_types("GB->GH", frozenset({RailType.BANK_TO_MOMO_BRIDGE}))
    with pytest.raises(StalePlanError, match="policy_permitted changed") as exc:
        await world.service.execute_recovery(plan.plan_id)
    assert world.gateway.requests == []  # nothing executed, no silent reroute
    assert world.service.get_plan_status(plan.plan_id) is PlanStatus.STALE
    replacement = world.service.get_plan(exc.value.replacement_plan_id)
    assert replacement.rail_id is RailId.BANK_MOMO_BRIDGE
    assert world.service.get_plan_status(replacement.plan_id) is PlanStatus.PENDING_APPROVAL
    assert world.repository.get(txn_id).state is RecoveryState.AWAITING_APPROVAL


async def test_rail_outage_at_execution_makes_plan_stale(world, txn_id):  # M
    plan = await plan_and_approve(world)
    world.registry.set_status(RailId.MOMO_B, RailStatus.DOWN)
    with pytest.raises(StalePlanError, match=r"rail_status changed \(UP -> DOWN\)"):
        await world.service.execute_recovery(plan.plan_id)
    assert world.gateway.requests == []
    assert world.repository.get(txn_id).state is RecoveryState.AWAITING_APPROVAL
