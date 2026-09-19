"""RecoveryAdvisor: runs the Pydantic AI agent, or falls back to deterministic advice.

The model advises and orchestrates; the application decides and executes. Whatever happens
here, the plan that can be approved and executed is the deterministic service's plan, taken
from the service, never from model output. Model failure (no provider configured, Gateway
unavailable, timeout, invalid output, budget exceeded) cannot break a recovery: the same
deterministic plan is explained by code instead, and the outcome says so explicitly
(`orchestrator = deterministic_fallback`, with the reason).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field
from pydantic_ai import ModelResponse
from pydantic_ai.exceptions import (
    ModelAPIError,
    ModelHTTPError,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from sikarescue import telemetry
from sikarescue.agent.model import (
    ModelChoice,
    ModelUnavailableError,
    build_model,
    describe_model,
)
from sikarescue.agent.recovery_agent import RecoveryAgentDeps, advice_problems, recovery_agent
from sikarescue.agent.toolbox import ContextMode, RecoveryAgentToolbox
from sikarescue.config import Settings
from sikarescue.errors import PIILeakError, SikaRescueError
from sikarescue.models import (
    Actor,
    DomainModel,
    RecoveryAdvice,
    RecoveryDecisionContext,
    RecoveryPlan,
    RejectedRouteAdvice,
)
from sikarescue.services.recovery import RecoveryService

# Verified live 2026-09-19: Gemini 3.8 Flash on Gateway route `sr` (OpenAI Chat Completions).
DEFAULT_MODEL = "gateway/openai-chat:models/gemini-3.8-flash"
DEFAULT_GATEWAY_ROUTE = "sr"
MAX_OUTPUT_TOKENS = 2048


class Orchestrator(StrEnum):
    PYDANTIC_AI = "pydantic_ai"
    DETERMINISTIC = "deterministic"  # configured: no model requested
    DETERMINISTIC_FALLBACK = "deterministic_fallback"  # a model was requested but not usable


class AdviceRejectedError(Exception):
    """The agent's final advice contradicted the deterministic plan."""


class AgentUsage(DomainModel):
    requests: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class AdvisoryOutcome(DomainModel):
    orchestrator: Orchestrator
    plan: RecoveryPlan  # ALWAYS the deterministic service's plan
    advice: RecoveryAdvice  # display-only
    context: RecoveryDecisionContext
    model: str | None = None
    # The model name the provider itself reported on the run's responses (runtime evidence).
    provider_model: str | None = None
    via_gateway: bool = False
    gateway_route: str | None = None
    fallback_reason: str | None = None
    tool_calls: tuple[str, ...] = ()
    rejected_drafts: int = Field(default=0, ge=0)
    usage: AgentUsage | None = None
    elapsed_seconds: float = Field(ge=0)
    trace_id: str | None = None


@dataclass(frozen=True)
class AdvisorConfig:
    mode: Literal["deterministic", "pydantic"] = "deterministic"
    model_name: str = DEFAULT_MODEL
    gateway_route: str | None = DEFAULT_GATEWAY_ROUTE
    request_timeout_seconds: float = 20.0
    run_timeout_seconds: float = 60.0
    max_model_requests: int = 8
    context_mode: ContextMode = "compact"

    @classmethod
    def from_settings(cls, settings: Settings, **overrides: object) -> AdvisorConfig:
        values: dict[str, object] = {
            "mode": settings.agent_mode,
            "model_name": settings.agent_model,
            "gateway_route": settings.gateway_route,
            "request_timeout_seconds": settings.agent_request_timeout_seconds,
            "run_timeout_seconds": settings.agent_run_timeout_seconds,
            "max_model_requests": settings.agent_max_model_requests,
        }
        return cls(**(values | overrides))  # type: ignore[arg-type]


def describe_failure(exc: BaseException, config: AdvisorConfig) -> str:
    """Short, secret-free reason for the fallback (never includes response bodies)."""
    if isinstance(exc, ModelUnavailableError):
        return f"model unavailable: {exc}"
    if isinstance(exc, TimeoutError):
        return f"agent run exceeded {config.run_timeout_seconds:g}s"
    if isinstance(exc, ModelHTTPError):
        return f"model provider/Gateway returned HTTP {exc.status_code}"
    if isinstance(exc, ModelAPIError):
        return f"model provider/Gateway unavailable ({type(exc).__name__})"
    if isinstance(exc, UsageLimitExceeded):
        return f"agent exceeded its budget ({exc})"[:200]
    if isinstance(exc, UnexpectedModelBehavior):
        return f"model did not produce valid advice ({exc.message})"[:200]
    if isinstance(exc, AdviceRejectedError):
        return f"advice contradicted the deterministic plan ({exc})"[:200]
    if isinstance(exc, PIILeakError):
        return "model boundary refused a payload (PII guard)"
    # Never echo arbitrary exception text: it can carry URLs, headers or response bodies.
    return f"{type(exc).__name__}: model call failed"


_EFFECT_NAMES = {
    "SENDER_DEBIT": "the sender debit",
    "FX_CONVERSION": "the GBP→GHS FX conversion",
    "GH_SETTLEMENT": "the Ghana settlement",
    "RECIPIENT_CREDIT": "the recipient credit",
}
_BACKEND_NAMES = {"modal": "Modal", "local": "local compute"}


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def _joined(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def deterministic_advice(context: RecoveryDecisionContext) -> RecoveryAdvice:
    """The same RecoveryAdvice shape, written by code from the deterministic facts only."""
    plan = context.selected_plan
    obligation = context.outstanding_obligation
    selected = next(r for r in context.candidate_routes if r.rail_id == plan.rail_id)
    effects = [
        _EFFECT_NAMES.get(e.operation.value, e.operation.value) for e in context.completed_effects
    ]
    done = _sentence(_joined(effects) + " succeeded") if effects else "Nothing has settled yet"
    failure = context.failure
    if failure is None:
        failed = "The payout did not complete."
    elif failure.value_moved is False:
        failed = f"The {failure.rail_id} payout failed ({failure.summary}); no value moved."
    else:
        failed = f"The {failure.rail_id} payout outcome is {failure.outcome}."
    if context.sender_debited:
        why = (
            "The sender debit already succeeded and is an immutable effect: restarting from "
            "origin would debit the sender twice. Only the outstanding "
            f"{obligation.amount} payout from {obligation.source} remains."
        )
    else:
        why = "No value-moving effect has completed, so there is nothing to protect yet."
    if selected.stress:
        stress = "; ".join(
            f"{s.scenario.value.lower().replace('_', ' ')} {s.reliability:.1%}"
            for s in selected.stress
        )
        backend = _BACKEND_NAMES.get(context.compute.backend, context.compute.backend)
        stress_summary = (
            f"{plan.rail_id} succeeded in {selected.simulated_reliability:.1%} of simulated "
            f"normal runs ({context.compute.simulated_outcomes:,} synthetic outcomes on "
            f"{backend}). Under stress: {stress}."
        )
    else:
        stress_summary = f"{plan.rail_id} was simulated under normal conditions only."
    return RecoveryAdvice(
        transaction_id=context.transaction_id,
        plan_id=plan.plan_id,
        incident_summary=f"{context.transaction_id}: {done}. {failed} "
        f"Funds are held at {context.funds_location}.",
        funds_location=context.funds_location,
        why_origin_retry_is_unsafe=why,
        recommended_route=plan.rail_id,
        reason_codes=context.reason_codes,
        rejected_routes=tuple(
            RejectedRouteAdvice(
                rail_id=r.rail_id,
                reasons=r.reasons,
                explanation=("; ".join(r.details) or "rejected by hard constraints")[:240],
            )
            for r in context.rejected_routes
        ),
        incremental_fee_gbp=plan.incremental_fee_gbp,
        estimated_arrival_seconds=plan.quoted_arrival_seconds,
        simulated_reliability=plan.simulated_reliability,
        stress_summary=stress_summary,
        operator_message=(
            f"Approve plan {plan.plan_id} to pay {plan.amount} from {obligation.source} via "
            f"{plan.rail_id}. The incremental fee of £{plan.incremental_fee_gbp} is absorbed "
            "by the operator and the sender is not debited again."
        ),
    )


class RecoveryAdvisor:
    def __init__(
        self,
        service: RecoveryService,
        config: AdvisorConfig | None = None,
        *,
        settings: Settings | None = None,
        model: Model | None = None,
        http_client: Any | None = None,
    ) -> None:
        self.service = service
        self.config = config or AdvisorConfig()
        self._settings = settings
        self._model = model  # injected (tests); otherwise resolved from settings
        self._http_client = http_client  # e.g. to record Gateway response headers (demos)

    def model_choice(self) -> ModelChoice | None:
        if self._model is not None:
            return None
        try:
            return describe_model(
                self.config.model_name,
                self.config.gateway_route,
                base_url=self._current_settings().gateway_base_url,
            )
        except ModelUnavailableError:
            return None

    def _current_settings(self) -> Settings:
        if self._settings is None:
            self._settings = Settings()
        return self._settings

    async def advise(self, transaction_id: str) -> AdvisoryOutcome:
        started = time.perf_counter()
        toolbox = RecoveryAgentToolbox(
            self.service, transaction_id, context_mode=self.config.context_mode
        )
        with telemetry.span(
            "recovery_advice", transaction_id=transaction_id, agent_mode=self.config.mode
        ) as span:
            fallback_reason: str | None = None
            deps = RecoveryAgentDeps(toolbox=toolbox)
            if self.config.mode == "pydantic":
                try:
                    outcome = await self._advise_with_agent(deps, started)
                except Exception as exc:
                    planning_error = toolbox.planning_error()
                    if isinstance(planning_error, SikaRescueError):
                        raise planning_error from None  # deterministic, not a model failure
                    fallback_reason = describe_failure(exc, self.config)
                    telemetry.event(
                        "agent_fallback_used",
                        level="warn",
                        transaction_id=transaction_id,
                        reason=fallback_reason,
                    )
                else:
                    await self._record(outcome)
                    span.set(**_span_facts(outcome))
                    return outcome
            outcome = await self._advise_deterministically(
                toolbox, started, fallback_reason, rejected_drafts=len(deps.rejections)
            )
            await self._record(outcome)
            span.set(**_span_facts(outcome))
            return outcome

    async def _advise_with_agent(self, deps: RecoveryAgentDeps, started: float) -> AdvisoryOutcome:
        toolbox = deps.toolbox
        choice: ModelChoice | None = None
        if self._model is not None:
            model = self._model
        else:
            settings = self._current_settings()
            choice = describe_model(
                self.config.model_name,
                self.config.gateway_route,
                base_url=settings.gateway_base_url,
            )
            model = build_model(choice, settings, http_client=self._http_client)
        model_label = choice.name if choice else model.model_name
        telemetry.event(
            "agent_run_started",
            transaction_id=toolbox.transaction_id,
            model=model_label,
            via_gateway=bool(choice and choice.via_gateway),
            gateway_route=choice.route if choice else None,
            run_timeout_seconds=self.config.run_timeout_seconds,
        )
        run = asyncio.wait_for(
            recovery_agent.run(
                f"Transaction {toolbox.transaction_id} has a failed payout. Investigate it "
                "with your tools and return RecoveryAdvice for the human approver.",
                deps=deps,
                model=model,
                model_settings=ModelSettings(
                    timeout=self.config.request_timeout_seconds, max_tokens=MAX_OUTPUT_TOKENS
                ),
                usage_limits=UsageLimits(
                    request_limit=self.config.max_model_requests, tool_calls_limit=12
                ),
            ),
            timeout=self.config.run_timeout_seconds,
        )
        try:
            result = await run
        except UnexpectedModelBehavior as exc:
            if deps.rejections:  # say WHICH facts the model kept getting wrong
                raise AdviceRejectedError("; ".join(deps.rejections[-1])) from exc
            raise
        advice = result.output
        plan = await toolbox.ensure_plan()
        # Defence in depth: re-check against a FRESH packet even though the validator passed.
        context = toolbox.decision_context(plan)
        problems = advice_problems(advice, context)
        if problems:
            raise AdviceRejectedError("; ".join(problems))
        usage = result.usage
        provider_model = next(
            (
                m.model_name
                for m in reversed(result.all_messages())
                if isinstance(m, ModelResponse) and m.model_name
            ),
            None,
        )
        return AdvisoryOutcome(
            orchestrator=Orchestrator.PYDANTIC_AI,
            plan=plan,
            advice=advice,
            context=context,
            model=model_label,
            provider_model=provider_model,
            via_gateway=bool(choice and choice.via_gateway),
            gateway_route=choice.route if choice else None,
            tool_calls=tuple(toolbox.calls),
            rejected_drafts=len(deps.rejections),
            usage=AgentUsage(
                requests=usage.requests,
                tool_calls=usage.tool_calls,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            ),
            elapsed_seconds=round(time.perf_counter() - started, 3),
            trace_id=telemetry.current_trace_id(),
        )

    async def _advise_deterministically(
        self,
        toolbox: RecoveryAgentToolbox,
        started: float,
        fallback_reason: str | None,
        *,
        rejected_drafts: int = 0,
    ) -> AdvisoryOutcome:
        plan = await toolbox.ensure_plan()  # deterministic planning errors propagate
        context = toolbox.decision_context(plan)
        choice = self.model_choice() if fallback_reason else None
        return AdvisoryOutcome(
            orchestrator=(
                Orchestrator.DETERMINISTIC_FALLBACK
                if self.config.mode == "pydantic"
                else Orchestrator.DETERMINISTIC
            ),
            plan=plan,
            advice=deterministic_advice(context),
            context=context,
            model=choice.name if choice else None,
            via_gateway=False,  # no model call succeeded through it
            gateway_route=None,
            fallback_reason=fallback_reason,
            tool_calls=tuple(toolbox.calls),
            rejected_drafts=rejected_drafts,
            elapsed_seconds=round(time.perf_counter() - started, 3),
            trace_id=telemetry.current_trace_id(),
        )

    async def _record(self, outcome: AdvisoryOutcome) -> None:
        await self.service.record_recovery_advice(
            outcome.plan.transaction_id,
            plan_id=outcome.plan.plan_id,
            orchestrator=outcome.orchestrator.value,
            actor=Actor.AGENT
            if outcome.orchestrator is Orchestrator.PYDANTIC_AI
            else Actor.RECOVERY_ENGINE,
            model=outcome.model,
            fallback_reason=outcome.fallback_reason,
        )


def _span_facts(outcome: AdvisoryOutcome) -> dict[str, object]:
    usage = outcome.usage
    return {
        "orchestrator": outcome.orchestrator.value,
        "plan_id": outcome.plan.plan_id,
        "selected_route": outcome.plan.rail_id.value,
        "model": outcome.model,
        "via_gateway": outcome.via_gateway,
        "fallback_reason": outcome.fallback_reason,
        "agent_tool_calls": ",".join(outcome.tool_calls) or None,
        "rejected_drafts": outcome.rejected_drafts,
        "input_tokens": usage.input_tokens if usage else None,
        "output_tokens": usage.output_tokens if usage else None,
        "model_requests": usage.requests if usage else None,
    }
