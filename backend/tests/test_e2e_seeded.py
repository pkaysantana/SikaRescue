"""End-to-end seeded demo: the whole SK-10421 story through the deterministic core."""

from __future__ import annotations

from decimal import Decimal

from sikarescue.demo_data.sk10421 import build_demo_world
from sikarescue.models import (
    AuditEventType,
    ExecutionStatus,
    FundsLocation,
    OperationType,
    RailId,
    RecoveryState,
    RejectionReason,
)


async def test_sk10421_end_to_end(txn_id):
    world = build_demo_world()
    service = world.service

    # 1. Incident: sender debited, funds stuck in Ghana settlement, restart NOT safe.
    state = service.get_transaction_state(txn_id)
    assert state.failed_leg is RailId.MOMO_A
    assert state.funds_location is FundsLocation.GH_SETTLEMENT_ACCOUNT
    assert state.sender_debited and not state.safe_to_restart_from_origin

    # 2. Plan: 4 candidates (retry + 3 alternatives), 2 rejected, 2 scored, MOMO_B chosen.
    plan = await service.create_recovery_plan(txn_id)
    rejected = {e.rail_id: e.rejection_reasons for e in plan.evaluations if not e.passed}
    assert len(plan.evaluations) == 4
    assert rejected[RailId.TOKEN_BRIDGE] == (RejectionReason.POLICY_DENIED,)
    assert RejectionReason.RAIL_UNAVAILABLE in rejected[RailId.MOMO_A]
    assert plan.rail_id is RailId.MOMO_B
    assert plan.incremental_fee.amount == Decimal("0.18")
    assert plan.expected_latency_seconds == 74
    selected = next(e for e in plan.evaluations if e.rail_id is RailId.MOMO_B)
    assert selected.quoted_reliability == 0.981
    assert service.get_transaction_state(txn_id).recovery_state is RecoveryState.AWAITING_APPROVAL

    # 3. Human approval of exactly this plan, then execution of the remaining leg only.
    await service.approve_recovery(plan.plan_id, plan_hash=plan.plan_hash, approver="ops.demo")
    result = await service.execute_recovery(plan.plan_id)
    assert result.status is ExecutionStatus.SUCCEEDED
    assert [r.rail_id for r in world.gateway.requests] == [RailId.MOMO_B]

    # 4. Reconcile: exactly one of every effect, zero duplicate sender debits.
    reconciliation = await service.reconcile_transaction(txn_id)
    assert reconciliation.reconciled and reconciliation.duplicate_sender_debits == 0
    journal = world.repository.get(txn_id).journal
    assert [e.operation for e in journal.effects()] == [
        OperationType.SENDER_DEBIT,
        OperationType.FX_CONVERSION,
        OperationType.GH_SETTLEMENT,
        OperationType.RECIPIENT_CREDIT,
    ]
    assert service.get_transaction_state(txn_id).recovery_state is RecoveryState.RECONCILED

    # 5. The audit timeline tells the full story in order.
    milestones = [
        AuditEventType.LEG_SUCCEEDED,
        AuditEventType.LEG_FAILED,
        AuditEventType.DIAGNOSIS_COMPLETED,
        AuditEventType.ROUTES_EVALUATED,
        AuditEventType.RECOVERY_PLAN_CREATED,
        AuditEventType.APPROVAL_GRANTED,
        AuditEventType.EXECUTION_SUCCEEDED,
        AuditEventType.RECONCILED,
    ]
    types = [e.event_type for e in service.get_audit_timeline(txn_id)]
    positions = [types.index(m) for m in milestones]
    assert positions == sorted(positions)
