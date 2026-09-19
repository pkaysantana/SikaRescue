"""Systemic MOMO_A outage: hard constraints stay local, the allocation kernel may run on Modal,
and every result is re-verified before a single figure is reported. No network: Modal's
`.map.aio` is faked in-process with the real kernel."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from sikarescue.compute.outage import (
    UNSERVED,
    AllocationSpec,
    allocate,
    generate_portfolio,
    pack,
    unpack,
)
from sikarescue.demo_data.outage import PORTFOLIO_SEED, PORTFOLIO_SIZE, build_outage_analyzer
from sikarescue.demo_data.sk10421 import build_policy, build_rails
from sikarescue.models import FundsLocation, Money, RailId, RejectionReason
from sikarescue.models.outage import UnservedReason


class FakeModalFunction:
    """Stands in for `modal.Function`: `.map.aio` runs the real kernel in-process."""

    def __init__(self, transform=None, error=None):
        self.batches: list[list[dict]] = []
        self._transform, self._error = transform, error
        self.map = self

    async def aio(self, payloads, order_outputs=True):
        payloads = list(payloads)
        self.batches.append(payloads)
        if self._error:
            raise self._error
        for payload in payloads:
            raw = allocate(AllocationSpec.model_validate(payload)).model_dump(mode="json")
            yield self._transform(raw, payload) if self._transform else raw


@pytest.fixture(scope="module")
def analysis():
    return asyncio.run(build_outage_analyzer("local").run())


def _scenario(analysis, scenario_id):
    return next(s for s in analysis.scenarios if s.scenario.scenario_id == scenario_id)


# ============================================================ constraints the allocation obeys


def test_allocation_respects_liquidity(analysis):
    for result in analysis.scenarios:
        limits = {r.rail_id: r.liquidity for r in result.rails}
        for allocation in result.allocations:
            assert allocation.amount.amount <= limits[allocation.rail_id].amount
            assert allocation.liquidity_utilisation <= 1
    squeeze = _scenario(analysis, "LIQUIDITY_SQUEEZE")
    assert {b.reason for b in squeeze.unserved} >= {UnservedReason.LIQUIDITY_EXHAUSTED}


def test_allocation_respects_capacity(analysis):
    for result in analysis.scenarios:
        limits = {r.rail_id: r.capacity for r in result.rails}
        for allocation in result.allocations:
            assert allocation.obligations <= limits[allocation.rail_id]
    throttled = _scenario(analysis, "MOMO_B_THROTTLED")
    momo_b = next(a for a in throttled.allocations if a.rail_id is RailId.MOMO_B)
    assert momo_b.obligations == momo_b.capacity == 15_000  # half of 30,000: binding
    assert UnservedReason.CAPACITY_EXHAUSTED in {b.reason for b in throttled.unserved}


def test_policy_blocked_and_failed_rails_receive_no_allocation(analysis):
    for result in analysis.scenarios:
        allocated = {a.rail_id for a in result.allocations}
        token = next(r for r in result.rails if r.rail_id is RailId.TOKEN_BRIDGE)
        assert not token.policy_permitted and not token.eligible
        assert RejectionReason.POLICY_DENIED in token.rejection_reasons
        assert RailId.TOKEN_BRIDGE not in allocated and RailId.MOMO_A not in allocated
        for rail in result.rails:
            if not rail.eligible:
                assert rail.rail_id not in allocated
    correlated = _scenario(analysis, "CORRELATED_OUTAGE")
    assert {a.rail_id for a in correlated.allocations} == {RailId.BANK_MOMO_BRIDGE}


def test_over_limit_obligations_are_never_allocated(analysis):
    over = analysis.portfolio.over_policy_limit
    assert over > 0
    for result in analysis.scenarios:
        bucket = next(b for b in result.unserved if b.reason is UnservedReason.POLICY_LIMIT)
        assert bucket.obligations == over


def test_eligibility_masks_agree_with_the_policy_engine():
    """The fleet path applies exactly the single-payment PolicyEngine rules."""
    policy = build_policy()
    rails = {r.rail_id: r for r in build_rails()}
    limit = policy.max_payout.amount
    for amount in ("20.00", "9999.99", "10000.00", "10000.01", "24000.00"):
        money = Money.of(amount, policy.max_payout.currency)
        for rail_id in (RailId.MOMO_B, RailId.BANK_MOMO_BRIDGE, RailId.TOKEN_BRIDGE):
            decision = policy.check_payout(
                "GB->GH", rails[rail_id], FundsLocation.GH_SETTLEMENT_ACCOUNT, money
            )
            rail_allowed = rail_id is not RailId.TOKEN_BRIDGE
            assert decision.permitted == (rail_allowed and Decimal(amount) <= limit)


def test_total_allocation_never_exceeds_outstanding_obligations(analysis):
    portfolio = analysis.portfolio
    for result in analysis.scenarios:
        assert result.affected_obligations == portfolio.size
        assert result.recoverable_obligations <= portfolio.size
        assert result.recoverable_amount.amount <= portfolio.total_amount.amount
        assert sum(a.obligations for a in result.allocations) == result.recoverable_obligations
        assert sum(a.amount.amount for a in result.allocations) == result.recoverable_amount.amount


def test_nominal_outage_results(analysis):
    nominal = _scenario(analysis, "NOMINAL")
    assert nominal.affected_obligations == PORTFOLIO_SIZE == 40_000
    assert nominal.unserved_obligations == analysis.portfolio.over_policy_limit
    by_rail = {a.rail_id: a for a in nominal.allocations}
    assert set(by_rail) == {RailId.MOMO_B, RailId.BANK_MOMO_BRIDGE}
    fee = sum(
        a.obligations
        * {RailId.MOMO_B: Decimal("0.18"), RailId.BANK_MOMO_BRIDGE: Decimal("0.42")}[a.rail_id]
        for a in nominal.allocations
    )
    assert nominal.aggregate_incremental_fee.amount == fee
    bridge_offline = _scenario(analysis, "BRIDGE_OFFLINE")
    no_rail = next(
        b for b in bridge_offline.unserved if b.reason is UnservedReason.NO_ELIGIBLE_RAIL
    )
    # Nobody else serves bank accounts; over-limit ones are reported as POLICY_LIMIT instead.
    portfolio = generate_portfolio(PORTFOLIO_SEED, PORTFOLIO_SIZE)
    banks_over_limit = sum(
        1
        for amount, endpoint in zip(portfolio.amounts, portfolio.endpoints, strict=True)
        if endpoint == 1 and amount > 1_000_000
    )
    assert banks_over_limit > 0
    assert no_rail.obligations == analysis.portfolio.bank_account - banks_over_limit


# ============================================================ kernel + verification


def test_kernel_is_deterministic():
    a, b = generate_portfolio(PORTFOLIO_SEED, 5_000), generate_portfolio(PORTFOLIO_SEED, 5_000)
    assert a.digest == b.digest and a.amounts == b.amounts


async def test_modal_fan_out_matches_the_local_kernel(analysis):
    fake = FakeModalFunction()
    remote = await build_outage_analyzer("modal", remote_function=fake).run()
    assert (remote.backend, remote.fallback_from) == ("modal", None)
    assert remote.parallel_jobs == len(fake.batches[0]) == 5  # one input per scenario
    assert remote.function_ref == "sikarescue-compute/allocate_outage_scenario"
    local = {s.scenario.scenario_id: s for s in analysis.scenarios}
    for result in remote.scenarios:
        expected = local[result.scenario.scenario_id]
        assert result.model_dump(exclude={"compute_seconds"}) == expected.model_dump(
            exclude={"compute_seconds"}
        )
    # Policy never crosses the boundary: only masks, budgets, seed and size.
    assert set(fake.batches[0][0]) == {
        "scenario_id",
        "seed",
        "size",
        "portfolio_digest",
        "rails",
        "masks",
    }
    assert {r["rail_id"] for r in fake.batches[0][0]["rails"]} == {"MOMO_B", "BANK_MOMO_BRIDGE"}


def _reassign(rail_index: int, *, only=None):
    """A tampering worker: pushes obligations onto `rail_index`."""

    def transform(raw, payload):
        assignments = bytearray(unpack(raw["assignments"]))
        for i in range(len(assignments)):
            if only is None or only(i, assignments):
                assignments[i] = rail_index
        return raw | {"assignments": pack(bytes(assignments))}

    return transform


@pytest.mark.parametrize(
    "transform",
    [
        _reassign(3),  # TOKEN_BRIDGE: policy-denied, never sent to compute
        _reassign(1),  # everything onto MOMO_B: liquidity and capacity blown
        _reassign(UNSERVED, only=lambda i, a: i % 97 == 0),  # drops servable obligations
    ],
    ids=["policy-denied-rail", "over-budget", "not-maximal"],
)
async def test_invalid_remote_allocations_are_rejected_and_recomputed_locally(analysis, transform):
    remote = await build_outage_analyzer(
        "modal", remote_function=FakeModalFunction(transform=transform)
    ).run()
    assert remote.backend == "local" and remote.fallback_from == "modal"
    assert "ComputeIntegrityError" in remote.fallback_reason
    local = {s.scenario.scenario_id: s.recoverable_obligations for s in analysis.scenarios}
    assert {s.scenario.scenario_id: s.recoverable_obligations for s in remote.scenarios} == local


async def test_modal_unavailable_falls_back_to_local_visibly():
    remote = await build_outage_analyzer(
        "modal", remote_function=FakeModalFunction(error=ConnectionError("modal unreachable"))
    ).run()
    assert (remote.backend, remote.fallback_from) == ("local", "modal")
    assert "modal unreachable" in remote.fallback_reason


def test_worker_refuses_a_portfolio_it_cannot_reproduce():
    portfolio = generate_portfolio(PORTFOLIO_SEED, 1_000)
    spec = AllocationSpec(
        scenario_id="NOMINAL",
        seed=PORTFOLIO_SEED + 1,  # a different portfolio than the digest describes
        size=1_000,
        portfolio_digest=portfolio.digest,
        rails=(),
        masks=pack(bytes(1_000)),
    )
    with pytest.raises(ValueError, match="digest"):
        allocate(spec)
