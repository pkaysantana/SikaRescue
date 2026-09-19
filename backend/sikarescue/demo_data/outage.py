"""SYNTHETIC systemic-outage scenario: MOMO_A fails for the whole GB->GH corridor.

Fleet liquidity and capacity are illustrative treasury/throughput figures for one outage
window, NOT measurements. Fees come from the same rail quotes the single-payment demo uses,
and policy is the same PolicyEngine.
"""

from __future__ import annotations

from sikarescue.models import Currency, Money, RailId
from sikarescue.models.outage import FleetRail, OutageScenario
from sikarescue.services.outage import (
    LocalOutageBackend,
    ModalOutageBackend,
    OutageAnalyzer,
    OutageComputeBackend,
)
from sikarescue.services.rails import RailRegistry

FAILED_RAIL = RailId.MOMO_A
CORRIDOR = "GB->GH"
PORTFOLIO_SEED = 10421
PORTFOLIO_SIZE = 40_000


def build_fleet_rails() -> tuple[FleetRail, ...]:
    def rail(rail_id: RailId, liquidity: str, capacity: int) -> FleetRail:
        return FleetRail(
            rail_id=rail_id, liquidity=Money.of(liquidity, Currency.GHS), capacity=capacity
        )

    return (
        rail(RailId.MOMO_A, "60000000.00", 50_000),  # the failed rail
        rail(RailId.MOMO_B, "30000000.00", 30_000),
        rail(RailId.BANK_MOMO_BRIDGE, "14000000.00", 12_000),
        rail(RailId.TOKEN_BRIDGE, "50000000.00", 40_000),  # not permitted by corridor policy
    )


def build_outage_scenarios() -> tuple[OutageScenario, ...]:
    down = (FAILED_RAIL,)
    return (
        OutageScenario(
            scenario_id="NOMINAL",
            label="MOMO_A outage",
            description="MOMO_A is down; fallback rails at planned liquidity and capacity.",
            down_rails=down,
        ),
        OutageScenario(
            scenario_id="LIQUIDITY_SQUEEZE",
            label="Liquidity squeeze",
            description="Fallback prefunding 40% below plan.",
            down_rails=down,
            liquidity_factor={RailId.MOMO_B: 0.6, RailId.BANK_MOMO_BRIDGE: 0.6},
        ),
        OutageScenario(
            scenario_id="MOMO_B_THROTTLED",
            label="MOMO_B throttled",
            description="MOMO_B accepts half its planned volume.",
            down_rails=down,
            capacity_factor={RailId.MOMO_B: 0.5},
        ),
        OutageScenario(
            scenario_id="BRIDGE_OFFLINE",
            label="Bridge offline too",
            description="BANK_MOMO_BRIDGE is also down: bank-account recipients have no rail.",
            down_rails=(*down, RailId.BANK_MOMO_BRIDGE),
        ),
        OutageScenario(
            scenario_id="CORRELATED_OUTAGE",
            label="Correlated MoMo outage",
            description="MOMO_B fails with MOMO_A; only the bridge remains.",
            down_rails=(*down, RailId.MOMO_B),
        ),
    )


def build_outage_analyzer(
    compute_backend: str = "local",
    *,
    timeout_seconds: float = 45.0,
    size: int = PORTFOLIO_SIZE,
    seed: int = PORTFOLIO_SEED,
    remote_function: object | None = None,
) -> OutageAnalyzer:
    """The synthetic MOMO_A corridor outage, on the same rails, quotes and policy as SK-10421."""
    from sikarescue.demo_data.sk10421 import (
        build_policy,
        build_quotes,
        build_rails,
        build_simulation_profiles,
    )

    backend: OutageComputeBackend = (
        ModalOutageBackend(remote_function=remote_function)
        if compute_backend == "modal"
        else LocalOutageBackend()
    )
    return OutageAnalyzer(
        registry=RailRegistry(build_rails(), build_quotes(), build_simulation_profiles()),
        policy=build_policy(),
        fleet=build_fleet_rails(),
        scenarios=build_outage_scenarios(),
        failed_rail=FAILED_RAIL,
        corridor=CORRIDOR,
        seed=seed,
        size=size,
        backend=backend,
        timeout_seconds=timeout_seconds,
    )
