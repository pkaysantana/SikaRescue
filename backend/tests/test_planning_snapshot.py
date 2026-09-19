"""Route evaluation runs outside the transaction lock; stale results are never persisted."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from sikarescue.demo_data.sk10421 import PAYOUT_AMOUNT
from sikarescue.errors import StalePlanError, StalePlanningResultError
from sikarescue.models import (
    AttemptOutcome,
    AuditEventType,
    FinancialEffect,
    OperationAttempt,
    OperationType,
    ProviderCallbackRecorded,
    RailId,
    RailStatus,
    RecoveryState,
    effect_key,
)
from sikarescue.models.enums import OPERATION_FLOW

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


@asynccontextmanager
async def _locked_like_a_concurrent_command(aggregate, timeout: float = 1.0):
    """Take the lock as another command would; fail fast (not hang) if planning holds it."""
    await asyncio.wait_for(aggregate.lock.acquire(), timeout)
    try:
        yield
    finally:
        aggregate.lock.release()


def _during_evaluation(world, monkeypatch, action):
    """Run `action(aggregate)` while route evaluation is in flight."""
    original = world.service._evaluate_snapshot

    async def hooked(snapshot):
        result = await original(snapshot)
        await action(world.repository.get(snapshot.transaction_id))
        return result

    monkeypatch.setattr(world.service, "_evaluate_snapshot", hooked)


async def _late_callback(aggregate):
    async with _locked_like_a_concurrent_command(aggregate):  # new financial evidence
        aggregate.journal.record(
            ProviderCallbackRecorded(
                rail_id=RailId.MOMO_A,
                attempt_id="att_000000000004",
                note="Late synthetic callback.",
            )
        )


def _assert_nothing_persisted(aggregate):
    assert aggregate.plans == {}
    assert aggregate.approval_requests == {}
    assert aggregate.current_plan_id is None
    assert aggregate.state is not RecoveryState.AWAITING_APPROVAL
    assert aggregate.audit[-1].event_type is AuditEventType.PLANNING_RESULT_DISCARDED


async def test_route_evaluation_runs_without_the_transaction_lock(world, txn_id, monkeypatch):
    aggregate = world.repository.get(txn_id)
    original = world.service._evaluate_snapshot
    lock_held_during_evaluation = []

    async def spy(snapshot):
        lock_held_during_evaluation.append(aggregate.lock.locked())
        # Another command could take the lock right now (would time out if it were held).
        await asyncio.wait_for(aggregate.lock.acquire(), timeout=1)
        aggregate.lock.release()
        return await original(snapshot)

    monkeypatch.setattr(world.service, "_evaluate_snapshot", spy)
    plan = await world.service.create_recovery_plan(txn_id)
    assert lock_held_during_evaluation == [False]
    assert plan.rail_id is RailId.MOMO_B


async def test_replacement_plan_is_also_evaluated_outside_the_lock(world, txn_id, monkeypatch):
    aggregate = world.repository.get(txn_id)
    plan = await world.service.create_recovery_plan(txn_id)
    await _late_callback(aggregate)
    original = world.service._evaluate_snapshot
    lock_held = []

    async def spy(snapshot):
        lock_held.append(aggregate.lock.locked())
        return await original(snapshot)

    monkeypatch.setattr(world.service, "_evaluate_snapshot", spy)
    with pytest.raises(StalePlanError) as exc:
        await world.service.approve_recovery(
            plan.plan_id, plan_hash=plan.plan_hash, approver="ops.demo"
        )
    assert lock_held == [False]
    assert exc.value.replacement_plan_id is not None


async def test_revision_change_during_evaluation_discards_the_result(world, txn_id, monkeypatch):
    aggregate = world.repository.get(txn_id)
    _during_evaluation(world, monkeypatch, _late_callback)
    with pytest.raises(StalePlanningResultError, match="revision changed"):
        await world.service.create_recovery_plan(txn_id)
    _assert_nothing_persisted(aggregate)
    assert aggregate.state is RecoveryState.DIAGNOSING

    monkeypatch.undo()  # a retry plans against the new revision
    plan = await world.service.create_recovery_plan(txn_id)
    assert plan.expected_revision == aggregate.revision
    assert aggregate.state is RecoveryState.AWAITING_APPROVAL


async def test_recipient_credit_during_evaluation_discards_the_result(world, txn_id, monkeypatch):
    aggregate = world.repository.get(txn_id)

    async def credit_recorded_elsewhere(agg):
        op = OperationType.RECIPIENT_CREDIT
        source, destination = OPERATION_FLOW[op]
        async with _locked_like_a_concurrent_command(agg):
            attempt = OperationAttempt(
                attempt_id="att_0000000000ff",
                transaction_id=txn_id,
                operation=op,
                rail_id=RailId.MOMO_B,
                source=source,
                destination=destination,
                amount=PAYOUT_AMOUNT,
                outcome=AttemptOutcome.SUCCEEDED,
                idempotency_key="out-of-band",
                started_at=NOW,
                completed_at=NOW,
            )
            agg.journal.record_attempt(attempt)
            agg.journal.post_effect(
                FinancialEffect(
                    effect_key=effect_key(txn_id, op),
                    transaction_id=txn_id,
                    operation=op,
                    rail_id=RailId.MOMO_B,
                    attempt_id=attempt.attempt_id,
                    source=source,
                    destination=destination,
                    source_amount=PAYOUT_AMOUNT,
                    destination_amount=PAYOUT_AMOUNT,
                    posted_at=NOW,
                )
            )

    _during_evaluation(world, monkeypatch, credit_recorded_elsewhere)
    with pytest.raises(StalePlanningResultError) as exc:
        await world.service.create_recovery_plan(txn_id)
    assert "a recipient credit was recorded" in exc.value.reasons
    assert any(r.startswith("funds moved") for r in exc.value.reasons)
    _assert_nothing_persisted(aggregate)
    assert aggregate.journal.count_effects(OperationType.RECIPIENT_CREDIT) == 1


async def test_selected_rail_change_during_evaluation_discards_the_result(
    world, txn_id, monkeypatch
):
    aggregate = world.repository.get(txn_id)

    async def momo_b_goes_down(_agg):
        world.registry.set_status(RailId.MOMO_B, RailStatus.DOWN)

    _during_evaluation(world, monkeypatch, momo_b_goes_down)
    with pytest.raises(StalePlanningResultError, match="MOMO_B eligibility or quote changed"):
        await world.service.create_recovery_plan(txn_id)
    _assert_nothing_persisted(aggregate)

    monkeypatch.undo()
    plan = await world.service.create_recovery_plan(txn_id)
    assert plan.rail_id is RailId.BANK_MOMO_BRIDGE


async def test_concurrent_planning_persists_exactly_one_plan(world, txn_id, monkeypatch):
    original = world.service._evaluate_snapshot

    async def slow(snapshot):
        await asyncio.sleep(0.01)
        return await original(snapshot)

    monkeypatch.setattr(world.service, "_evaluate_snapshot", slow)
    first, second = await asyncio.gather(
        world.service.create_recovery_plan(txn_id),
        world.service.create_recovery_plan(txn_id),
    )
    assert first.plan_id == second.plan_id
    assert len(world.repository.get(txn_id).plans) == 1
