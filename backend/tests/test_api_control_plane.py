"""Web flow for provider evidence, funds position, frontier, preview, counterfactual and the
systemic outage panel. In-process ASGI; the extraction model is scripted (no network)."""

from __future__ import annotations

import httpx

from sikarescue.agent.advisor import AdvisorConfig
from sikarescue.agent.evidence import EvidenceExtractor
from sikarescue.api.app import create_app
from sikarescue.api.session import DemoSession
from sikarescue.config import Settings
from sikarescue.models import RailId

from helpers import extraction_for, scripted_extraction

SETTINGS = Settings(
    _env_file=None, compute_backend="local", agent_mode="deterministic", payout_latency_seconds=0
)


def _scripted(world):
    return EvidenceExtractor(
        AdvisorConfig(mode="pydantic"), model=scripted_extraction(extraction_for(world))
    )


def _client(session: DemoSession) -> httpx.AsyncClient:
    app = create_app(SETTINGS, session=session, configure_telemetry=False)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://demo")


async def _post(c: httpx.AsyncClient, path: str, body=None) -> dict:
    response = await c.post(path, json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def test_definitive_failure_flow_through_the_api():
    session = DemoSession(SETTINGS, extractor_factory=_scripted)
    async with _client(session) as c:
        view = await _post(c, "/api/demo/classify")
        incident = view["incident"]
        assert incident["http_status"] == 200 and '"status": "COMPLETED"' in incident["raw_payload"]
        assert incident["extraction"]["ai_used"] is True
        assert incident["extraction"]["evidence"]["provider_code"] == "MA-4017"
        verdict = incident["verdict"]
        assert verdict["classification"] == verdict["recorded_outcome"] == "DEFINITIVE_FAILED"
        assert all(c["passed"] for c in verdict["checks"] + verdict["requirements"])
        assert view["next_action"] == "analyse" and view["state"] == "DIAGNOSING"
        position = view["funds_position"]
        assert (position["position_status"], position["label"]) == ("AVAILABLE", "Funds are here")
        assert position["amount"] == "GHS 1,830.00"
        frontier = view["frontier"]
        assert frontier["basis"] == "CANDIDATES"
        assert (frontier["candidates"], frontier["rejected_before_simulation"]) == (4, 2)

        view = await _post(c, "/api/demo/analyse")
        frontier = view["frontier"]
        assert frontier["basis"] == "PLAN"
        assert (
            frontier["candidates"],
            frontier["rejected_before_simulation"],
            frontier["eligible"],
            frontier["selected"],
        ) == (4, 2, 2, 1)
        preview = view["preview"]
        assert preview["valid"] and preview["proposed_rail"] == "MoMo B"
        rows = {r["label"]: r for r in preview["rows"]}
        assert (rows["Recipient credits"]["current"], rows["Recipient credits"]["proposed"]) == (
            "0",
            "1",
        )
        assert rows["Sender debits"]["proposed"] == "1"
        counterfactual = view["counterfactual"]
        assert counterfactual["naive_repeated_effects"] == 3
        assert counterfactual["naive_extra_sender_debit"] == "£120.00"
        assert len(counterfactual["sikarescue_steps"]) == 1

        plan = view["analysis"]["plan"]
        await _post(
            c, "/api/demo/approve", {"plan_id": plan["plan_id"], "plan_hash": plan["plan_hash"]}
        )
        done = await _post(c, "/api/demo/execute", {"plan_id": plan["plan_id"]})
        proof = done["reconciliation"]
        assert done["state"] == "RECONCILED"
        assert (
            proof["sender_debit_count"],
            proof["recipient_credit_count"],
            proof["duplicate_sender_debits"],
        ) == (1, 1, 0)
        assert done["funds_position"]["position_status"] == "FINAL"
        assert done["frontier"]["basis"] == "SETTLED" and done["preview"] is None
        payout = next(n for n in done["effect_graph"] if n["kind"] == "RECIPIENT_PAYOUT")
        assert payout["status"] == "COMPLETE" and payout["rail_name"] == "MoMo B"


async def test_unknown_scenario_requires_manual_review_and_offers_no_payout():
    session = DemoSession(SETTINGS, extractor_factory=_scripted)
    async with _client(session) as c:
        first = (await c.get("/api/demo/status")).json()["transaction"]["payment_instance_id"]
        view = await _post(c, "/api/demo/reset", {"scenario": "unknown"})
        assert view["scenario"]["current"] == "unknown"
        assert view["transaction"]["payment_instance_id"] != first  # a new payment, not a mutation
        assert view["incident"]["response_received"] is False

        view = await _post(c, "/api/demo/classify")
        assert view["incident"]["verdict"]["classification"] == "UNKNOWN"
        assert view["state"] == "MANUAL_REVIEW" and view["next_action"] == "manual_review"
        position = view["funds_position"]
        assert position["last_confirmed_location"] == "GH_SETTLEMENT_ACCOUNT"
        assert position["position_status"] == "UNCERTAIN" and position["certainty"] == "UNCERTAIN"
        assert position["available_for_automatic_action"] is False
        assert position["label"] == "Last confirmed here"
        frontier = view["frontier"]
        assert frontier["payout_actions_permitted"] is False
        assert {a["kind"] for a in frontier["actions"]} == {
            "QUERY_PROVIDER",
            "WAIT_FOR_PROVIDER_EVIDENCE",
            "MANUAL_REVIEW",
        }
        assert not any(a["moves_value"] for a in frontier["actions"])
        assert view["preview"] is None and view["analysis"] is None
        assert view["counterfactual"]["duplicate_recipient_credit_risk"] is True

        refused = await c.post("/api/demo/analyse")
        assert refused.status_code == 409
        assert session.gateway.requests == []  # nothing was ever dispatched

        after = await _post(c, "/api/demo/reset")  # an UNKNOWN outcome is never erased
        assert view["transaction"]["payment_instance_id"] in after["retired_instance_ids"]


async def test_without_ai_the_opaque_payload_fails_closed_to_manual_review():
    session = DemoSession(SETTINGS)  # deterministic: no model, envelope-only reader
    async with _client(session) as c:
        view = await _post(c, "/api/demo/classify")
        extraction = view["incident"]["extraction"]
        assert extraction["ai_used"] is False and extraction["orchestrator"] == "deterministic"
        assert extraction["evidence"]["extracted_by"] == "deterministic_envelope"
        assert view["incident"]["verdict"]["classification"] == "UNKNOWN"
        assert view["state"] == "MANUAL_REVIEW"
        assert session.gateway.requests == []


async def test_analyse_before_classification_is_refused_without_escalation():
    session = DemoSession(SETTINGS, extractor_factory=_scripted)
    async with _client(session) as c:
        refused = await c.post("/api/demo/analyse")
        assert refused.status_code == 409 and refused.json()["error"] == "EvidencePendingError"
        assert refused.json()["view"]["state"] == "FAILED"
        assert refused.json()["view"]["next_action"] == "classify"


async def test_stale_plan_invalidates_the_preview_in_the_view():
    session = DemoSession(SETTINGS, extractor_factory=_scripted)
    async with _client(session) as c:
        await _post(c, "/api/demo/classify")
        plan = (await _post(c, "/api/demo/analyse"))["analysis"]["plan"]
        await _post(
            c, "/api/demo/approve", {"plan_id": plan["plan_id"], "plan_hash": plan["plan_hash"]}
        )
        registry = session.world.registry
        quote = registry.quote(RailId.MOMO_B)
        registry.replace_quote(quote.model_copy(update={"quote_id": "qte_0000000000dd"}))
        preview = (await c.get("/api/demo/status")).json()["preview"]
        assert preview["valid"] is False and preview["rows"] == []
        assert any("quote changed" in r for r in preview["invalidation_reasons"])


async def test_systemic_outage_panel_runs_and_is_retained():
    session = DemoSession(SETTINGS, extractor_factory=_scripted)
    async with _client(session) as c:
        assert (await c.get("/api/outage")).json() is None
        view = (await c.post("/api/outage/run")).json()
        assert view["backend"] == "local" and view["parallel_jobs"] == 5
        assert view["portfolio"]["size"] == 40_000
        nominal = view["scenarios"][0]
        assert nominal["scenario_id"] == "NOMINAL"
        assert {a["rail_name"] for a in nominal["allocations"]} == {"MoMo B", "Bank → MoMo bridge"}
        token = next(r for r in nominal["rails"] if r["rail_id"] == "TOKEN_BRIDGE")
        assert token["eligible"] is False and "POLICY_DENIED" in token["rejection_reasons"]
        assert (await c.get("/api/outage")).json()["scenarios"][0] == nominal
        # The payment demo is untouched by the fleet analysis.
        assert (await c.get("/api/demo/status")).json()["next_action"] == "classify"
