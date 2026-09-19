"""Shared test helpers (plain module; fixtures live in conftest.py)."""

from __future__ import annotations

from sikarescue.demo_data.sk10421 import TRANSACTION_ID, DemoWorld
from sikarescue.models import RecoveryPlan


async def plan_and_approve(world: DemoWorld, approver: str = "ops.demo") -> RecoveryPlan:
    plan = await world.service.create_recovery_plan(TRANSACTION_ID)
    await world.service.approve_recovery(plan.plan_id, plan_hash=plan.plan_hash, approver=approver)
    return plan
