"""LIVE Modal smoke test + benchmark (network, real Modal account; NOT part of the unit suite).

    uv run modal deploy -m sikarescue.compute.modal_app      # once
    uv run python scripts/modal_smoke.py                     # stress workload, shards 1 2 4
    uv run python scripts/modal_smoke.py --shards 2 --repeats 3

What it proves, on the seeded SK-10421 fixture:
  1. the deployed function evaluates the same request as the local backend;
  2. success counts / trial counts match exactly, latency percentiles within 0.1 s;
  3. ranking is identical and passes the application's independent verification;
  4. a tampered Modal response is rejected (fallback to local, or plan refused).
Synthetic data only.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import time

try:
    from sikarescue.compute.backend import FallbackComputeBackend, LocalRouteComputeBackend
    from sikarescue.compute.modal_backend import ModalRouteComputeBackend
    from sikarescue.compute.scenarios import workload_config
    from sikarescue.compute.verification import verify_evaluations
    from sikarescue.demo_data.sk10421 import TRANSACTION_ID, build_demo_world
    from sikarescue.errors import ComputeIntegrityError
    from sikarescue.models import RailId, RouteEvaluationBatch, RouteEvaluationRequest
except ImportError:
    sys.exit("Run inside the project environment:  uv run python scripts/modal_smoke.py")

LATENCY_TOLERANCE_SECONDS = 0.1


def compare(local: RouteEvaluationBatch, remote: RouteEvaluationBatch) -> list[str]:
    """Differences that would matter; empty list = equivalent."""
    problems = []
    status = lambda b: {e.route_id: e.hard_constraint_status for e in b.evaluations}  # noqa: E731
    if status(local) != status(remote):
        problems.append("accepted/rejected route sets differ")
    rank = lambda b: [e.route_id for e in b.evaluations if e.passed]  # noqa: E731
    if rank(local) != rank(remote):
        problems.append(f"ranking differs: {rank(local)} vs {rank(remote)}")
    remote_by_route = {e.route_id: e for e in remote.evaluations}
    for le in local.evaluations:
        for lr, rr in zip(
            le.scenario_results, remote_by_route[le.route_id].scenario_results, strict=True
        ):
            where = f"{le.rail_id}/{lr.scenario.scenario_id}"
            if (lr.simulation_count, lr.successful_runs) != (
                rr.simulation_count,
                rr.successful_runs,
            ):
                problems.append(f"{where}: counts differ")
            for field in ("p50_latency_seconds", "p95_latency_seconds"):
                a, b = getattr(lr, field), getattr(rr, field)
                if abs(a - b) > LATENCY_TOLERANCE_SECONDS:
                    problems.append(f"{where}: {field} {a} vs {b}")
            if abs(lr.recovery_within_sla_probability - rr.recovery_within_sla_probability) > 1e-4:
                problems.append(f"{where}: within-SLA probability differs")
    return problems


class _TamperingFunction:
    """Wraps the REAL deployed function and corrupts one shard result in flight."""

    def __init__(self, real):
        self.real = real
        self.map = self

    async def aio(self, payloads, order_outputs=True):
        corrupted = False
        async for raw in self.real.map.aio(payloads, order_outputs=order_outputs):
            if not corrupted:
                raw = raw | {"successes": raw["stop"] - raw["start"]}  # claim 100% success
                corrupted = True
            yield raw


async def main() -> int:
    parser = argparse.ArgumentParser(description="Live Modal smoke test and benchmark")
    parser.add_argument("--shards", type=int, nargs="+", default=[1, 2, 4])
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--workload", default="stress", choices=["quick", "stress"])
    args = parser.parse_args()

    profile = subprocess.run(["modal", "profile", "current"], capture_output=True, text=True)
    print(f"Modal workspace/profile: {profile.stdout.strip() or '(unknown)'}")
    world = build_demo_world()
    candidates = world.service.discover_recovery_routes(TRANSACTION_ID)
    config = workload_config(args.workload)
    request = RouteEvaluationRequest(
        request_id="cmp_00000000a0a0",
        transaction_id=TRANSACTION_ID,
        candidates=candidates,
        config=config,
    )
    print(
        f"workload: {args.workload} = {len(config.scenarios)} scenarios x "
        f"{config.trials_per_scenario:,} trials per surviving route"
    )

    started = time.perf_counter()
    local = await LocalRouteComputeBackend().evaluate(request)
    local_wall = time.perf_counter() - started
    print(
        f"\nLOCAL  {local.summary.simulated_trials:,} trials  wall {local_wall:6.2f}s  "
        f"(single process)"
    )

    ok = True
    for shards in args.shards:
        backend = ModalRouteComputeBackend(shards_per_scenario=shards)
        for attempt in range(1, args.repeats + 1):
            started = time.perf_counter()
            remote = await backend.evaluate(request)
            wall = time.perf_counter() - started
            s = remote.summary
            problems = compare(local, remote) + verify_evaluations(
                candidates, remote.evaluations, config
            )
            ok &= not problems
            print(
                f"MODAL  shards={shards} run {attempt}: {s.parallel_jobs:>3} jobs  "
                f"{s.simulated_trials:,} trials  wall {wall:6.2f}s  "
                f"remote compute {s.remote_compute_seconds:6.2f}s (sum)  "
                f"{'MATCHES local' if not problems else 'MISMATCH: ' + '; '.join(problems)}"
            )

    print("\nTamper demo 1: corrupted shard result in flight -> rejected -> local fallback")
    real = ModalRouteComputeBackend(shards_per_scenario=2)._function()
    tampered = FallbackComputeBackend(
        ModalRouteComputeBackend(shards_per_scenario=2, remote_function=_TamperingFunction(real)),
        LocalRouteComputeBackend(),
    )
    fallback_batch = await tampered.evaluate(request)
    s = fallback_batch.summary
    print(f"  backend={s.backend} fallback_from={s.fallback_from}\n  reason: {s.fallback_reason}")
    ok &= s.backend == "local" and s.fallback_from == "modal"

    print("\nTamper demo 2: real Modal batch forged to admit TOKEN_BRIDGE -> verification")
    genuine = await ModalRouteComputeBackend(shards_per_scenario=2).evaluate(request)
    donor = next(e for e in genuine.evaluations if e.rail_id is RailId.MOMO_B)
    forged = []
    for e in genuine.evaluations:
        if e.rail_id is RailId.TOKEN_BRIDGE:
            data = donor.model_dump() | {"route_id": e.route_id, "rail_id": e.rail_id}
            data["scenario_results"] = [
                r | {"route_id": e.route_id} for r in data["scenario_results"]
            ]
            e = type(e).model_validate(data)
        forged.append(e)
    problems = verify_evaluations(candidates, tuple(forged), config)
    print(f"  verification problems: {problems}")
    ok &= bool(problems)
    try:
        raise ComputeIntegrityError(problems)
    except ComputeIntegrityError as exc:
        print(f"  -> {type(exc).__name__}: plan creation would be refused")

    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
