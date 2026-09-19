"""Logfire telemetry: complete workflow tracing, no PII, and never on the critical path.

Uses Logfire's own `capfire` fixture (in-memory exporter; nothing leaves the process).
"""

from __future__ import annotations

import io
import json
import re

import logfire
import pytest

from sikarescue import telemetry
from sikarescue.agent.advisor import Orchestrator
from sikarescue.cli.demo import run_demo
from sikarescue.compute.backend import (
    FallbackComputeBackend,
    LocalRouteComputeBackend,
    RouteComputeBackend,
)
from sikarescue.demo_data.sk10421 import (
    RECIPIENT_NAME,
    RECIPIENT_PHONE,
    RECIPIENT_REFERENCE,
    build_demo_world,
)
from sikarescue.models import RecoveryState

from helpers import pydantic_advisor, scripted_model

WORKFLOW_EVENTS = {
    "incident_received",
    "transaction_reconstructed",
    "agent_run_started",
    "agent_tool_inspect_incident",
    "agent_tool_evaluate_routes",
    "local_compute_started",
    "local_compute_finished",
    "route_evaluations_verified",
    "recovery_plan_created",
    "recovery_advice_generated",
    "approval_requested",
    "approval_received",
    "execution_admitted",
    "payout_started",
    "payout_completed",
    "recipient_credit_recorded",
    "reconciliation_completed",
}
PYDANTIC_AI_SPANS = {
    "invoke_agent recovery_agent",
    "execute_tool inspect_incident",
    "execute_tool evaluate_recovery_options",
}


class DownModal(RouteComputeBackend):
    name = "modal"

    async def evaluate(self, request):
        raise ConnectionError("modal unreachable")


@pytest.fixture
def traced(capfire):
    telemetry.configure_telemetry(configure_logfire=False)  # reuse capfire's Logfire
    return capfire


async def _run(world, advisor=None):
    out = io.StringIO()
    outcome = await run_demo(
        world, auto_approve=True, out=out, show_timeline=False, advisor=advisor
    )
    return outcome, out.getvalue()


def _spans(capfire):
    return {s.name: s for s in capfire.exporter.exported_spans}


async def test_complete_workflow_is_one_correlated_trace(world, traced):
    outcome, text = await _run(world, pydantic_advisor(world, scripted_model()))
    spans = traced.exporter.exported_spans
    names = {s.name for s in spans}

    assert outcome.exit_code == 0
    assert names >= WORKFLOW_EVENTS, WORKFLOW_EVENTS - names
    assert names >= PYDANTIC_AI_SPANS, PYDANTIC_AI_SPANS - names
    # One trace from incident to reconciliation, exposed to the application for "View trace".
    trace_ids = {format(s.context.trace_id, "032x") for s in spans}
    assert trace_ids == {outcome.trace_id} and outcome.advisory.trace_id == outcome.trace_id
    assert f"trace id                 : {outcome.trace_id}" in text


async def test_spans_carry_safe_decision_attributes(world, traced):
    outcome, _ = await _run(world, pydantic_advisor(world, scripted_model()))
    spans = _spans(traced)
    plan = outcome.plan

    compute = spans["route_compute"].attributes
    assert compute["compute_backend"] == "local" and compute["simulation_count"] == 12_000
    assert compute["fallback_used"] is False
    planning = spans["recovery_planning"].attributes
    assert planning["plan_id"] == plan.plan_id and planning["selected_route"] == "MOMO_B"
    assert planning["rejected_route_count"] == 2
    assert planning["transaction_revision"] == plan.expected_revision
    advice = spans["recovery_advice"].attributes
    assert advice["orchestrator"] == "pydantic_ai" and advice["plan_id"] == plan.plan_id
    final = spans["sikarescue_recovery"].attributes
    assert final["state"] == "RECONCILED" and final["transaction_id"] == "SK-10421"


async def test_modal_compute_and_fallback_are_traced(traced):
    world = build_demo_world(
        compute=FallbackComputeBackend(DownModal(), LocalRouteComputeBackend())
    )
    outcome, _ = await _run(world)
    spans = _spans(traced)
    assert outcome.exit_code == 0
    assert {"modal_compute_started", "modal_compute_finished", "modal_fallback_used"} <= set(spans)
    finished = spans["modal_compute_finished"].attributes
    assert finished["fallback_used"] is True and finished["fallback_from"] == "modal"
    assert finished["compute_backend"] == "local"


async def test_no_recipient_pii_in_any_span_or_attribute(world, traced):
    await _run(world, pydantic_advisor(world, scripted_model()))
    dumped = json.dumps(traced.exporter.exported_spans_as_dict(), default=str).lower()
    for value in (
        RECIPIENT_NAME,
        RECIPIENT_PHONE,
        RECIPIENT_PHONE.replace(" ", ""),
        RECIPIENT_REFERENCE,
    ):
        assert value.lower() not in dumped


def test_scrubbing_patterns_cover_the_synthetic_pii():
    for value in (RECIPIENT_NAME, RECIPIENT_PHONE, RECIPIENT_REFERENCE, "recipient_phone"):
        assert any(re.search(p, value, re.IGNORECASE) for p in telemetry.PII_SCRUB_PATTERNS)


@pytest.mark.parametrize("agent", [False, True], ids=["deterministic", "pydantic_ai"])
async def test_telemetry_failure_never_breaks_recovery(world, traced, monkeypatch, agent):
    def boom(*args, **kwargs):
        raise RuntimeError("telemetry backend down")

    monkeypatch.setattr(logfire, "span", boom)
    monkeypatch.setattr(logfire, "log", boom)
    monkeypatch.setattr(logfire, "force_flush", boom)
    advisor = pydantic_advisor(world, scripted_model()) if agent else None
    outcome, text = await _run(world, advisor)
    telemetry.flush()

    assert outcome.exit_code == 0
    assert world.repository.get("SK-10421").state is RecoveryState.RECONCILED
    expected = Orchestrator.PYDANTIC_AI if agent else Orchestrator.DETERMINISTIC
    assert outcome.advisory.orchestrator is expected
    assert "state                    : RECONCILED" in text


async def test_logfire_configuration_failure_is_reported_not_raised(world, monkeypatch):
    def refuse(**kwargs):
        raise OSError("cannot reach logfire")

    monkeypatch.setattr(logfire, "configure", refuse)
    status = telemetry.configure_telemetry(token="not-a-real-token")
    assert not status.enabled and "Logfire unavailable (OSError)" in status.detail
    outcome, text = await _run(world)
    assert outcome.exit_code == 0 and outcome.trace_id is None
    assert "Logfire                  : Logfire unavailable (OSError)" in text
