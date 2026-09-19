"""EvidenceExtractor: Pydantic AI's one bounded job in the incident pipeline.

    raw synthetic provider payload
      -> Pydantic AI structured extraction (this module)   -> FailureEvidence
      -> deterministic FailureVerifier (services.evidence)
      -> SUCCEEDED / DEFINITIVE_FAILED / UNKNOWN

The agent has no tools and can only return `ExtractedFailureEvidence`: structure plus verbatim
citations. It cannot classify, authorise, rank or execute anything, and its output is bound
to the payload by a digest computed here, by code. Citations that are not verbatim are sent
back for correction (`ModelRetry`); the verifier re-checks everything independently.

If no model is configured, or the model fails in any way, the fallback reads only our own
transport observations. It never interprets the provider's body, so the verifier can never
prove a definitive failure from it: a missing model fails CLOSED (UNKNOWN), never open.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import Field
from pydantic_ai import Agent, ModelResponse, ModelRetry, RunContext
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from sikarescue import telemetry
from sikarescue.agent.advisor import (
    MAX_OUTPUT_TOKENS,
    AdvisorConfig,
    AgentUsage,
    Orchestrator,
    describe_failure,
)
from sikarescue.agent.model import ModelChoice, build_model, describe_model
from sikarescue.config import Settings
from sikarescue.models import (
    DomainModel,
    EvidenceSource,
    ExtractedFailureEvidence,
    FailureEvidence,
    PaymentTransaction,
    ProviderIncident,
)
from sikarescue.services.evidence import envelope_evidence, grounding_problems
from sikarescue.services.model_boundary import assert_no_recipient_pii

INSTRUCTIONS = """\
You extract structured FailureEvidence from ONE raw, synthetic payout-provider incident.
You do NOT decide whether the payout failed, whether recovery is safe, or whether money may
move. A deterministic verifier decides that from your citations, and it rejects any claim it
cannot find verbatim in the payload.

Rules:
- Report only what the payload states. If something is not stated, use null or UNKNOWN.
  Never infer, guess or complete missing information.
- Copy provider_code, provider_reference and provider_message EXACTLY as written, character
  for character. provider_message may be a verbatim excerpt.
- evidence_fragments: 1-6 short verbatim substrings of the payload (each under 200 characters,
  copied exactly, without added quotes or ellipses) that support your fields.
- transport_outcome describes the payout request itself, from the client log lines:
  RESPONSE_RECEIVED only if the client received the provider's response to that request;
  TIMEOUT if no response arrived before the client's timeout; CONNECTION_LOST if the
  connection dropped. A later status probe is NOT a response to the payout request.
- acceptance_stage: PRE_ACCEPTANCE only if the provider explicitly says the instruction was
  rejected before it was accepted or queued; POST_ACCEPTANCE if it says it was accepted,
  queued or processed; otherwise UNKNOWN. An HTTP status code alone proves neither.
- explicit_rejection: true only if the provider explicitly rejected the instruction; false
  only if it explicitly accepted it; otherwise null.
- The payload contains no personal data; never add any.
"""


@dataclass
class EvidenceDeps:
    payload: str
    rejections: list[list[str]] = field(default_factory=list)


evidence_agent = Agent(
    name="failure_evidence_extractor",
    deps_type=EvidenceDeps,
    output_type=ExtractedFailureEvidence,
    instructions=INSTRUCTIONS,
    retries={"output": 2},
)


@evidence_agent.output_validator
async def _grounded(
    ctx: RunContext[EvidenceDeps], extraction: ExtractedFailureEvidence
) -> ExtractedFailureEvidence:
    problems = grounding_problems(ctx.deps.payload, extraction)
    if problems:
        ctx.deps.rejections.append(problems)
        telemetry.event(
            "failure_evidence_rejected", level="warn", problems="; ".join(problems)[:500]
        )
        raise ModelRetry(
            "Every cited string must be copied verbatim from the payload. Fix exactly these: "
            + "; ".join(problems)
        )
    return extraction


class EvidenceExtraction(DomainModel):
    """How the evidence was obtained. Display and audit only: it authorises nothing."""

    evidence: FailureEvidence
    orchestrator: Orchestrator
    model: str | None = None
    provider_model: str | None = None
    via_gateway: bool = False
    gateway_route: str | None = None
    fallback_reason: str | None = None
    rejected_drafts: int = Field(default=0, ge=0)
    usage: AgentUsage | None = None
    elapsed_seconds: float = Field(ge=0)
    trace_id: str | None = None


def _failure_reason(exc: BaseException, deps: EvidenceDeps, config: AdvisorConfig) -> str:
    if isinstance(exc, UnexpectedModelBehavior) and deps.rejections:
        return f"extraction kept citing text not in the payload ({deps.rejections[-1][0]})"[:200]
    return describe_failure(exc, config)


class EvidenceExtractor:
    def __init__(
        self,
        config: AdvisorConfig | None = None,
        *,
        settings: Settings | None = None,
        model: Model | None = None,
        http_client: Any | None = None,
    ) -> None:
        self.config = config or AdvisorConfig()
        self._settings = settings
        self._model = model  # injected (tests); otherwise resolved from settings
        self._http_client = http_client

    def _current_settings(self) -> Settings:
        if self._settings is None:
            self._settings = Settings()
        return self._settings

    async def extract(
        self, incident: ProviderIncident, instruction: PaymentTransaction
    ) -> EvidenceExtraction:
        started = time.perf_counter()
        with (
            telemetry.span(
                "provider_payload_received",
                transaction_id=incident.transaction_id,
                incident_id=incident.incident_id,
                rail_id=incident.rail_id.value,
                response_received=incident.response_received,
                http_status=incident.http_status,
                payload_bytes=len(incident.raw_payload.encode()),
                raw_payload_digest=incident.raw_payload_digest[:12],
            ),
            telemetry.span("failure_evidence_extracted", agent_mode=self.config.mode) as span,
        ):
            outcome = await self._extract(incident, instruction, started)
            evidence = outcome.evidence
            span.set(
                extracted_by=evidence.extracted_by.value,
                orchestrator=outcome.orchestrator.value,
                model=outcome.model,
                fallback_reason=outcome.fallback_reason,
                transport_outcome=evidence.transport_outcome.value,
                acceptance_stage=evidence.acceptance_stage.value,
                provider_code=evidence.provider_code,
                fragments=len(evidence.evidence_fragments),
                rejected_drafts=outcome.rejected_drafts,
            )
            return outcome

    async def _extract(
        self, incident: ProviderIncident, instruction: PaymentTransaction, started: float
    ) -> EvidenceExtraction:
        if self.config.mode != "pydantic":
            return self._envelope(incident, started, Orchestrator.DETERMINISTIC, None)
        deps = EvidenceDeps(payload=incident.raw_payload)
        try:
            assert_no_recipient_pii(incident.raw_payload, instruction)  # boundary check
            return await self._run_agent(incident, deps, started)
        except Exception as exc:  # CancelledError propagates
            reason = _failure_reason(exc, deps, self.config)
            telemetry.event(
                "evidence_extraction_fallback",
                level="warn",
                transaction_id=incident.transaction_id,
                reason=reason,
            )
            outcome = self._envelope(incident, started, Orchestrator.DETERMINISTIC_FALLBACK, reason)
            return outcome.model_copy(update={"rejected_drafts": len(deps.rejections)})

    async def _run_agent(
        self, incident: ProviderIncident, deps: EvidenceDeps, started: float
    ) -> EvidenceExtraction:
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
        result = await asyncio.wait_for(
            evidence_agent.run(
                f"Extract FailureEvidence for attempt {incident.attempt_id} on "
                f"{incident.rail_id}. The raw incident follows between the markers.\n"
                f"-----BEGIN INCIDENT-----\n{incident.raw_payload}-----END INCIDENT-----",
                deps=deps,
                model=model,
                model_settings=ModelSettings(
                    timeout=self.config.request_timeout_seconds, max_tokens=MAX_OUTPUT_TOKENS
                ),
                usage_limits=UsageLimits(request_limit=self.config.max_model_requests),
            ),
            timeout=self.config.run_timeout_seconds,
        )
        evidence = FailureEvidence.bind(incident, result.output, EvidenceSource.PYDANTIC_AI)
        usage = result.usage
        return EvidenceExtraction(
            evidence=evidence,
            orchestrator=Orchestrator.PYDANTIC_AI,
            model=choice.name if choice else model.model_name,
            provider_model=next(
                (
                    m.model_name
                    for m in reversed(result.all_messages())
                    if isinstance(m, ModelResponse) and m.model_name
                ),
                None,
            ),
            via_gateway=bool(choice and choice.via_gateway),
            gateway_route=choice.route if choice else None,
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

    @staticmethod
    def _envelope(
        incident: ProviderIncident,
        started: float,
        orchestrator: Orchestrator,
        reason: str | None,
    ) -> EvidenceExtraction:
        return EvidenceExtraction(
            evidence=envelope_evidence(incident),
            orchestrator=orchestrator,
            fallback_reason=reason,
            elapsed_seconds=round(time.perf_counter() - started, 3),
            trace_id=telemetry.current_trace_id(),
        )
