"""RouteComputeBackend: local backend, reproducibility, and independent verification."""

from __future__ import annotations

import asyncio

import pytest

from sikarescue.compute import evaluation as evaluation_module
from sikarescue.compute.backend import (
    FallbackComputeBackend,
    LocalRouteComputeBackend,
    RouteComputeBackend,
    build_compute_backend,
    evaluate_request,
)
from sikarescue.compute.scenarios import LOCAL_SCENARIOS, simulation_config
from sikarescue.config import Settings
from sikarescue.demo_data.sk10421 import build_demo_world
from sikarescue.errors import ComputeIntegrityError, ConfigurationError, StalePlanningResultError
from sikarescue.models import (
    AuditEventType,
    Currency,
    HardConstraintStatus,
    Money,
    ProviderCallbackRecorded,
    RailId,
    RouteEvaluationBatch,
    RouteEvaluationRequest,
)

CONFIG = simulation_config(trials_per_scenario=500)


def _request(world, txn_id, config=CONFIG) -> RouteEvaluationRequest:
    return RouteEvaluationRequest(
        request_id="cmp_000000000001",
        transaction_id=txn_id,
        candidates=world.service.discover_recovery_routes(txn_id),
        config=config,
    )


async def test_local_backend_evaluates_every_candidate(world, txn_id):
    batch = await LocalRouteComputeBackend().evaluate(_request(world, txn_id))
    by_rail = {e.rail_id: e for e in batch.evaluations}
    assert set(by_rail) == {RailId.MOMO_A, RailId.MOMO_B, RailId.BANK_MOMO_BRIDGE,
                            RailId.TOKEN_BRIDGE}  # fmt: skip
    assert batch.summary.backend == "local"
    assert batch.summary.routes_simulated == 2
    assert batch.summary.simulated_trials == 2 * len(LOCAL_SCENARIOS) * 500
    for rail in (RailId.MOMO_B, RailId.BANK_MOMO_BRIDGE):
        e = by_rail[rail]
        assert e.hard_constraint_status is HardConstraintStatus.PASSED
        assert [r.scenario.scenario_id for r in e.scenario_results] == list(LOCAL_SCENARIOS)
        assert (
            e.simulated_success_probability == e.scenario_results[0].simulated_success_probability
        )
        assert e.simulated_within_sla_probability is not None
        assert e.p50_latency_seconds <= e.p95_latency_seconds
        assert e.compute_backend == "local"


async def test_same_request_gives_identical_evaluations(world, txn_id):
    backend = LocalRouteComputeBackend()
    first = await backend.evaluate(_request(world, txn_id))
    second = await backend.evaluate(_request(world, txn_id))
    assert first.evaluations == second.evaluations
    assert first.summary.model_dump(exclude={"elapsed_seconds"}) == second.summary.model_dump(
        exclude={"elapsed_seconds"}
    )


async def test_hard_rejected_routes_are_never_simulated(world, txn_id, monkeypatch):
    simulated_routes = []
    original = evaluation_module.simulate_route

    def spy(route_id, *args, **kwargs):
        simulated_routes.append(route_id)
        return original(route_id, *args, **kwargs)

    monkeypatch.setattr(evaluation_module, "simulate_route", spy)
    batch = evaluate_request(_request(world, txn_id), "local")
    assert set(simulated_routes) == {"rt_MOMO_B", "rt_BANK_MOMO_BRIDGE"}
    for e in batch.evaluations:
        if not e.passed:
            assert e.scenario_results == ()
            assert e.simulated_success_probability is None and e.score is None


async def test_planning_uses_simulated_fields(world, txn_id):
    plan = await world.service.create_recovery_plan(txn_id)
    selected = next(e for e in plan.evaluations if e.route_id == plan.route_id)
    assert selected.simulated_success_probability is not None
    assert selected.p95_latency_seconds is not None
    assert plan.compute is not None and plan.compute.backend == "local"
    assert plan.compute.simulated_trials == 12_000  # 2 routes x 3 scenarios x 2,000 trials
    audit = next(
        e
        for e in world.service.get_audit_timeline(txn_id)
        if e.event_type is AuditEventType.ROUTES_EVALUATED
    )
    assert audit.data["simulated_trials"] == 12_000 and audit.data["compute_backend"] == "local"


class _RecordingBackend(RouteComputeBackend):
    """A stand-in for a future remote backend: same contract, different implementation."""

    name = "fake-remote"

    def __init__(self, tamper=None, during=None):
        self.requests: list[RouteEvaluationRequest] = []
        self._tamper = tamper
        self._during = during

    async def evaluate(self, request):
        self.requests.append(request)
        if self._during:
            await self._during()
        batch = evaluate_request(request, self.name)
        return self._tamper(batch) if self._tamper else batch


async def test_service_depends_only_on_the_backend_abstraction(txn_id):
    backend = _RecordingBackend()
    world = build_demo_world(compute=backend, simulation=CONFIG)
    plan = await world.service.create_recovery_plan(txn_id)
    assert len(backend.requests) == 1
    assert plan.compute.backend == "fake-remote"
    assert all(e.compute_backend == "fake-remote" for e in plan.evaluations)


def _mark_token_bridge_passed(batch: RouteEvaluationBatch) -> RouteEvaluationBatch:
    """A malicious/buggy backend claims the policy-denied TOKEN_BRIDGE is the best route."""
    donor = next(e for e in batch.evaluations if e.rail_id is RailId.MOMO_B)
    forged = []
    for e in batch.evaluations:
        if e.rail_id is RailId.TOKEN_BRIDGE:
            data = donor.model_dump()
            data |= {"route_id": e.route_id, "rail_id": e.rail_id, "rank": 1}
            data["scenario_results"] = [
                r | {"route_id": e.route_id} for r in data["scenario_results"]
            ]
            e = type(e).model_validate(data)
        forged.append(e)
    return batch.model_copy(update={"evaluations": tuple(forged)})


def _alter_fee(batch: RouteEvaluationBatch) -> RouteEvaluationBatch:
    first = batch.evaluations[0]
    cheaper = first.model_copy(
        update={"estimated_incremental_cost": Money.of("0.01", Currency.GBP)}
    )
    return batch.model_copy(update={"evaluations": (cheaper, *batch.evaluations[1:])})


@pytest.mark.parametrize(
    ("tamper", "problem"),
    [(_mark_token_bridge_passed, "hard-constraint"), (_alter_fee, "incremental fee")],
)
async def test_backend_cannot_override_constraints_or_money(txn_id, tamper, problem):
    world = build_demo_world(compute=_RecordingBackend(tamper=tamper), simulation=CONFIG)
    with pytest.raises(ComputeIntegrityError, match=problem):
        await world.service.create_recovery_plan(txn_id)
    aggregate = world.repository.get(txn_id)
    assert aggregate.plans == {}
    assert aggregate.audit[-1].event_type is AuditEventType.PLANNING_RESULT_DISCARDED


async def test_revision_change_while_backend_computes_is_rejected(txn_id):
    holder = {}

    async def evidence_arrives_mid_compute():
        aggregate = holder["world"].repository.get(txn_id)
        # Compute runs lock-free, so a concurrent command gets the lock (fail fast if not).
        await asyncio.wait_for(aggregate.lock.acquire(), timeout=1)
        try:
            aggregate.journal.record(
                ProviderCallbackRecorded(
                    rail_id=RailId.MOMO_A, attempt_id="att_000000000004", note="late callback"
                )
            )
        finally:
            aggregate.lock.release()

    holder["world"] = world = build_demo_world(
        compute=_RecordingBackend(during=evidence_arrives_mid_compute), simulation=CONFIG
    )
    with pytest.raises(StalePlanningResultError, match="revision changed"):
        await world.service.create_recovery_plan(txn_id)
    assert world.repository.get(txn_id).plans == {}


def test_backend_selection_from_configuration(monkeypatch):
    assert isinstance(build_compute_backend("local"), LocalRouteComputeBackend)
    modal = build_compute_backend("modal")  # lazy: no network until first evaluation
    assert isinstance(modal, FallbackComputeBackend) and modal.name == "modal"
    assert isinstance(modal.fallback, LocalRouteComputeBackend)
    with pytest.raises(ConfigurationError, match="unknown"):
        build_compute_backend("gpu-cluster")
    monkeypatch.setenv("SIKARESCUE_COMPUTE_BACKEND", "local")
    assert Settings().compute_backend == "local"
