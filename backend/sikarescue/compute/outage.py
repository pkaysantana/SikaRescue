"""Systemic rail outage: deterministic synthetic portfolio + allocation kernel. Pure, no Modal.

The kernel answers one control-plane question per scenario: if a payout rail fails, how many
outstanding obligations can each fallback rail absorb within its liquidity and capacity?

What crosses to a compute worker (local thread or Modal container):
  * the portfolio's seed and size (the worker regenerates it and must reproduce its digest);
  * per-obligation eligibility MASKS, computed locally by the deterministic layer from rail
    status, corridor policy and recipient compatibility - the worker never sees policy;
  * each eligible rail's liquidity and capacity budget, cheapest rail first.
What comes back: one assignment per obligation (a rail index, or UNSERVED). The caller
re-verifies every assignment against the masks and budgets and recomputes all metrics itself.

Allocation rule (deterministic greedy, not globally optimal): oldest obligation first (ties
by index); each goes to the cheapest eligible rail that still has both the liquidity and a
capacity slot for it; otherwise it stays unserved.
"""

from __future__ import annotations

import base64
import hashlib
import math
import random
import struct
import time
import zlib
from dataclasses import dataclass

from pydantic import Field, model_validator

from sikarescue.models import DomainModel, RailId

OUTAGE_FUNCTION_NAME = "allocate_outage_scenario"
UNSERVED = 255
MAX_RAILS = 8
MOBILE_MONEY, BANK_ACCOUNT = 0, 1

# Synthetic portfolio shape (illustrative, not measured).
MEDIAN_AMOUNT_GHS = 700.0
AMOUNT_SIGMA = 0.9
MIN_AMOUNT_GHS, MAX_AMOUNT_GHS = 20.0, 25_000.0
BANK_ACCOUNT_SHARE = 0.15
MEAN_AGE_MINUTES = 45.0
MAX_AGE_MINUTES = 240


@dataclass(frozen=True)
class Portfolio:
    seed: int
    size: int
    amounts: tuple[int, ...]  # minor units (pesewas)
    endpoints: bytes  # MOBILE_MONEY / BANK_ACCOUNT per obligation
    ages: tuple[int, ...]  # minutes the payout has been outstanding
    digest: str


def _digest(amounts: tuple[int, ...], endpoints: bytes, ages: tuple[int, ...]) -> str:
    packed = struct.pack(f"<{len(amounts)}q", *amounts) + endpoints
    packed += struct.pack(f"<{len(ages)}H", *ages)
    return hashlib.sha256(packed).hexdigest()


def generate_portfolio(seed: int, size: int) -> Portfolio:
    """Deterministic synthetic portfolio of outstanding GHS payout obligations."""
    rng = random.Random(seed)
    mu = math.log(MEDIAN_AMOUNT_GHS)
    amounts, ages = [], []
    endpoints = bytearray(size)
    for i in range(size):
        ghs = min(max(rng.lognormvariate(mu, AMOUNT_SIGMA), MIN_AMOUNT_GHS), MAX_AMOUNT_GHS)
        amounts.append(round(ghs * 100))
        endpoints[i] = BANK_ACCOUNT if rng.random() < BANK_ACCOUNT_SHARE else MOBILE_MONEY
        ages.append(min(int(rng.expovariate(1 / MEAN_AGE_MINUTES)), MAX_AGE_MINUTES))
    amounts_t, ages_t, endpoints_b = tuple(amounts), tuple(ages), bytes(endpoints)
    return Portfolio(
        seed, size, amounts_t, endpoints_b, ages_t, _digest(amounts_t, endpoints_b, ages_t)
    )


def processing_order(portfolio: Portfolio) -> list[int]:
    """Oldest obligation first; ties by index. Part of the kernel's published rule."""
    ages = portfolio.ages
    return sorted(range(portfolio.size), key=lambda i: (-ages[i], i))


def pack(data: bytes) -> str:
    return base64.b64encode(zlib.compress(data, 6)).decode("ascii")


def unpack(text: str) -> bytes:
    return zlib.decompress(base64.b64decode(text))


class RailBudget(DomainModel):
    index: int = Field(ge=0, lt=MAX_RAILS)
    rail_id: RailId
    liquidity_minor: int = Field(ge=0)
    capacity: int = Field(ge=0)
    fee_minor: int = Field(ge=0)  # incremental fee per payout, GBP pence


class AllocationSpec(DomainModel):
    scenario_id: str = Field(pattern=r"^[A-Z_]{3,40}$")
    seed: int = Field(ge=0)
    size: int = Field(gt=0, le=200_000)
    portfolio_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    rails: tuple[RailBudget, ...]  # eligible rails only, cheapest first
    masks: str  # base64(zlib(one byte per obligation: bit i = may use rail index i))

    @model_validator(mode="after")
    def _check(self) -> AllocationSpec:
        fees = [r.fee_minor for r in self.rails]
        if fees != sorted(fees) or len({r.index for r in self.rails}) != len(self.rails):
            raise ValueError("rails must be unique and ordered cheapest first")
        return self


class AllocationResult(DomainModel):
    scenario_id: str
    portfolio_digest: str
    assignments: str  # base64(zlib(one byte per obligation: rail index or UNSERVED))
    compute_seconds: float = Field(ge=0)


def allocate(spec: AllocationSpec) -> AllocationResult:
    """The allocation kernel. Runs identically in-process or in a Modal container."""
    started = time.perf_counter()
    portfolio = generate_portfolio(spec.seed, spec.size)
    if portfolio.digest != spec.portfolio_digest:
        raise ValueError("regenerated portfolio does not match the requested digest")
    masks = unpack(spec.masks)
    if len(masks) != spec.size:
        raise ValueError("one eligibility mask per obligation is required")
    remaining_liquidity = {r.index: r.liquidity_minor for r in spec.rails}
    remaining_capacity = {r.index: r.capacity for r in spec.rails}
    order = [r.index for r in spec.rails]  # cheapest first
    amounts = portfolio.amounts
    assignments = bytearray([UNSERVED]) * spec.size
    for i in processing_order(portfolio):
        mask, amount = masks[i], amounts[i]
        if not mask:
            continue
        for rail in order:
            if (
                mask >> rail & 1
                and remaining_capacity[rail] > 0
                and remaining_liquidity[rail] >= amount
            ):
                assignments[i] = rail
                remaining_capacity[rail] -= 1
                remaining_liquidity[rail] -= amount
                break
    return AllocationResult(
        scenario_id=spec.scenario_id,
        portfolio_digest=portfolio.digest,
        assignments=pack(bytes(assignments)),
        compute_seconds=round(time.perf_counter() - started, 4),
    )
