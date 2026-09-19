"""Demo API: thin over the deterministic services, idempotent under double-clicks, resettable.

In-process ASGI transport: no network, no Modal, no model. `DemoView` is the only payload
the UI renders, so it is asserted directly.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from pydantic_ai import ModelResponse
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.function import AgentInfo, FunctionModel

from sikarescue.api.app import create_app
from sikarescue.api.session import DemoSession
from sikarescue.compute.backend import (
    FallbackComputeBackend,
    LocalRouteComputeBackend,
    RouteComputeBackend,
)
from sikarescue.config import Settings
from sikarescue.demo_data.sk10421 import (
    RECIPIENT_NAME,
    RECIPIENT_PHONE,
    RECIPIENT_REFERENCE,
    TRANSACTION_ID,
    build_demo_world,
)
from sikarescue.models import AuditEventType, OperationType

from helpers import pydantic_advisor, scripted_model

SETTINGS = Settings(
    _env_file=None, compute_backend="local", agent_mode="deterministic", payout_latency_seconds=0
)


class DownModal(RouteComputeBackend):
    name = "modal"

    async def evaluate(self, request):
        raise ConnectionError("modal unreachable")


def _client(session: DemoSession | None = None) -> httpx.AsyncClient:
    app = create_app(SETTINGS, session=session or DemoSession(SETTINGS), configure_telemetry=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://demo")


@pytest.fixture
async def client():
    async with _client() as c:
        yield c


async def _analyse(c: httpx.AsyncClient) -> dict:
    response = await c.post("/api/demo/analyse")
    assert response.status_code == 200, response.text
    return response.json()


async def _run_to_reconciled(c: httpx.AsyncClient) -> dict:
    plan = (await _analyse(c))["analysis"]["plan"]
    approved = await c.post(
        "/api/demo/approve", json={"plan_id": plan["plan_id"], "plan_hash": plan["plan_hash"]}
    )
    assert approved.status_code == 200, approved.text
    executed = await c.post("/api/demo/execute", json={"plan_id": plan["plan_id"]})
    assert executed.status_code == 200, executed.text
    return executed.json()


async def test_health(client):
    body = (await client.get("/api/health")).json()
    assert body["status"] == "ok" and body["single_worker"] is True
    assert body["configured_compute_backend"] == "local"


async def test_initial_view_is_the_seeded_failure(client):
    view = (await client.get("/api/demo/status")).json()
    assert view["state"] == "FAILED" and view["next_action"] == "analyse"
    assert view["transaction"] == {
        "transaction_id": "SK-10421",
        "send_amount": "£120.00",
        "payout_amount": "GHS 1,830.00",
        "fx_rate": "15.25",
        "origin": "United Kingdom",
        "destination": "Ghana",
        "recipient_rail": "Mobile Money",
        "recipient_token": "rcp_8f3k2m9q",
    }
    legs = [(leg["label"], leg["status"], leg["funds_here"]) for leg in view["journey"]]
    assert legs == [
        ("Sender debit", "SUCCESS", False),
        ("GBP → GHS FX", "SUCCESS", False),
        ("Ghana settlement", "SUCCESS", True),
        ("Payout", "FAILED", False),
    ]
    diagnosis = view["diagnosis"]
    assert diagnosis["funds_location"] == "GH_SETTLEMENT_ACCOUNT"
    assert diagnosis["sender_debited"] and diagnosis["sender_debit_count"] == 1
    assert diagnosis["safe_to_restart_from_origin"] is False
    assert view["analysis"] is None and view["payout_calls"] == 0


async def test_analysis_shows_deterministic_routes_plan_and_compute(client):
    view = await _analyse(client)
    assert view["state"] == "AWAITING_APPROVAL" and view["next_action"] == "approve"
    routes = {r["rail_id"]: r for r in view["analysis"]["routes"]}
    assert routes["MOMO_A"]["eligible"] is False
    assert routes["MOMO_A"]["rejection_reasons"] == [
        "RAIL_UNAVAILABLE",
        "FAILED_EARLIER_FOR_TRANSACTION",
    ]
    assert routes["TOKEN_BRIDGE"]["rejection_reasons"] == ["POLICY_DENIED"]
    assert routes["MOMO_B"]["selected"] and routes["MOMO_B"]["fee"] == "£0.18"
    assert routes["BANK_MOMO_BRIDGE"]["eligible"] and not routes["BANK_MOMO_BRIDGE"]["selected"]
    plan = view["analysis"]["plan"]
    assert (plan["rail_id"], plan["incremental_fee"], plan["source"]) == (
        "MOMO_B",
        "£0.18",
        "GH_SETTLEMENT_ACCOUNT",
    )
    assert plan["plan_hash"].startswith(plan["plan_hash_short"]) and len(plan["plan_hash"]) == 64
    compute = view["analysis"]["compute"]
    assert compute["backend"] == "local" and compute["fallback_from"] is None
    assert compute["rejected_routes_simulated"] == 0  # rejected routes are never simulated
    advice = view["analysis"]["advice"]
    assert advice["orchestrator"] == "deterministic" and advice["ai_used"] is False
    assert advice["model"] is None and advice["trace_id"] is None


async def test_full_flow_reconciles_with_exactly_one_debit_and_credit(client):
    view = await _run_to_reconciled(client)
    assert view["state"] == "RECONCILED" and view["next_action"] == "done"
    proof = view["reconciliation"]
    assert proof["reconciled"] is True and all(c["passed"] for c in proof["checks"])
    assert (
        proof["sender_debit_count"],
        proof["recipient_credit_count"],
        proof["duplicate_sender_debits"],
        proof["funds_location"],
    ) == (1, 1, 0, "RECIPIENT_ENDPOINT")
    assert view["payout_calls"] == 1 and view["execution"]["rail_id"] == "MOMO_B"
    transitions = [t["to_state"] for t in view["transitions"]]
    assert transitions[-4:] == ["APPROVED", "RECOVERY_EXECUTING", "RECOVERED", "RECONCILED"]
    final_leg = view["journey"][-1]
    assert (final_leg["rail_id"], final_leg["status"], final_leg["is_recovery"]) == (
        "MOMO_B",
        "SUCCESS",
        True,
    )
    assert final_leg["funds_here"] and view["approval"]["approver"] == "demo.operator"


async def test_double_clicks_never_duplicate_work():
    session = DemoSession(SETTINGS)
    async with _client(session) as c:
        first, second = await asyncio.gather(
            c.post("/api/demo/analyse"), c.post("/api/demo/analyse")
        )
        plan = first.json()["analysis"]["plan"]
        assert second.json()["analysis"]["plan"]["plan_id"] == plan["plan_id"]
        aggregate = session.world.repository.get(TRANSACTION_ID)
        advice_events = [
            e for e in aggregate.audit if e.event_type is AuditEventType.RECOVERY_ADVICE_GENERATED
        ]
        assert len(aggregate.plans) == 1 and len(advice_events) == 1

        body = {"plan_id": plan["plan_id"], "plan_hash": plan["plan_hash"]}
        approvals = await asyncio.gather(
            *(c.post("/api/demo/approve", json=body) for _ in range(2))
        )
        assert [r.status_code for r in approvals] == [200, 200]

        executions = await asyncio.gather(
            *(c.post("/api/demo/execute", json={"plan_id": plan["plan_id"]}) for _ in range(3))
        )
        assert [r.status_code for r in executions] == [200, 200, 200]
        assert len(session.world.gateway.requests) == 1
        assert aggregate.journal.count_effects(OperationType.RECIPIENT_CREDIT) == 1
        assert executions[-1].json()["state"] == "RECONCILED"


async def test_wrong_hash_and_premature_execution_are_refused(client):
    plan = (await _analyse(client))["analysis"]["plan"]
    early = await client.post("/api/demo/execute", json={"plan_id": plan["plan_id"]})
    assert early.status_code == 409 and early.json()["error"] == "ApprovalRequiredError"
    assert early.json()["view"]["payout_calls"] == 0
    wrong = await client.post(
        "/api/demo/approve", json={"plan_id": plan["plan_id"], "plan_hash": "0" * 64}
    )
    assert wrong.status_code == 409 and wrong.json()["error"] == "ApprovalMismatchError"
    assert wrong.json()["view"]["state"] == "AWAITING_APPROVAL"
    malformed = await client.post("/api/demo/approve", json={"plan_id": "x", "plan_hash": "y"})
    assert malformed.status_code == 422


async def test_reset_restores_the_seed_and_a_second_run_works():
    session = DemoSession(SETTINGS)
    async with _client(session) as c:
        await _run_to_reconciled(c)
        again = await c.post("/api/demo/analyse")
        assert again.status_code == 409 and "reset" in again.json()["detail"]

        view = (await c.post("/api/demo/reset")).json()
        assert view["state"] == "FAILED" and view["resets"] == 1
        assert view["analysis"] is None and view["reconciliation"] is None
        assert view["diagnosis"]["funds_location"] == "GH_SETTLEMENT_ACCOUNT"
        journal = session.world.repository.get(TRANSACTION_ID).journal
        assert len(journal.effects()) == 3  # the seed only: no duplicated effects
        assert journal.count_effects(OperationType.SENDER_DEBIT) == 1

        second = await _run_to_reconciled(c)
        assert second["state"] == "RECONCILED"
        assert second["reconciliation"]["duplicate_sender_debits"] == 0
        assert second["payout_calls"] == 1


async def test_ai_fallback_is_labelled_and_unused_model_fields_are_hidden():
    def broken(messages, info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=503, model_name="gemini", body=None)

    session = DemoSession(
        SETTINGS, advisor_factory=lambda world: pydantic_advisor(world, FunctionModel(broken))
    )
    async with _client(session) as c:
        advice = (await _analyse(c))["analysis"]["advice"]
        assert advice["orchestrator"] == "deterministic_fallback" and advice["ai_used"] is False
        assert advice["label"] == "Deterministic fallback — AI provider unavailable"
        assert "HTTP 503" in advice["fallback_reason"]
        assert advice["model"] is None and advice["gateway_route"] is None
        # Analyse is idempotent, so the flow reuses the same plan and still reconciles.
        assert (await _run_to_reconciled(c))["state"] == "RECONCILED"


async def test_ai_advice_shows_genuine_model_fields():
    session = DemoSession(
        SETTINGS, advisor_factory=lambda world: pydantic_advisor(world, scripted_model())
    )
    async with _client(session) as c:
        advice = (await _analyse(c))["analysis"]["advice"]
        assert advice["orchestrator"] == "pydantic_ai" and advice["ai_used"] is True
        assert advice["label"] == "Pydantic AI" and advice["model"] and advice["provider_model"]
        assert advice["tool_calls"] == ["inspect_incident", "evaluate_recovery_options"]
        assert advice["recommended_route"] == "MOMO_B"


async def test_modal_fallback_is_reported_truthfully():
    compute = FallbackComputeBackend(DownModal(), LocalRouteComputeBackend())
    session = DemoSession(SETTINGS, world_factory=lambda: build_demo_world(compute=compute))
    async with _client(session) as c:
        summary = (await _analyse(c))["analysis"]["compute"]
        assert summary["configured_backend"] == "modal"
        assert (summary["backend"], summary["fallback_from"]) == ("local", "modal")
        assert "modal unreachable" in summary["fallback_reason"]


async def test_no_recipient_pii_in_any_response():
    async with _client() as c:
        bodies = [(await c.get("/api/demo/status")).text, (await c.get("/api/health")).text]
        bodies.append(json.dumps(await _run_to_reconciled(c)))
        bodies.append((await c.get("/api/demo/transaction")).text)
    dumped = " ".join(bodies).lower()
    for value in (
        RECIPIENT_NAME,
        RECIPIENT_PHONE,
        RECIPIENT_PHONE.replace(" ", ""),
        RECIPIENT_REFERENCE,
    ):
        assert value.lower() not in dumped


async def test_built_frontend_is_served_next_to_the_api(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>SikaRescue</title>")
    app = create_app(SETTINGS, configure_telemetry=False, frontend_dist=tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://d") as c:
        assert "SikaRescue" in (await c.get("/")).text
        assert (await c.get("/api/health")).json()["status"] == "ok"
