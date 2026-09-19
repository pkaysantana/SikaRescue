"""Hard constraints run before scoring (requirements 3-6)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from sikarescue.compute.scoring import ScoringInput, score_route
from sikarescue.errors import NoEligibleRouteError
from sikarescue.models import (
    Currency,
    EndpointType,
    Money,
    RailId,
    RailStatus,
    RecoveryState,
    RejectionReason,
)
from sikarescue.services.routes import hard_constraint_violations


def _by_rail(evaluations):
    return {e.rail_id: e for e in evaluations}


def test_seeded_evaluation_outcome(world, txn_id):
    evaluations = world.service.evaluate_recovery_routes(txn_id)
    by_rail = _by_rail(evaluations)
    assert set(by_rail) == {RailId.MOMO_A, RailId.MOMO_B, RailId.BANK_MOMO_BRIDGE,
                            RailId.TOKEN_BRIDGE}  # fmt: skip
    assert [e.rail_id for e in evaluations if e.passed] == [RailId.MOMO_B, RailId.BANK_MOMO_BRIDGE]
    assert by_rail[RailId.MOMO_B].rank == 1
    assert by_rail[RailId.MOMO_B].score.total > by_rail[RailId.BANK_MOMO_BRIDGE].score.total


def test_token_bridge_rejected_by_policy(world, txn_id):  # 3
    token = _by_rail(world.service.evaluate_recovery_routes(txn_id))[RailId.TOKEN_BRIDGE]
    assert not token.passed
    assert token.rejection_reasons == (RejectionReason.POLICY_DENIED,)
    assert "POL-001" in token.rejection_details[0]
    assert token.score is None and token.rank is None  # never scored, despite best quote
    decision = world.service.check_policy(txn_id, RailId.TOKEN_BRIDGE)
    assert not decision.permitted and [d.rule_id for d in decision.denials] == ["POL-001"]


def test_down_rail_cannot_be_selected(world, txn_id):  # 4
    by_rail = _by_rail(world.service.evaluate_recovery_routes(txn_id))
    assert RejectionReason.RAIL_UNAVAILABLE in by_rail[RailId.MOMO_A].rejection_reasons
    world.registry.set_status(RailId.MOMO_B, RailStatus.DOWN)
    by_rail = _by_rail(world.service.evaluate_recovery_routes(txn_id))
    assert by_rail[RailId.MOMO_B].rejection_reasons == (RejectionReason.RAIL_UNAVAILABLE,)
    assert [e.rail_id for e in by_rail.values() if e.passed] == [RailId.BANK_MOMO_BRIDGE]


def test_momo_a_down_rule_alone_rejects_it(world, txn_id):  # 18
    """Isolate the DOWN rule from 'failed earlier': DOWN alone is enough to reject MOMO_A."""
    momo_a = next(
        c for c in world.service.discover_recovery_routes(txn_id) if c.rail.rail_id is RailId.MOMO_A
    )
    assert momo_a.rail.status is RailStatus.DOWN
    only_down = momo_a.model_copy(update={"failed_earlier_for_transaction": False})
    assert [r for r, _ in hard_constraint_violations(only_down)] == [
        RejectionReason.RAIL_UNAVAILABLE
    ]


async def test_momo_a_never_selected_while_down_even_as_last_resort(world, txn_id):  # 18
    world.registry.set_status(RailId.MOMO_B, RailStatus.DOWN)
    world.registry.set_status(RailId.BANK_MOMO_BRIDGE, RailStatus.DOWN)
    with pytest.raises(NoEligibleRouteError):
        await world.service.create_recovery_plan(txn_id)
    aggregate = world.repository.get(txn_id)
    assert aggregate.plans == {}  # no plan on a DOWN rail, escalated instead
    assert aggregate.state is RecoveryState.MANUAL_REVIEW
    assert world.gateway.requests == []


async def test_down_rail_never_planned(world, txn_id):  # 4
    world.registry.set_status(RailId.MOMO_B, RailStatus.DOWN)
    plan = await world.service.create_recovery_plan(txn_id)
    assert plan.rail_id is RailId.BANK_MOMO_BRIDGE


async def test_incompatible_rail_cannot_be_selected(world, txn_id):  # 5
    momo_b = world.registry.get(RailId.MOMO_B)
    world.registry.replace_rail(
        momo_b.model_copy(update={"supported_endpoints": frozenset({EndpointType.BANK_ACCOUNT})})
    )
    by_rail = _by_rail(world.service.evaluate_recovery_routes(txn_id))
    assert by_rail[RailId.MOMO_B].rejection_reasons == (RejectionReason.RECIPIENT_INCOMPATIBLE,)
    plan = await world.service.create_recovery_plan(txn_id)
    assert plan.rail_id is RailId.BANK_MOMO_BRIDGE


async def test_insufficient_liquidity_cannot_be_selected(world, txn_id):  # 6
    world.liquidity.set_balance(RailId.MOMO_B, Money.of("1000.00", Currency.GHS))
    liquidity = world.service.check_liquidity(txn_id, RailId.MOMO_B)
    assert not liquidity.sufficient
    by_rail = _by_rail(world.service.evaluate_recovery_routes(txn_id))
    assert by_rail[RailId.MOMO_B].rejection_reasons == (RejectionReason.INSUFFICIENT_LIQUIDITY,)
    plan = await world.service.create_recovery_plan(txn_id)
    assert plan.rail_id is RailId.BANK_MOMO_BRIDGE


def test_scoring_is_deterministic_and_bounded():
    inp = ScoringInput("rt_MOMO_B", 0.981, Decimal("0.18"), 74, 1)
    first, second = score_route(inp), score_route(inp)
    assert first == second
    assert first.total == 0.8297
    extreme = score_route(ScoringInput("rt_X", 0.5, Decimal("9.99"), 9999, 9))
    assert extreme.total == 0.0


def test_scoring_prefers_cheaper_when_otherwise_equal():
    cheap = score_route(ScoringInput("rt_A", 0.97, Decimal("0.10"), 60, 1))
    dear = score_route(ScoringInput("rt_B", 0.97, Decimal("0.50"), 60, 1))
    assert cheap.total > dear.total
