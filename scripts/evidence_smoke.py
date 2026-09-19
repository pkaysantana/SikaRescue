"""LIVE smoke test: provider incident -> Pydantic AI (via Gateway) -> FailureEvidence ->
deterministic FailureVerifier, for both synthetic MOMO_A payloads.

Every check is made from RUNTIME data: the extraction outcome, the verifier's verdict, the
journal, the HTTP destinations actually called and the spans actually emitted. The model only
extracts; the verdict is always the deterministic verifier's. Exit code 0 only if all passed.

  uv run python scripts/evidence_smoke.py
"""

from __future__ import annotations

import asyncio
import os
import sys

try:
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from pydantic_ai import capture_run_messages

    from sikarescue import telemetry
    from sikarescue.agent.advisor import AdvisorConfig, Orchestrator
    from sikarescue.agent.evidence import EvidenceExtractor
    from sikarescue.agent.live import HeaderRecorder, preflight
    from sikarescue.agent.model import gateway_endpoint
    from sikarescue.config import Settings
    from sikarescue.demo_data.incidents import IncidentScenario
    from sikarescue.demo_data.sk10421 import (
        RECIPIENT_NAME,
        RECIPIENT_PHONE,
        RECIPIENT_REFERENCE,
        build_demo_world,
    )
    from sikarescue.models import AttemptOutcome, RecoveryState
except ImportError:
    sys.exit("Run inside the project environment:  uv run python scripts/evidence_smoke.py")

PII = (RECIPIENT_NAME, RECIPIENT_PHONE, RECIPIENT_PHONE.replace(" ", ""), RECIPIENT_REFERENCE)
EXPECTED = {
    IncidentScenario.DEFINITIVE: AttemptOutcome.DEFINITIVE_FAILED,
    IncidentScenario.UNKNOWN: AttemptOutcome.UNKNOWN,
}
SPANS = {"provider_payload_received", "failure_evidence_extracted", "failure_evidence_verified"}


async def main() -> int:
    settings = Settings()
    model_name = settings.agent_model
    route = settings.gateway_route
    print("== Live smoke: incident -> Pydantic AI -> FailureEvidence -> deterministic verifier ==")
    lines, missing = preflight(settings, model_name)
    print("\n".join(lines))
    if not model_name.startswith("gateway/") or missing:
        print(f"\nNOT RUN: needs a gateway/<flavour>:<model> model and {missing or 'credentials'}.")
        return 2
    for stale in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
        if os.environ.pop(stale, None) is not None:
            print(f"{stale:<25}: present in the environment; removed for this run (unused)")
    memory = InMemorySpanExporter()
    status = telemetry.configure_telemetry(
        token=settings.logfire_token.get_secret_value() if settings.logfire_token else None,
        environment=settings.environment,
        span_processors=[SimpleSpanProcessor(memory)],
    )
    print(f"Logfire                  : {status.detail}\n")

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append((name, bool(ok), detail))

    root, _ = gateway_endpoint(settings.gateway_base_url, route)
    for scenario, expected in EXPECTED.items():
        world = build_demo_world(incident=scenario)
        aggregate = world.repository.get(world.transaction_id)
        incident = aggregate.incident
        assert incident is not None
        memory.clear()
        recorder = HeaderRecorder()
        extractor = EvidenceExtractor(
            AdvisorConfig.from_settings(settings, mode="pydantic"),
            settings=settings,
            http_client=recorder.client(settings.agent_request_timeout_seconds),
        )
        with capture_run_messages() as messages:
            extraction = await extractor.extract(incident, aggregate.instruction)
        classification = await world.service.classify_provider_incident(
            world.transaction_id, extraction.evidence
        )
        telemetry.flush()
        verdict = classification.verdict
        evidence = extraction.evidence
        label = scenario.value
        answer = f"HTTP {incident.http_status}" if incident.response_received else "no response"
        print(f"--- {label}: {answer}")
        print(f"extraction : {extraction.orchestrator} {extraction.fallback_reason or ''}".rstrip())
        print(
            f"evidence   : code={evidence.provider_code} transport={evidence.transport_outcome} "
            f"stage={evidence.acceptance_stage} explicit_rejection={evidence.explicit_rejection} "
            f"reference={evidence.provider_reference} completeness={evidence.completeness}"
        )
        for fragment in evidence.evidence_fragments:
            print(f"  cites    : {fragment!r}")
        print(f"verdict    : {verdict.classification} (recorded {classification.recorded_outcome})")
        for c in (*verdict.checks, *verdict.requirements):
            print(f"  {'PASS' if c.passed else 'FAIL'} {c.name:<36} {c.detail}")
        print(f"trace id   : {extraction.trace_id}\n")

        check(
            f"{label}: extracted by Pydantic AI",
            extraction.orchestrator is Orchestrator.PYDANTIC_AI,
            f"{extraction.orchestrator} {extraction.fallback_reason or ''}".strip(),
        )
        via = [d for d in recorder.destinations if d.startswith(f"{root}/{route}/")]
        check(
            f"{label}: model calls via Gateway route {route}",
            bool(recorder.destinations) and len(via) == len(recorder.destinations),
            f"{len(via)}/{len(recorder.destinations)} requests",
        )
        check(
            f"{label}: provider-reported model",
            bool(extraction.provider_model),
            f"{extraction.provider_model}; drafts rejected {extraction.rejected_drafts}",
        )
        check(
            f"{label}: verifier classification = {expected}",
            verdict.classification is expected,
            f"{verdict.classification}; reasons: {'; '.join(verdict.reasons)[:120]}",
        )
        state = world.service.get_transaction_state(world.transaction_id)
        want_state = (
            RecoveryState.DIAGNOSING
            if expected is AttemptOutcome.DEFINITIVE_FAILED
            else RecoveryState.MANUAL_REVIEW
        )
        check(
            f"{label}: transaction state",
            state.recovery_state is want_state,
            f"{state.recovery_state}; funds {state.funds_position.position_status}",
        )
        frontier = world.service.get_safe_action_frontier(world.transaction_id)
        payout_expected = expected is AttemptOutcome.DEFINITIVE_FAILED
        check(
            f"{label}: payout actions {'offered' if payout_expected else 'absent'}",
            frontier.payout_actions_permitted == payout_expected,
            f"basis {frontier.basis}; actions {[a.kind.value for a in frontier.actions]}",
        )
        check(
            f"{label}: nothing dispatched",
            world.gateway.requests == [],
            f"{len(world.gateway.requests)} payout requests",
        )
        sent = " ".join(str(m) for m in messages).lower()
        check(
            f"{label}: model inputs PII-free",
            not any(v.lower() in sent for v in PII),
            f"{len(messages)} messages",
        )
        names = {s.name for s in memory.get_finished_spans()}
        check(
            f"{label}: ingestion spans emitted",
            names >= SPANS,
            ", ".join(sorted(SPANS & names)),
        )

    telemetry.flush()
    width = max(len(n) for n, _, _ in checks)
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<{width}}  {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
