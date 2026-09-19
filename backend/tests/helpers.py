"""Shared test helpers (plain module; fixtures live in conftest.py)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from pydantic_ai import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from sikarescue.agent.advisor import AdvisorConfig, RecoveryAdvisor, deterministic_advice
from sikarescue.demo_data.sk10421 import TRANSACTION_ID, DemoWorld
from sikarescue.models import RecoveryDecisionContext, RecoveryPlan

INVESTIGATE = ("inspect_incident", "evaluate_recovery_options")
ALL_TOOLS = (*INVESTIGATE, "inspect_recovery_plan", "get_recovery_audit_summary")


async def plan_and_approve(world: DemoWorld, approver: str = "ops.demo") -> RecoveryPlan:
    plan = await world.service.create_recovery_plan(world.transaction_id)
    await world.service.approve_recovery(plan.plan_id, plan_hash=plan.plan_hash, approver=approver)
    return plan


def _tool_returns(messages: list[ModelMessage]) -> dict[str, ToolReturnPart]:
    return {
        part.tool_name: part
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    }


def retry_count(messages: list[ModelMessage]) -> int:
    return sum(
        isinstance(part, RetryPromptPart)
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
    )


def scripted_model(
    *,
    tools: Sequence[str] = INVESTIGATE,
    transaction_id: str = TRANSACTION_ID,
    tamper: Callable[[dict[str, Any], int], dict[str, Any]] | None = None,
    seen: list[list[ModelMessage]] | None = None,
    first: Callable[[], ModelResponse] | None = None,
) -> FunctionModel:
    """A deterministic stand-in for an LLM that behaves like a well-mannered agent.

    1. calls `tools`; 2. copies the returned decision packet into RecoveryAdvice.
    `tamper(advice, retries_so_far)` can corrupt the advice; `first` overrides turn one.
    """

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if seen is not None:
            seen.append(list(messages))
        returns = _tool_returns(messages)
        if first is not None and len(messages) == 1:
            return first()
        if "evaluate_recovery_options" not in returns:
            return ModelResponse(
                parts=[ToolCallPart(name, {"transaction_id": transaction_id}) for name in tools]
            )
        packet = RecoveryDecisionContext.model_validate(
            returns["evaluate_recovery_options"].model_response_object()
        )
        advice = deterministic_advice(packet).model_dump(mode="json")
        if tamper is not None:
            advice = tamper(advice, retry_count(messages))
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, advice)])

    return FunctionModel(respond)


def pydantic_advisor(world: DemoWorld, model: FunctionModel, **config: Any) -> RecoveryAdvisor:
    return RecoveryAdvisor(world.service, AdvisorConfig(mode="pydantic", **config), model=model)


# ------------------------------------------------------------------ evidence extraction

# What a well-behaved extraction model returns for each synthetic MOMO_A incident.
DEFINITIVE_EXTRACTION: dict[str, Any] = {
    "provider": "MOMO_A",
    "provider_code": "MA-4017",
    "provider_message": "Beneficiary wallet not provisioned for inbound transfers.",
    "transport_outcome": "RESPONSE_RECEIVED",
    "acceptance_stage": "PRE_ACCEPTANCE",
    "explicit_rejection": True,
    "provider_reference": "mareq_5d02b7c1",
    "evidence_fragments": [
        '"disposition": "NOT_ACCEPTED"',
        '"code": "MA-4017"',
        "Instruction rejected during validation; it was not queued and no funds were "
        "reserved or moved.",
    ],
    "completeness": "COMPLETE",
}
UNKNOWN_EXTRACTION: dict[str, Any] = {
    "provider": "MOMO_A",
    "provider_code": None,
    "provider_message": None,
    "transport_outcome": "TIMEOUT",
    "acceptance_stage": "UNKNOWN",
    "explicit_rejection": None,
    "provider_reference": None,
    "evidence_fragments": [
        "no response bytes received before read timeout (30000 ms)",
        "Your request may or may not have been processed.",
    ],
    "completeness": "PARTIAL",
}


def scripted_extraction(
    *drafts: dict[str, Any], seen: list[list[ModelMessage]] | None = None
) -> FunctionModel:
    """A stand-in extraction model: returns `drafts` in order (the last one repeats)."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if seen is not None:
            seen.append(list(messages))
        draft = drafts[min(retry_count(messages), len(drafts) - 1)]
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, draft)])

    return FunctionModel(respond)


def extraction_for(world: DemoWorld) -> dict[str, Any]:
    """The honest extraction for whichever incident this world was seeded with."""
    incident = world.repository.get(world.transaction_id).incident
    assert incident is not None
    return DEFINITIVE_EXTRACTION if incident.response_received else UNKNOWN_EXTRACTION
