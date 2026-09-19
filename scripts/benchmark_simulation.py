"""Measure the local (pure Python) simulation workload to size the Phase 4 Modal fan-out.

    uv run python scripts/benchmark_simulation.py
    uv run python scripts/benchmark_simulation.py --trials 2000 10000 --scenarios 3

Synthetic workload only: the SK-10421 routes that survive the hard filters.
"""

from __future__ import annotations

import argparse
import asyncio
import platform
import sys
import time

try:
    from sikarescue.compute.backend import LocalRouteComputeBackend
    from sikarescue.compute.scenarios import SCENARIOS, STRESS_SCENARIOS, workload_config
    from sikarescue.compute.simulation import simulate_route
    from sikarescue.demo_data.sk10421 import TRANSACTION_ID, build_demo_world
    from sikarescue.models import RouteEvaluationRequest
except ImportError:
    sys.exit("Run inside the project environment:  uv run python scripts/benchmark_simulation.py")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--trials", type=int, nargs="+", default=[2000, 10000, 50000])
    parser.add_argument("--scenarios", type=int, default=len(STRESS_SCENARIOS), choices=range(1, 7))
    args = parser.parse_args()

    world = build_demo_world()
    candidates = world.service.discover_recovery_routes(TRANSACTION_ID)
    evaluations = asyncio.run(world.service.evaluate_recovery_routes(TRANSACTION_ID))
    passed = {e.route_id for e in evaluations if e.passed}
    survivors = [c for c in candidates if c.route_id in passed]
    scenarios = [SCENARIOS[s] for s in STRESS_SCENARIOS[: args.scenarios]]
    print(f"Python {platform.python_version()} on {platform.system()} ({platform.machine()})")
    print(f"routes simulated: {len(survivors)} ({', '.join(c.rail.rail_id for c in survivors)})")
    print(f"scenarios: {len(scenarios)} ({', '.join(s.scenario_id for s in scenarios)})")
    print()
    print(f"{'trials/scenario':>16} {'total trials':>13} {'elapsed':>10} {'us/trial':>9}")
    for trials in args.trials:
        started = time.perf_counter()
        for c in survivors:
            for s in scenarios:
                simulate_route(
                    c.route_id,
                    c.simulation_profile,
                    s,
                    seed=10421,
                    trials=trials,
                    sla_seconds=120.0,
                )
        elapsed = time.perf_counter() - started
        total = trials * len(survivors) * len(scenarios)
        print(f"{trials:>16,} {total:>13,} {elapsed:>9.3f}s {elapsed / total * 1e6:>9.2f}")

    request = RouteEvaluationRequest(
        request_id="cmp_00000000beef",
        transaction_id=TRANSACTION_ID,
        candidates=candidates,
        config=workload_config("stress"),
    )
    started = time.perf_counter()
    batch = asyncio.run(LocalRouteComputeBackend().evaluate(request))
    elapsed = time.perf_counter() - started
    print()
    print(
        f"end-to-end LocalRouteComputeBackend (stress workload, {len(candidates)} candidates): "
        f"{batch.summary.simulated_trials:,} trials, {elapsed * 1000:.0f} ms wall "
        f"({batch.summary.elapsed_seconds * 1000:.0f} ms in evaluation)"
    )


if __name__ == "__main__":
    main()
