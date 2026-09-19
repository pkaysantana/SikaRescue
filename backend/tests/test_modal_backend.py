"""ModalRouteComputeBackend + fallback. No network: `.map.aio` is faked in-process.

The live, networked check is `scripts/modal_smoke.py` (outside the unit suite).
"""

from __future__ import annotations

import asyncio
import io

import modal.exception
import pytest

from sikarescue.cli.demo import run_demo
from sikarescue.compute.backend import (
    FallbackComputeBackend,
    LocalRouteComputeBackend,
    RouteComputeBackend,
    build_compute_backend,
)
from sikarescue.compute.modal_backend import ModalRouteComputeBackend
from sikarescue.compute.scenarios import STRESS_SCENARIOS, workload_config
from sikarescue.compute.shards import ShardSpec, run_shard
from sikarescue.config import Settings
from sikarescue.demo_data.sk10421 import TRANSACTION_ID, build_demo_world
from sikarescue.errors import ComputeIntegrityError
from sikarescue.models import (
    AuditEventType,
    RailId,
    RecoveryState,
    RouteEvaluationBatch,
    RouteEvaluationRequest,
)

# Same six scenarios as the Modal demo workload, fewer trials to keep unit tests fast.
CONFIG = workload_config("stress", trials_per_scenario=300)
LATENCY_TOLERANCE_SECONDS = 0.1  # justified: libm last-ulp differences across platforms


class FakeModalFunction:
    """Stands in for `modal.Function`: `.map.aio` runs each shard in-process."""

    def __init__(self, transform=None, error=None, delay=0.0):
        self.payload_batches: list[list[dict]] = []
        self._transform, self._error, self._delay = transform, error, delay
        self.map = self

    async def aio(self, payloads, order_outputs=True):
        payloads = list(payloads)
        self.payload_batches.append(payloads)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        for payload in payloads:
            raw = run_shard(ShardSpec.model_validate(payload)).model_dump()
            yield self._transform(raw) if self._transform else raw


def _request(world, config=CONFIG) -> RouteEvaluationRequest:
    return RouteEvaluationRequest(
        request_id="cmp_0000000000c1",
        transaction_id=TRANSACTION_ID,
        candidates=world.service.discover_recovery_routes(TRANSACTION_ID),
        config=config,
    )


def _modal(fake, shards=2) -> ModalRouteComputeBackend:
    return ModalRouteComputeBackend(shards_per_scenario=shards, remote_function=fake)


def _world_with(compute):
    return build_demo_world(compute=compute, simulation=CONFIG)


# --- 1, 2: contract and schema ---------------------------------------------------------


async def test_modal_backend_satisfies_the_compute_contract(world):
    backend = _modal(FakeModalFunction())
    assert isinstance(backend, RouteComputeBackend) and backend.name == "modal"
    batch = await backend.evaluate(_request(world))
    assert RouteEvaluationBatch.model_validate(batch.model_dump()) == batch
    s = batch.summary
    assert (s.backend, s.shards_per_scenario, s.fallback_from) == ("modal", 2, None)
    assert s.parallel_jobs == 2 * len(STRESS_SCENARIOS) * 2  # routes x scenarios x shards
    assert s.simulated_trials == 2 * len(STRESS_SCENARIOS) * 300
    assert s.function_ref == "sikarescue-compute/simulate_shard"
    assert s.remote_compute_seconds is not None


# --- 3, 4, 14: agreement with local, reproducibility ------------------------------------


async def test_modal_results_agree_with_local(world):
    local = await LocalRouteComputeBackend().evaluate(_request(world))
    remote = await _modal(FakeModalFunction()).evaluate(_request(world))
    assert [e.route_id for e in remote.evaluations] == [e.route_id for e in local.evaluations]
    for le, re in zip(local.evaluations, remote.evaluations, strict=True):
        assert (le.hard_constraint_status, le.rank) == (re.hard_constraint_status, re.rank)
        for lr, rr in zip(le.scenario_results, re.scenario_results, strict=True):
            assert (lr.simulation_count, lr.successful_runs) == (
                rr.simulation_count,
                rr.successful_runs,
            )
            for field in ("p50_latency_seconds", "p95_latency_seconds"):
                assert abs(getattr(lr, field) - getattr(rr, field)) <= LATENCY_TOLERANCE_SECONDS


@pytest.mark.parametrize("shards", [1, 3, 7])
async def test_shard_boundaries_do_not_change_results(world, shards):
    baseline = await _modal(FakeModalFunction(), shards=2).evaluate(_request(world))
    other = await _modal(FakeModalFunction(), shards=shards).evaluate(_request(world))
    assert other.evaluations == baseline.evaluations


async def test_same_seeded_modal_request_is_reproducible(world):
    first = await _modal(FakeModalFunction()).evaluate(_request(world))
    second = await _modal(FakeModalFunction()).evaluate(_request(world))
    assert first.evaluations == second.evaluations


# --- 5: only surviving routes cross the boundary ---------------------------------------


async def test_hard_rejected_routes_are_never_sent_to_modal(world):
    fake = FakeModalFunction()
    await _modal(fake).evaluate(_request(world))
    sent = {payload["route_id"] for payload in fake.payload_batches[0]}
    assert sent == {"rt_MOMO_B", "rt_BANK_MOMO_BRIDGE"}
    # Nothing but simulation inputs crosses: no policy, liquidity, money or state.
    assert set(fake.payload_batches[0][0]) == {
        "shard_id", "route_id", "profile", "scenario", "seed", "start", "stop", "sla_seconds",
    }  # fmt: skip


# --- 6, 7: malformed / tampered output ---------------------------------------------------


def _claim_perfect_success(raw):
    return raw | {"successes": raw["stop"] - raw["start"]}


def _wrong_shard(raw):
    return raw | {"start": raw["start"] + 1}


def _garbage(raw):
    return {"hello": "world"}


@pytest.mark.parametrize(
    ("transform", "problem"),
    [
        (_claim_perfect_success, "malformed shard result"),
        (_wrong_shard, "does not match request"),
        (_garbage, "malformed shard result"),
    ],
)
async def test_malformed_modal_output_is_rejected(world, transform, problem):
    with pytest.raises(ComputeIntegrityError, match=problem):
        await _modal(FakeModalFunction(transform=transform)).evaluate(_request(world))


class _ForgingBackend(RouteComputeBackend):
    """A compromised remote backend that marks policy-denied TOKEN_BRIDGE as the best route."""

    name = "modal"

    async def evaluate(self, request):
        genuine = await _modal(FakeModalFunction()).evaluate(request)
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
        return genuine.model_copy(update={"evaluations": tuple(forged)})


async def test_tampered_hard_constraint_result_is_rejected(txn_id):
    # Unwrapped: the recovery service refuses to create a plan.
    world = _world_with(_ForgingBackend())
    with pytest.raises(ComputeIntegrityError, match="TOKEN_BRIDGE: hard-constraint"):
        await world.service.create_recovery_plan(txn_id)
    assert world.repository.get(txn_id).plans == {}
    # Wrapped (production configuration): rejected before use, local stands in, visibly.
    wrapped = FallbackComputeBackend(_ForgingBackend(), LocalRouteComputeBackend())
    plan = await _world_with(wrapped).service.create_recovery_plan(txn_id)
    assert plan.compute.fallback_from == "modal"
    assert "hard-constraint" in plan.compute.fallback_reason
    assert plan.rail_id is RailId.MOMO_B  # TOKEN_BRIDGE never becomes permissible


# --- 8, 9, 10: fallback ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fake", "timeout", "reason"),
    [
        (FakeModalFunction(delay=5.0), 0.05, "TimeoutError: no result within 0.05s"),
        (
            FakeModalFunction(error=modal.exception.RemoteError("boom")),
            5.0,
            "RemoteError: remote compute failed",
        ),
        (
            FakeModalFunction(error=ConnectionError("unreachable")),
            5.0,
            "ConnectionError: remote compute unreachable",
        ),
        (FakeModalFunction(transform=_garbage), 5.0, "ComputeIntegrityError"),
    ],
    ids=["timeout", "remote-exception", "unavailable", "malformed"],
)
async def test_modal_failure_falls_back_to_local_and_is_recorded(world, fake, timeout, reason):
    backend = FallbackComputeBackend(
        _modal(fake), LocalRouteComputeBackend(), timeout_seconds=timeout
    )
    batch = await backend.evaluate(_request(world))
    s = batch.summary
    assert (s.backend, s.fallback_from) == ("local", "modal")
    assert reason in s.fallback_reason
    assert s.parallel_jobs == 0  # the statistics were produced in-process
    assert backend.last_failure == s.fallback_reason


# --- 11, 12: recovery completes after fallback; state never corrupted ----------------------


async def test_recovery_reaches_reconciled_after_modal_fallback(txn_id):
    failing = FallbackComputeBackend(
        _modal(FakeModalFunction(error=modal.exception.RemoteError("down"))),
        LocalRouteComputeBackend(),
    )
    world = _world_with(failing)
    plan = await world.service.create_recovery_plan(txn_id)
    assert plan.compute.backend == "local" and plan.compute.fallback_from == "modal"
    audit = [e.event_type for e in world.service.get_audit_timeline(txn_id)]
    assert AuditEventType.COMPUTE_FALLBACK in audit
    await world.service.approve_recovery(plan.plan_id, plan_hash=plan.plan_hash, approver="ops")
    await world.service.execute_recovery(plan.plan_id)
    reconciliation = await world.service.reconcile_transaction(txn_id)
    assert reconciliation.reconciled and reconciliation.duplicate_sender_debits == 0
    assert world.repository.get(txn_id).state is RecoveryState.RECONCILED


class _BrokenBackend(RouteComputeBackend):
    name = "local"

    async def evaluate(self, request):
        raise RuntimeError("local compute also unavailable")


async def test_modal_failure_never_corrupts_transaction_state(txn_id):
    """Even if every backend fails, nothing is persisted and the transaction is retryable."""
    everything_down = FallbackComputeBackend(
        _modal(FakeModalFunction(error=modal.exception.RemoteError("down"))), _BrokenBackend()
    )
    world = _world_with(everything_down)
    aggregate = world.repository.get(txn_id)
    revision, journal = aggregate.revision, aggregate.journal.entries
    with pytest.raises(RuntimeError):
        await world.service.create_recovery_plan(txn_id)
    assert aggregate.plans == {} and aggregate.executions == {}
    assert aggregate.journal.entries == journal and aggregate.revision == revision
    assert aggregate.state is RecoveryState.DIAGNOSING  # planning may simply be retried
    assert not aggregate.lock.locked()
    assert world.gateway.requests == []


# --- 13: local backend unaffected; configuration ----------------------------------------


async def test_local_backend_remains_fully_usable(txn_id):
    world = build_demo_world(compute=build_compute_backend("local"))
    plan = await world.service.create_recovery_plan(txn_id)
    assert plan.compute.backend == "local" and plan.compute.fallback_from is None


def test_modal_backend_is_built_lazily_without_network():
    backend = build_compute_backend("modal", remote_timeout_seconds=7, shards_per_scenario=3)
    assert isinstance(backend, FallbackComputeBackend)
    assert backend.timeout_seconds == 7 and backend.primary.shards_per_scenario == 3
    assert backend.primary._remote is None  # no Modal lookup until the first evaluation


def test_modal_timeout_defaults_to_15s_and_env_overrides(monkeypatch):
    monkeypatch.delenv("SIKARESCUE_MODAL_TIMEOUT_SECONDS", raising=False)
    assert Settings(_env_file=None).modal_timeout_seconds == 15.0
    assert build_compute_backend("modal").timeout_seconds == 15.0
    monkeypatch.setenv("SIKARESCUE_MODAL_TIMEOUT_SECONDS", "9.5")
    assert Settings(_env_file=None).modal_timeout_seconds == 9.5


async def test_cli_shows_modal_fanout_and_reconciles(txn_id):
    world = _world_with(
        FallbackComputeBackend(_modal(FakeModalFunction()), LocalRouteComputeBackend())
    )
    out = io.StringIO()
    outcome = await run_demo(world, auto_approve=True, out=out, show_timeline=False)
    text = out.getvalue()
    assert outcome.exit_code == 0
    assert "backend=modal  parallel jobs=24" in text
    assert "compute backend          : modal" in text
    assert "state                    : RECONCILED" in text


async def test_cli_shows_fallback_visibly(txn_id):
    failing = FallbackComputeBackend(
        _modal(FakeModalFunction(error=modal.exception.RemoteError("down"))),
        LocalRouteComputeBackend(),
    )
    out = io.StringIO()
    outcome = await run_demo(_world_with(failing), auto_approve=True, out=out, show_timeline=False)
    text = out.getvalue()
    assert outcome.exit_code == 0
    assert "modal compute unavailable -> evaluated with local instead" in text
    assert "reason: RemoteError: remote compute failed" in text
    assert "compute backend          : local (fallback from modal)" in text


# --- remote error text never reaches the API/UI ---------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        modal.exception.RemoteError("Traceback ... token=ak-SECRET123 at https://u:p@host/x"),
        RuntimeError("Authorization: Bearer sk-live-SECRET body={'card': '4111'}"),
        ConnectionError("https://user:hunter2@internal:8443/?key=SECRET"),
        ValueError("SECRET response body"),
    ],
    ids=["remote-traceback", "runtime-with-header", "connection-with-credentials", "value"],
)
def test_describe_failure_never_echoes_remote_exception_text(error):
    from sikarescue.compute.backend import describe_failure

    reason = describe_failure(error, 15.0)
    assert "SECRET" not in reason and "hunter2" not in reason and "http" not in reason
    assert reason.startswith(type(error).__name__ + ": remote compute")


def test_describe_failure_keeps_our_own_verification_messages():
    from sikarescue.compute.backend import describe_failure

    reason = describe_failure(ComputeIntegrityError(["TOKEN_BRIDGE: hard-constraint mismatch"]), 1)
    assert reason.startswith("ComputeIntegrityError: result failed local verification")
    assert "hard-constraint" in reason
    assert describe_failure(TimeoutError(), 0.5) == "TimeoutError: no result within 0.5s"
