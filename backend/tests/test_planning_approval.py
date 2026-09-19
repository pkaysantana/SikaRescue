"""Approval authorises exactly one immutable plan at one revision (9, G, H, I)."""

from __future__ import annotations

import pytest

from sikarescue.errors import ApprovalMismatchError, ApprovalRequiredError, StalePlanError
from sikarescue.models import (
    Currency,
    ExecutionStatus,
    Money,
    PlanStatus,
    ProviderCallbackRecorded,
    RailId,
    RecoveryState,
)

from helpers import plan_and_approve


def _late_provider_callback(world, txn_id):
    """New financial evidence arrives (advances the transaction revision)."""
    world.repository.get(txn_id).journal.record(
        ProviderCallbackRecorded(
            rail_id=RailId.MOMO_A,
            attempt_id="att_000000000004",
            note="Late synthetic callback confirming MOMO_A rejection.",
        )
    )


async def test_plan_creation_awaits_approval(world, txn_id):
    plan = await world.service.create_recovery_plan(txn_id)
    aggregate = world.repository.get(txn_id)
    assert aggregate.state is RecoveryState.AWAITING_APPROVAL
    assert world.service.get_plan_status(plan.plan_id) is PlanStatus.PENDING_APPROVAL
    request = await world.service.request_recovery_approval(plan.plan_id)
    assert (request.plan_hash, request.transaction_revision) == (
        plan.plan_hash,
        plan.expected_revision,
    )
    assert plan.expected_revision == aggregate.revision


async def test_create_plan_is_idempotent_while_fresh(world, txn_id):
    first = await world.service.create_recovery_plan(txn_id)
    second = await world.service.create_recovery_plan(txn_id)
    assert first.plan_id == second.plan_id
    assert len(world.repository.get(txn_id).plans) == 1


async def test_valid_approval_transitions_into_execution(world, txn_id):  # 9
    plan = await world.service.create_recovery_plan(txn_id)
    decision = await world.service.approve_recovery(
        plan.plan_id, plan_hash=plan.plan_hash, approver="ops.demo"
    )
    assert decision.approved
    assert (decision.plan_id, decision.plan_hash, decision.transaction_revision) == (
        plan.plan_id,
        plan.plan_hash,
        plan.expected_revision,
    )
    assert decision.approver == "ops.demo"
    assert world.repository.get(txn_id).state is RecoveryState.APPROVED
    result = await world.service.execute_recovery(plan.plan_id)
    assert result.status is ExecutionStatus.SUCCEEDED


async def test_repeated_identical_approval_is_idempotent(world, txn_id):
    plan = await plan_and_approve(world)
    again = await world.service.approve_recovery(
        plan.plan_id, plan_hash=plan.plan_hash, approver="ops.demo"
    )
    assert again.decision_id == world.repository.get(txn_id).approvals[plan.plan_id].decision_id


async def test_stale_revision_rejected_at_approval(world, txn_id):  # G
    plan = await world.service.create_recovery_plan(txn_id)
    _late_provider_callback(world, txn_id)
    with pytest.raises(StalePlanError) as exc:
        await world.service.approve_recovery(
            plan.plan_id, plan_hash=plan.plan_hash, approver="ops.demo"
        )
    assert any("revision changed" in r for r in exc.value.reasons)
    assert world.service.get_plan_status(plan.plan_id) is PlanStatus.STALE
    replacement = world.service.get_plan(exc.value.replacement_plan_id)
    assert replacement.plan_id != plan.plan_id
    assert replacement.supersedes_plan_id == plan.plan_id
    assert replacement.expected_revision == world.repository.get(txn_id).revision
    assert world.service.get_plan_status(replacement.plan_id) is PlanStatus.PENDING_APPROVAL
    assert world.repository.get(txn_id).state is RecoveryState.AWAITING_APPROVAL


async def test_stale_revision_rejected_at_execution(world, txn_id):  # G
    plan = await plan_and_approve(world)
    _late_provider_callback(world, txn_id)
    with pytest.raises(StalePlanError):
        await world.service.execute_recovery(plan.plan_id)
    assert world.gateway.requests == []
    assert world.repository.get(txn_id).state is RecoveryState.AWAITING_APPROVAL


async def test_approval_for_plan_a_cannot_execute_plan_b(world, txn_id):  # H
    plan_a = await plan_and_approve(world)
    _late_provider_callback(world, txn_id)
    with pytest.raises(StalePlanError) as exc:
        await world.service.execute_recovery(plan_a.plan_id)
    plan_b = world.service.get_plan(exc.value.replacement_plan_id)

    with pytest.raises(ApprovalRequiredError):
        await world.service.execute_recovery(plan_b.plan_id)
    with pytest.raises(ApprovalMismatchError):  # A's hash does not authorise B
        await world.service.approve_recovery(
            plan_b.plan_id, plan_hash=plan_a.plan_hash, approver="ops.demo"
        )
    with pytest.raises(ApprovalRequiredError):  # stale A cannot run either
        await world.service.execute_recovery(plan_a.plan_id)
    assert world.gateway.requests == []

    await world.service.approve_recovery(
        plan_b.plan_id, plan_hash=plan_b.plan_hash, approver="ops.demo"
    )
    result = await world.service.execute_recovery(plan_b.plan_id)
    assert result.status is ExecutionStatus.SUCCEEDED and result.plan_id == plan_b.plan_id


async def test_changed_plan_requires_fresh_approval(world, txn_id):  # I
    plan = await world.service.create_recovery_plan(txn_id)
    altered = plan.model_copy(update={"rail_id": RailId.BANK_MOMO_BRIDGE}).content_hash()
    assert altered != plan.plan_hash
    with pytest.raises(ApprovalMismatchError, match="content hash"):
        await world.service.approve_recovery(plan.plan_id, plan_hash=altered, approver="ops.demo")
    assert world.repository.get(txn_id).approvals == {}

    # The rail's fee changes before approval: the old plan is stale; a new plan needs approval.
    quote = world.registry.quote(RailId.MOMO_B)
    world.registry.replace_quote(
        quote.model_copy(update={"incremental_fee": Money.of("0.35", Currency.GBP)})
    )
    with pytest.raises(StalePlanError, match="fee changed") as exc:
        await world.service.approve_recovery(
            plan.plan_id, plan_hash=plan.plan_hash, approver="ops.demo"
        )
    new_plan = world.service.get_plan(exc.value.replacement_plan_id)
    assert new_plan.incremental_fee == Money.of("0.35", Currency.GBP)
    assert new_plan.plan_hash != plan.plan_hash
    assert world.service.get_plan_status(new_plan.plan_id) is PlanStatus.PENDING_APPROVAL
    assert new_plan.plan_id not in world.repository.get(txn_id).approvals


async def test_declining_a_plan_escalates_and_blocks_execution(world, txn_id):
    plan = await world.service.create_recovery_plan(txn_id)
    decision = await world.service.reject_recovery(
        plan.plan_id, plan_hash=plan.plan_hash, approver="ops.demo", comment="call recipient"
    )
    assert decision.approved is False
    assert world.repository.get(txn_id).state is RecoveryState.MANUAL_REVIEW
    with pytest.raises(ApprovalRequiredError):
        await world.service.execute_recovery(plan.plan_id)
    assert world.gateway.requests == []
