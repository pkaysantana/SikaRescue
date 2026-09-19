"""Systemic rail-outage analysis: deterministic constraints and verification around a
parallel allocation kernel (local or Modal).

Responsibilities are split exactly as in single-payment recovery:
  1. LOCAL, before compute: rail status, the corridor's PolicyEngine and recipient
     compatibility decide, per obligation, which rails it may use (eligibility masks). Rails
     that fail a hard constraint are never sent to compute at all.
  2. COMPUTE (Modal fan-out, one job per scenario, or local): the allocation kernel.
  3. LOCAL, after compute: every assignment is re-verified against the masks, liquidity and
     capacity; the allocation must be maximal (nothing unserved still fits); and every metric
     is recomputed from the assignments. A result that fails verification is discarded and
     the scenario set is recomputed locally, visibly.
Modal accelerates the kernel. It never sees policy and cannot allocate around it.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sikarescue import telemetry
from sikarescue.compute.backend import describe_failure
from sikarescue.compute.outage import (
    BANK_ACCOUNT,
    MOBILE_MONEY,
    OUTAGE_FUNCTION_NAME,
    UNSERVED,
    AllocationResult,
    AllocationSpec,
    Portfolio,
    RailBudget,
    allocate,
    generate_portfolio,
    pack,
    unpack,
)
from sikarescue.compute.shards import MODAL_APP_NAME
from sikarescue.errors import ComputeIntegrityError
from sikarescue.models import (
    Currency,
    EndpointType,
    FundsLocation,
    Money,
    RailId,
    RailStatus,
    RejectionReason,
    new_id,
    utcnow,
)
from sikarescue.models.outage import (
    FleetRail,
    OutageAnalysis,
    OutagePortfolio,
    OutageRailAllocation,
    OutageRailConstraint,
    OutageScenario,
    OutageScenarioResult,
    UnservedBucket,
    UnservedReason,
)
from sikarescue.services.policy import PolicyEngine
from sikarescue.services.rails import RailRegistry

_ENDPOINT_CODE = {EndpointType.MOBILE_MONEY: MOBILE_MONEY, EndpointType.BANK_ACCOUNT: BANK_ACCOUNT}


def _money(minor: int, currency: Currency) -> Money:
    return Money(amount=(Decimal(minor) / 100).quantize(Decimal("0.01")), currency=currency)


def _minor(money: Money) -> int:
    return int((money.amount * 100).to_integral_value())


# ------------------------------------------------------------------ compute backends


class OutageComputeBackend(ABC):
    name: str
    function_ref: str | None = None

    @abstractmethod
    async def run(self, specs: list[AllocationSpec]) -> list[AllocationResult]:
        """Run the allocation kernel for every spec, in order."""


class LocalOutageBackend(OutageComputeBackend):
    name = "local"

    async def run(self, specs: list[AllocationSpec]) -> list[AllocationResult]:
        return await asyncio.to_thread(lambda: [allocate(spec) for spec in specs])


class ModalOutageBackend(OutageComputeBackend):
    """One Modal function input per scenario, run in parallel containers."""

    name = "modal"

    def __init__(self, *, remote_function: Any = None) -> None:
        self.function_ref = f"{MODAL_APP_NAME}/{OUTAGE_FUNCTION_NAME}"
        self._remote = remote_function  # injectable for tests: anything with `.map.aio`

    def _function(self) -> Any:
        if self._remote is None:
            import modal  # lazy: only needed when Modal is actually used

            self._remote = modal.Function.from_name(MODAL_APP_NAME, OUTAGE_FUNCTION_NAME)
        return self._remote

    async def run(self, specs: list[AllocationSpec]) -> list[AllocationResult]:
        payloads = [spec.model_dump(mode="json") for spec in specs]
        return [
            AllocationResult.model_validate(raw)
            async for raw in self._function().map.aio(payloads, order_outputs=True)
        ]


# ------------------------------------------------------------------ the analyzer


@dataclass(frozen=True)
class _ScenarioPlan:
    scenario: OutageScenario
    rails: tuple[OutageRailConstraint, ...]
    spec: AllocationSpec
    masks: bytes


class OutageAnalyzer:
    def __init__(
        self,
        *,
        registry: RailRegistry,
        policy: PolicyEngine,
        fleet: tuple[FleetRail, ...],
        scenarios: tuple[OutageScenario, ...],
        failed_rail: RailId,
        corridor: str,
        seed: int,
        size: int,
        backend: OutageComputeBackend,
        fallback: OutageComputeBackend | None = None,
        timeout_seconds: float = 45.0,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.fleet = fleet
        self.scenarios = scenarios
        self.failed_rail = failed_rail
        self.corridor = corridor
        self.seed = seed
        self.size = size
        self.backend = backend
        self.fallback = fallback if fallback is not None else LocalOutageBackend()
        self.timeout_seconds = timeout_seconds

    # --- 1. local hard constraints -------------------------------------------------------

    def _plan(self, scenario: OutageScenario, portfolio: Portfolio) -> _ScenarioPlan:
        limit = self.policy.max_payout  # rule POL-003, as enforced for single payments
        constraints: list[OutageRailConstraint] = []
        budgets: list[RailBudget] = []
        endpoint_bits = {MOBILE_MONEY: 0, BANK_ACCOUNT: 0}
        for index, fleet_rail in enumerate(self.fleet):
            rail = self.registry.get(fleet_rail.rail_id)
            status = RailStatus.DOWN if rail.rail_id in scenario.down_rails else rail.status
            # The policy decision for an in-limit payout from where the funds are held.
            decision = self.policy.check_payout(
                self.corridor, rail, FundsLocation.GH_SETTLEMENT_ACCOUNT, limit
            )
            liquidity = fleet_rail.liquidity.amount * Decimal(
                str(scenario.liquidity_factor.get(rail.rail_id, 1.0))
            )
            capacity = int(fleet_rail.capacity * scenario.capacity_factor.get(rail.rail_id, 1.0))
            fee = self.registry.quote(rail.rail_id).incremental_fee
            reasons = []
            if status is RailStatus.DOWN:
                reasons.append(RejectionReason.RAIL_UNAVAILABLE)
            if not decision.permitted:
                reasons.append(RejectionReason.POLICY_DENIED)
            liquidity_money = Money(
                amount=liquidity.quantize(Decimal("0.01")), currency=fleet_rail.liquidity.currency
            )
            constraints.append(
                OutageRailConstraint(
                    rail_id=rail.rail_id,
                    status=status,
                    policy_permitted=decision.permitted,
                    policy_detail="; ".join(d.detail for d in decision.denials)[:200]
                    or "permitted for this corridor",
                    supported_endpoints=tuple(sorted(rail.supported_endpoints)),
                    liquidity=liquidity_money,
                    capacity=capacity,
                    fee=fee,
                    eligible=not reasons,
                    rejection_reasons=tuple(reasons),
                )
            )
            if reasons:
                continue  # never sent to compute
            budgets.append(
                RailBudget(
                    index=index,
                    rail_id=rail.rail_id,
                    liquidity_minor=_minor(liquidity_money),
                    capacity=capacity,
                    fee_minor=_minor(fee),
                )
            )
            for endpoint in rail.supported_endpoints:
                endpoint_bits[_ENDPOINT_CODE[endpoint]] |= 1 << index
        limit_minor = _minor(limit) if limit.currency is Currency.GHS else -1
        masks = bytes(
            0 if amount > limit_minor else endpoint_bits[endpoint]
            for amount, endpoint in zip(portfolio.amounts, portfolio.endpoints, strict=True)
        )
        spec = AllocationSpec(
            scenario_id=scenario.scenario_id,
            seed=portfolio.seed,
            size=portfolio.size,
            portfolio_digest=portfolio.digest,
            rails=tuple(sorted(budgets, key=lambda b: (b.fee_minor, b.index))),
            masks=pack(masks),
        )
        return _ScenarioPlan(scenario, tuple(constraints), spec, masks)

    # --- 3. local verification + metrics -------------------------------------------------

    def _verify(
        self, plan: _ScenarioPlan, result: AllocationResult, portfolio: Portfolio
    ) -> OutageScenarioResult:
        spec, masks = plan.spec, plan.masks
        problems: list[str] = []
        if result.scenario_id != spec.scenario_id or result.portfolio_digest != portfolio.digest:
            raise ComputeIntegrityError(["result answers a different scenario or portfolio"])
        assignments = unpack(result.assignments)
        if len(assignments) != portfolio.size:
            raise ComputeIntegrityError(["one assignment per obligation is required"])
        budgets = {b.index: b for b in spec.rails}
        used_liquidity: dict[int, int] = defaultdict(int)
        used_capacity: dict[int, int] = defaultdict(int)
        amounts = portfolio.amounts
        for i, rail in enumerate(assignments):
            if rail == UNSERVED:
                continue
            if rail not in budgets:
                problems.append(f"obligation {i} assigned to a rail that failed hard constraints")
            elif not masks[i] >> rail & 1:
                problems.append(f"obligation {i} assigned to a rail it is not eligible for")
            used_liquidity[rail] += amounts[i]
            used_capacity[rail] += 1
            if len(problems) > 5:
                break
        for index, budget in budgets.items():
            if used_liquidity[index] > budget.liquidity_minor:
                problems.append(f"{budget.rail_id} liquidity exceeded")
            if used_capacity[index] > budget.capacity:
                problems.append(f"{budget.rail_id} capacity exceeded")
        remaining = {
            i: (b.liquidity_minor - used_liquidity[i], b.capacity - used_capacity[i])
            for i, b in budgets.items()
        }
        limit_minor = _minor(self.policy.max_payout)
        unserved: Counter[UnservedReason] = Counter()
        unserved_minor: Counter[UnservedReason] = Counter()
        for i, rail in enumerate(assignments):
            if rail != UNSERVED:
                continue
            eligible = [r for r in budgets if masks[i] >> r & 1]
            if any(remaining[r][1] > 0 and remaining[r][0] >= amounts[i] for r in eligible):
                problems.append(f"obligation {i} left unserved although a rail could absorb it")
                break
            if amounts[i] > limit_minor:
                reason = UnservedReason.POLICY_LIMIT
            elif not eligible:
                reason = UnservedReason.NO_ELIGIBLE_RAIL
            elif all(remaining[r][1] == 0 for r in eligible):
                reason = UnservedReason.CAPACITY_EXHAUSTED
            else:
                reason = UnservedReason.LIQUIDITY_EXHAUSTED
            unserved[reason] += 1
            unserved_minor[reason] += amounts[i]
        if problems:
            raise ComputeIntegrityError(problems[:6])

        ghs = Currency.GHS
        allocations = []
        fee_pence = 0
        for index, budget in sorted(budgets.items(), key=lambda kv: (kv[1].fee_minor, kv[0])):
            count, value = used_capacity[index], used_liquidity[index]
            fee_pence += count * budget.fee_minor
            allocations.append(
                OutageRailAllocation(
                    rail_id=budget.rail_id,
                    obligations=count,
                    amount=_money(value, ghs),
                    liquidity=_money(budget.liquidity_minor, ghs),
                    liquidity_utilisation=round(value / budget.liquidity_minor, 4)
                    if budget.liquidity_minor
                    else 0.0,
                    capacity=budget.capacity,
                    capacity_utilisation=round(count / budget.capacity, 4)
                    if budget.capacity
                    else 0.0,
                    incremental_fee=_money(count * budget.fee_minor, Currency.GBP),
                )
            )
        total_minor = sum(amounts)
        served_minor = sum(used_liquidity.values())
        served = sum(used_capacity.values())
        return OutageScenarioResult(
            scenario=plan.scenario,
            rails=plan.rails,
            affected_obligations=portfolio.size,
            affected_amount=_money(total_minor, ghs),
            recoverable_obligations=served,
            recoverable_amount=_money(served_minor, ghs),
            unserved_obligations=portfolio.size - served,
            unserved_amount=_money(total_minor - served_minor, ghs),
            unserved=tuple(
                UnservedBucket(
                    reason=r, obligations=unserved[r], amount=_money(unserved_minor[r], ghs)
                )
                for r in UnservedReason
                if unserved[r]
            ),
            allocations=tuple(allocations),
            aggregate_incremental_fee=_money(fee_pence, Currency.GBP),
            compute_seconds=result.compute_seconds,
            checks=(
                "portfolio digest reproduced by the compute worker",
                "every assignment uses a rail that passed hard constraints",
                "every assignment respects the obligation's policy and compatibility mask",
                "liquidity and capacity respected on every rail",
                "maximal: no unserved obligation still fits a rail's remaining budget",
            ),
        )

    # --- the whole run ---------------------------------------------------------------------

    async def _compute(
        self, backend: OutageComputeBackend, plans: list[_ScenarioPlan], portfolio: Portfolio
    ) -> tuple[list[OutageScenarioResult], float, float]:
        started = time.perf_counter()
        results = await backend.run([p.spec for p in plans])
        computed = time.perf_counter()
        if len(results) != len(plans):
            raise ComputeIntegrityError(["one result per scenario is required"])
        verified = [self._verify(p, r, portfolio) for p, r in zip(plans, results, strict=True)]
        return verified, computed - started, time.perf_counter() - computed

    async def run(self) -> OutageAnalysis:
        started = time.perf_counter()
        with telemetry.span(
            "systemic_outage_analysis",
            failed_rail=self.failed_rail.value,
            configured_backend=self.backend.name,
            portfolio_size=self.size,
            scenarios=len(self.scenarios),
        ) as span:
            portfolio = generate_portfolio(self.seed, self.size)
            plans = [self._plan(s, portfolio) for s in self.scenarios]
            backend, fallback_reason = self.backend, None
            try:
                verified, compute_s, verify_s = await asyncio.wait_for(
                    self._compute(self.backend, plans, portfolio), timeout=self.timeout_seconds
                )
            except Exception as exc:  # CancelledError is not an Exception: it propagates
                if self.backend.name == self.fallback.name:
                    raise
                fallback_reason = describe_failure(exc, self.timeout_seconds)
                telemetry.event(
                    "outage_compute_fallback",
                    level="warn",
                    fallback_from=self.backend.name,
                    reason=fallback_reason,
                )
                backend = self.fallback
                verified, compute_s, verify_s = await self._compute(backend, plans, portfolio)
            nominal = verified[0]
            span.set(
                backend=backend.name,
                fallback_reason=fallback_reason,
                parallel_jobs=len(plans),
                recoverable_nominal=nominal.recoverable_obligations,
                unserved_nominal=nominal.unserved_obligations,
                compute_wall_seconds=round(compute_s, 3),
                verification_seconds=round(verify_s, 3),
            )
            return OutageAnalysis(
                analysis_id=new_id("out"),
                failed_rail=self.failed_rail,
                corridor=self.corridor,
                portfolio=OutagePortfolio(
                    seed=portfolio.seed,
                    size=portfolio.size,
                    digest=portfolio.digest,
                    total_amount=_money(sum(portfolio.amounts), Currency.GHS),
                    mobile_money=portfolio.endpoints.count(MOBILE_MONEY),
                    bank_account=portfolio.endpoints.count(BANK_ACCOUNT),
                    over_policy_limit=sum(
                        a > _minor(self.policy.max_payout) for a in portfolio.amounts
                    ),
                    oldest_minutes=max(portfolio.ages),
                ),
                scenarios=tuple(verified),
                backend=backend.name,
                configured_backend=self.backend.name,
                fallback_from=self.backend.name if fallback_reason else None,
                fallback_reason=fallback_reason,
                parallel_jobs=len(plans),
                wall_seconds=round(time.perf_counter() - started, 3),
                compute_wall_seconds=round(compute_s, 3),
                kernel_seconds=round(sum(r.compute_seconds for r in verified), 3),
                verification_seconds=round(verify_s, 3),
                function_ref=backend.function_ref,
                generated_at=utcnow(),
            )
