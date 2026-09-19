"""RecoveryAgent + RecoveryAdvisor: the model advises; the deterministic core decides.

No network: models are Pydantic AI `FunctionModel`s scripted in-process, and
`ALLOW_MODEL_REQUESTS = False` (conftest) makes any real model request fail loudly.
"""

from __future__ import annotations

import asyncio
import io

import pytest
from pydantic import ValidationError
from pydantic_ai import ModelMessagesTypeAdapter, ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.function import AgentInfo, FunctionModel

from sikarescue.agent.advisor import (
    AdvisorConfig,
    Orchestrator,
    RecoveryAdvisor,
    deterministic_advice,
)
from sikarescue.agent.model import (
    ModelUnavailableError,
    build_model,
    describe_model,
    gateway_endpoint,
    missing_configuration,
)
from sikarescue.agent.recovery_agent import AGENT_TOOL_NAMES, advice_problems, recovery_agent
from sikarescue.cli.demo import run_demo
from sikarescue.compute.backend import LocalRouteComputeBackend
from sikarescue.config import Settings
from sikarescue.demo_data.sk10421 import (
    RECIPIENT_NAME,
    RECIPIENT_PHONE,
    RECIPIENT_REFERENCE,
    build_demo_world,
)
from sikarescue.errors import ManualReviewRequiredError
from sikarescue.models import (
    Actor,
    AttemptOutcome,
    AuditEventType,
    ModelAuditSummary,
    ModelPlanView,
    ModelTransactionView,
    OperationType,
    PlanStatus,
    RailId,
    RecoveryAdvice,
    RecoveryDecisionContext,
    RecoveryState,
)
from sikarescue.services import model_boundary

from helpers import ALL_TOOLS, INVESTIGATE, pydantic_advisor, scripted_model

PII_VARIANTS = (
    RECIPIENT_NAME,
    RECIPIENT_PHONE,
    RECIPIENT_PHONE.replace(" ", ""),
    RECIPIENT_REFERENCE,
)
ALLOWLISTED_TOOL_RESULTS = (
    ModelTransactionView,
    RecoveryDecisionContext,
    ModelPlanView,
    ModelAuditSummary,
)


def _assert_pii_free(text: str) -> None:
    lowered = text.lower()
    for value in PII_VARIANTS:
        assert value.lower() not in lowered, "recipient PII reached the model"


async def _advise(world, model, **config):
    return await pydantic_advisor(world, model, **config).advise("SK-10421")


# ============================================================== tools and boundaries


def test_agent_exposes_only_read_and_analysis_tools():
    tools = set(recovery_agent._function_toolset.tools)
    assert tools == set(AGENT_TOOL_NAMES)
    forbidden = ("approve", "execute", "reconcile", "credit", "debit", "journal", "policy")
    assert not [t for t in tools if any(word in t for word in forbidden)]


async def test_agent_path_returns_validated_advice_on_the_deterministic_plan(world, txn_id):
    outcome = await _advise(world, scripted_model())
    aggregate = world.repository.get(txn_id)

    assert outcome.orchestrator is Orchestrator.PYDANTIC_AI
    assert outcome.fallback_reason is None
    # Same immutable plan the service holds; exactly one plan was created.
    assert outcome.plan == aggregate.current_plan and len(aggregate.plans) == 1
    assert outcome.advice.plan_id == outcome.plan.plan_id
    assert outcome.advice.recommended_route is outcome.plan.rail_id is RailId.MOMO_B
    assert advice_problems(outcome.advice, outcome.context) == []
    assert outcome.tool_calls == INVESTIGATE
    assert outcome.usage is not None and outcome.usage.requests == 2
    # Planning needs a human: nothing is approved or executed by the agent path.
    assert aggregate.state is RecoveryState.AWAITING_APPROVAL
    assert aggregate.plan_status[outcome.plan.plan_id] is PlanStatus.PENDING_APPROVAL
    recorded = [
        e for e in aggregate.audit if e.event_type is AuditEventType.RECOVERY_ADVICE_GENERATED
    ]
    assert len(recorded) == 1 and recorded[0].actor is Actor.AGENT
    assert recorded[0].data["orchestrator"] == "pydantic_ai"


async def test_model_sees_only_allowlisted_views_and_never_pii(world):
    seen: list = []
    outcome = await _advise(world, scripted_model(tools=ALL_TOOLS, seen=seen))
    assert outcome.orchestrator is Orchestrator.PYDANTIC_AI
    assert outcome.tool_calls == ALL_TOOLS

    returns = [
        part
        for messages in seen
        for message in messages
        for part in getattr(message, "parts", ())
        if isinstance(part, ToolReturnPart)
    ]
    assert {r.tool_name for r in returns} == set(ALL_TOOLS)
    assert all(isinstance(r.content, ALLOWLISTED_TOOL_RESULTS) for r in returns)
    # Everything the model received, serialised exactly as a provider request would carry it.
    for messages in seen:
        _assert_pii_free(ModelMessagesTypeAdapter.dump_json(messages).decode())


async def test_pii_injected_into_a_tool_result_is_stopped_at_the_boundary(world, monkeypatch):
    real = model_boundary.build_model_audit_summary

    def leaky(aggregate, limit=40):
        summary = real(aggregate, limit)
        entry = summary.entries[0].model_copy(update={"summary": f"call {RECIPIENT_NAME}"})
        return summary.model_copy(update={"entries": (entry, *summary.entries[1:])})

    monkeypatch.setattr("sikarescue.agent.toolbox.build_model_audit_summary", leaky)
    seen: list = []
    outcome = await _advise(world, scripted_model(tools=ALL_TOOLS, seen=seen))

    assert outcome.orchestrator is Orchestrator.DETERMINISTIC_FALLBACK
    assert outcome.fallback_reason == "model boundary refused a payload (PII guard)"
    for messages in seen:
        _assert_pii_free(ModelMessagesTypeAdapter.dump_json(messages).decode())


async def test_agent_cannot_move_money_or_mutate_the_ledger(world, txn_id):
    aggregate = world.repository.get(txn_id)
    entries_before = aggregate.journal.entries

    await _advise(world, scripted_model(tools=ALL_TOOLS))

    assert aggregate.journal.entries == entries_before  # no journal writes at all
    assert not aggregate.journal.has_effect(OperationType.RECIPIENT_CREDIT)
    assert aggregate.journal.count_effects(OperationType.SENDER_DEBIT) == 1
    assert world.gateway.requests == [] and aggregate.executions == {}
    assert aggregate.approvals == {}


async def test_a_model_asking_to_execute_or_approve_gets_no_such_tool(world, txn_id):
    def try_to_execute() -> ModelResponse:
        return ModelResponse(
            parts=[
                ToolCallPart("execute_recovery", {"plan_id": "plan_000000000000"}),
                ToolCallPart("approve_recovery", {"plan_id": "plan_000000000000"}),
            ]
        )

    seen: list = []
    outcome = await _advise(world, scripted_model(first=try_to_execute, seen=seen))
    aggregate = world.repository.get(txn_id)

    retry_text = ModelMessagesTypeAdapter.dump_json(seen[1]).decode()
    assert "Unknown tool name" in retry_text or "unknown tool" in retry_text.lower()
    assert world.gateway.requests == [] and aggregate.executions == {}
    assert aggregate.state is RecoveryState.AWAITING_APPROVAL
    assert outcome.plan.plan_id == aggregate.current_plan_id


async def test_tools_are_scoped_to_the_bound_transaction(world):
    seen: list = []
    outcome = await _advise(world, scripted_model(transaction_id="SK-99999", seen=seen))
    retry_text = ModelMessagesTypeAdapter.dump_json(seen[1]).decode()
    assert "scoped to transaction SK-10421" in retry_text
    # The model never obtained data for another transaction; the run falls back safely.
    assert outcome.orchestrator is Orchestrator.DETERMINISTIC_FALLBACK


# ============================================================== structured output


def test_recovery_advice_schema(world):
    async def packet():
        plan = await world.service.create_recovery_plan("SK-10421")
        return model_boundary.build_decision_context(world.repository.get("SK-10421"), plan)

    advice = deterministic_advice(asyncio.run(packet()))
    data = advice.model_dump(mode="json")
    assert RecoveryAdvice.model_validate(data) == advice
    for bad in (
        {"approval_required": False},
        {"incremental_fee_gbp": "£0.18"},
        {"recommended_route": "WIRE_TRANSFER"},
        {"reason_codes": []},
        {"recipient_name": RECIPIENT_NAME},  # extra fields are forbidden
    ):
        with pytest.raises(ValidationError):
            RecoveryAdvice.model_validate(data | bad)


async def test_malformed_output_is_corrected_via_model_retry(world):
    def wrong_fee_once(advice, retries):
        return advice | {"incremental_fee_gbp": "0.01"} if retries == 0 else advice

    outcome = await _advise(world, scripted_model(tamper=wrong_fee_once))
    assert outcome.orchestrator is Orchestrator.PYDANTIC_AI
    assert outcome.rejected_drafts == 1
    assert outcome.advice.incremental_fee_gbp == "0.18"


async def test_structurally_invalid_output_falls_back_safely(world, txn_id):
    outcome = await _advise(world, scripted_model(tamper=lambda advice, _: {"route": "MOMO_B"}))
    assert outcome.orchestrator is Orchestrator.DETERMINISTIC_FALLBACK
    assert "did not produce valid advice" in outcome.fallback_reason
    assert outcome.plan == world.repository.get(txn_id).current_plan
    assert advice_problems(outcome.advice, outcome.context) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [("incremental_fee_gbp", "0.01"), ("recommended_route", "BANK_MOMO_BRIDGE")],
    ids=["fee", "route"],
)
async def test_model_output_cannot_change_fee_or_executable_route(world, txn_id, field, value):
    outcome = await _advise(world, scripted_model(tamper=lambda advice, _: advice | {field: value}))
    assert outcome.orchestrator is Orchestrator.DETERMINISTIC_FALLBACK
    assert outcome.fallback_reason.startswith("advice contradicted the deterministic plan")
    assert field in outcome.fallback_reason and outcome.rejected_drafts == 3

    plan = outcome.plan
    assert plan.rail_id is RailId.MOMO_B and f"{plan.incremental_fee.amount}" == "0.18"
    request = await world.service.request_recovery_approval(plan.plan_id)
    assert "via MOMO_B" in request.summary and "0.18 GBP" in request.summary
    await world.service.approve_recovery(plan.plan_id, plan_hash=plan.plan_hash, approver="ops.x")
    execution = await world.service.execute_recovery(plan.plan_id)
    assert execution.rail_id is RailId.MOMO_B
    assert [r.rail_id for r in world.gateway.requests] == [RailId.MOMO_B]
    result = await world.service.reconcile_transaction(txn_id)
    assert result.reconciled


# ============================================================== model failure -> fallback


async def test_model_timeout_falls_back_to_the_deterministic_flow(world, txn_id):
    async def hangs(messages, info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    outcome = await _advise(world, FunctionModel(hangs), run_timeout_seconds=0.2)
    assert outcome.orchestrator is Orchestrator.DETERMINISTIC_FALLBACK
    assert outcome.fallback_reason == "agent run exceeded 0.2s"
    assert outcome.plan.rail_id is RailId.MOMO_B
    assert world.repository.get(txn_id).state is RecoveryState.AWAITING_APPROVAL


class SlowLocalBackend(LocalRouteComputeBackend):
    async def evaluate(self, request):
        await asyncio.sleep(0.4)
        return await super().evaluate(request)


async def test_timeout_during_analysis_reuses_the_same_deterministic_planning(txn_id):
    world = build_demo_world(compute=SlowLocalBackend())
    outcome = await _advise(world, scripted_model(), run_timeout_seconds=0.15)
    aggregate = world.repository.get(txn_id)

    assert outcome.orchestrator is Orchestrator.DETERMINISTIC_FALLBACK
    assert outcome.tool_calls == INVESTIGATE  # the agent had requested the analysis
    # The cancelled agent did not cancel planning; the fallback joined it: ONE compute run.
    assert len(aggregate.plans) == 1
    evaluated = [e for e in aggregate.audit if e.event_type is AuditEventType.ROUTES_EVALUATED]
    assert len(evaluated) == 1


async def test_gateway_or_provider_error_falls_back(world):
    def gateway_down(messages, info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=502, model_name="claude-haiku-4-5", body="bad gateway")

    outcome = await _advise(world, FunctionModel(gateway_down))
    assert outcome.orchestrator is Orchestrator.DETERMINISTIC_FALLBACK
    assert outcome.fallback_reason == "model provider/Gateway returned HTTP 502"


async def test_missing_gateway_key_falls_back_and_names_the_variable(world, monkeypatch):
    for name in ("PYDANTIC_AI_GATEWAY_API_KEY", "PAIG_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    advisor = RecoveryAdvisor(
        world.service,
        AdvisorConfig(mode="pydantic", model_name="gateway/anthropic:claude-haiku-4-5"),
        settings=Settings(_env_file=None),
    )
    outcome = await advisor.advise("SK-10421")
    assert outcome.orchestrator is Orchestrator.DETERMINISTIC_FALLBACK
    assert "PYDANTIC_AI_GATEWAY_API_KEY" in outcome.fallback_reason
    assert outcome.model == "gateway/anthropic:claude-haiku-4-5" and not outcome.via_gateway


def test_gateway_models_are_wired_offline_from_environment_config():
    settings = Settings(_env_file=None, PYDANTIC_AI_GATEWAY_API_KEY="pylf_v1_eu_" + "k" * 24)
    choice = describe_model("gateway/openai:gpt-5-mini", route="sikarescue-openai")
    model = build_model(choice, settings)
    assert choice.via_gateway and choice.upstream == "openai"
    assert model.base_url == "https://gateway-eu.pydantic.dev/proxy/sikarescue-openai/"
    assert missing_configuration(choice, settings) == []
    assert missing_configuration(choice, Settings(_env_file=None)) != []


CONNECT_MODEL = "gateway/google-gemini-hackathon:models/gemini-3.8-flash"  # SDK cannot parse
LIVE_MODEL = "gateway/openai-chat:models/gemini-3.8-flash"


def test_default_model_is_the_verified_live_gateway_path_without_google_key(monkeypatch):
    for name in ("SIKARESCUE_AGENT_MODEL", "SIKARESCUE_GATEWAY_ROUTE", "GOOGLE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None, PYDANTIC_AI_GATEWAY_API_KEY="pylf_v2_eu_" + "k" * 24)
    assert settings.agent_model == LIVE_MODEL and settings.gateway_route == "sr"
    choice = describe_model(settings.agent_model, settings.gateway_route)
    assert choice.via_gateway and choice.route == "sr"
    model = build_model(choice, settings)
    assert model.base_url == "https://gateway-eu.pydantic.dev/proxy/sr/"
    # The Gateway path needs only the Gateway key: the Google key lives in the BYOK provider.
    assert missing_configuration(choice, settings) == []


async def test_a_model_string_the_sdk_cannot_resolve_falls_back_without_breaking(world, txn_id):
    advisor = RecoveryAdvisor(
        world.service,
        AdvisorConfig(mode="pydantic", model_name=CONNECT_MODEL),
        settings=Settings(_env_file=None, PYDANTIC_AI_GATEWAY_API_KEY="pylf_v2_eu_" + "k" * 24),
    )
    outcome = await advisor.advise(txn_id)
    assert outcome.orchestrator is Orchestrator.DETERMINISTIC_FALLBACK
    assert outcome.fallback_reason and outcome.plan.rail_id is RailId.MOMO_B
    assert world.repository.get(txn_id).state is RecoveryState.AWAITING_APPROVAL


@pytest.mark.parametrize(
    ("base_url", "route", "expected"),
    [
        (None, "sikarescue-gemini", (None, "sikarescue-gemini")),
        (
            "https://gateway-eu.pydantic.dev/proxy",
            "sikarescue-gemini",
            ("https://gateway-eu.pydantic.dev/proxy", "sikarescue-gemini"),
        ),
        # The Connect tab's full endpoint URL: the route is taken from it, never appended twice.
        (
            "https://gateway-eu.pydantic.dev/proxy/sikarescue-gemini",
            None,
            ("https://gateway-eu.pydantic.dev/proxy", "sikarescue-gemini"),
        ),
        (
            "https://gateway-eu.pydantic.dev/proxy/sikarescue-gemini/",
            "sikarescue-gemini",
            ("https://gateway-eu.pydantic.dev/proxy", "sikarescue-gemini"),
        ),
    ],
    ids=["no-base-url", "proxy-root", "connect-url", "connect-url-same-route"],
)
def test_gateway_base_url_forms_resolve_to_one_route(base_url, route, expected):
    assert gateway_endpoint(base_url, route) == expected


def test_full_gateway_url_is_used_without_doubling_the_route():
    settings = Settings(
        _env_file=None,
        PYDANTIC_AI_GATEWAY_API_KEY="pylf_v2_eu_" + "k" * 24,
        PYDANTIC_AI_GATEWAY_BASE_URL="https://gateway-eu.pydantic.dev/proxy/sikarescue-gemini",
    )
    choice = describe_model(
        "gateway/openai-chat:gemini-3.8-flash", None, base_url=settings.gateway_base_url
    )
    assert choice.route == "sikarescue-gemini"
    model = build_model(choice, settings)
    assert model.base_url == "https://gateway-eu.pydantic.dev/proxy/sikarescue-gemini/"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://gateway-eu.pydantic.dev/proxy/another-route",
        "https://gateway-eu.pydantic.dev/proxy/sikarescue-gemini/chat",
    ],
    ids=["conflicting-route", "nested-path"],
)
def test_ambiguous_gateway_base_url_is_refused(base_url):
    with pytest.raises(ModelUnavailableError):
        gateway_endpoint(base_url, "sikarescue-gemini")


async def test_deterministic_planning_errors_are_not_masked_as_model_failures():
    world = build_demo_world(momo_a_outcome=AttemptOutcome.UNKNOWN)
    with pytest.raises(ManualReviewRequiredError):
        await _advise(world, scripted_model())


# ============================================================== end to end via the CLI


async def test_cli_agent_path_reconciles_and_narrates_roles(world, txn_id):
    out = io.StringIO()
    outcome = await run_demo(
        world,
        auto_approve=True,
        out=out,
        show_timeline=False,
        advisor=pydantic_advisor(world, scripted_model()),
    )
    text = out.getvalue()
    assert outcome.exit_code == 0 and outcome.advisory.orchestrator is Orchestrator.PYDANTIC_AI
    assert world.repository.get(txn_id).state is RecoveryState.RECONCILED
    for expected in (
        "5. Recovery advice (Pydantic AI · checked against the plan)",
        "Agent steps  : inspect_incident → evaluate_recovery_options",
        "Pydantic AI        : inspected the incident · requested route evaluation",
        "orchestrator             : pydantic_ai",
        "state                    : RECONCILED",
        "duplicate sender debits  : 0",
    ):
        assert expected in text, expected


async def test_cli_model_failure_still_reconciles_with_fallback(world, txn_id):
    def broken(messages, info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=503, model_name="m", body=None)

    out = io.StringIO()
    outcome = await run_demo(
        world,
        auto_approve=True,
        out=out,
        show_timeline=False,
        advisor=pydantic_advisor(world, FunctionModel(broken)),
    )
    text = out.getvalue()
    assert outcome.exit_code == 0
    assert world.repository.get(txn_id).state is RecoveryState.RECONCILED
    assert "orchestrator             : deterministic_fallback" in text
    assert "Pydantic AI advice unavailable: model provider/Gateway returned HTTP 503" in text
    assert [r.rail_id for r in world.gateway.requests] == [RailId.MOMO_B]


async def test_deterministic_mode_reconciles_without_any_model(world, txn_id):
    out = io.StringIO()
    outcome = await run_demo(world, auto_approve=True, out=out, show_timeline=False)
    assert outcome.exit_code == 0
    assert outcome.advisory.orchestrator is Orchestrator.DETERMINISTIC
    assert outcome.advisory.usage is None
    assert world.repository.get(txn_id).state is RecoveryState.RECONCILED
    assert "orchestrator             : deterministic" in out.getvalue()
