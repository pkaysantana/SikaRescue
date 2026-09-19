"""LIVE smoke test: SK-10421 end to end through Pydantic AI -> Gateway -> model -> tools ->
Modal -> deterministic execution -> Logfire.

Every assertion is checked from RUNTIME data (the advisory outcome, the plan, the journal,
the messages actually sent to the model, the HTTP destinations actually called and the
spans actually emitted), never from configuration. Nothing is claimed that was not
observed; exit code 0 only if every check passed.

  uv run python scripts/gateway_ping.py                 # run first: minimal connectivity
  uv run python scripts/agent_smoke.py --compute modal
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import sys
from decimal import Decimal

try:
    import pydantic_ai
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from pydantic_ai import ModelMessagesTypeAdapter, ModelResponse, capture_run_messages

    from sikarescue import telemetry
    from sikarescue.agent.advisor import AdvisorConfig, Orchestrator, RecoveryAdvisor
    from sikarescue.agent.live import HeaderRecorder, preflight, verify_trace_in_logfire
    from sikarescue.agent.model import gateway_endpoint
    from sikarescue.cli.demo import run_demo
    from sikarescue.compute.backend import build_compute_backend
    from sikarescue.compute.scenarios import workload_config
    from sikarescue.config import Settings
    from sikarescue.demo_data.sk10421 import (
        RECIPIENT_NAME,
        RECIPIENT_PHONE,
        RECIPIENT_REFERENCE,
        TRANSACTION_ID,
        build_demo_world,
    )
    from sikarescue.models import FundsLocation, RailId
except ImportError:
    sys.exit("Run inside the project environment:  uv run python scripts/agent_smoke.py")

PII = (RECIPIENT_NAME, RECIPIENT_PHONE, RECIPIENT_PHONE.replace(" ", ""), RECIPIENT_REFERENCE)
APP_SPANS = {
    "sikarescue_recovery",
    "incident_received",
    "agent_run_started",
    "agent_tool_inspect_incident",
    "agent_tool_evaluate_routes",
    "modal_compute_started",
    "modal_compute_finished",
    "route_evaluations_verified",
    "recovery_plan_created",
    "recovery_advice_generated",
    "approval_received",
    "execution_admitted",
    "payout_completed",
    "recipient_credit_recorded",
    "reconciliation_completed",
}
PYDANTIC_AI_SPANS = {"invoke_agent recovery_agent", "execute_tool evaluate_recovery_options"}


class _ExportErrors(logging.Handler):
    """Collects WARNING+ records from Logfire / OpenTelemetry exporters during the run."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(f"{record.name}: {record.getMessage()}"[:200])


def _pii_free(text: str) -> bool:
    lowered = text.lower()
    return not any(v.lower() in lowered for v in PII)


async def main(args: argparse.Namespace) -> int:
    settings = Settings()
    model_name = args.model or settings.agent_model
    route = settings.gateway_route
    print("== Live smoke: Pydantic AI -> Gateway -> model -> tools -> Modal -> Logfire ==")
    lines, missing = preflight(settings, model_name)
    print("\n".join(lines))
    if not model_name.startswith("gateway/"):
        print("\nNOT RUN: this smoke test requires a gateway/<flavour>:<model> model.")
        return 2
    if missing:
        print(f"\nNOT RUN: missing {', '.join(missing)}. No live call was attempted.")
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
    export_errors = _ExportErrors()
    for name in ("logfire", "opentelemetry"):
        logging.getLogger(name).addHandler(export_errors)
    print(f"Logfire                  : {status.detail}")

    backend = args.compute or settings.compute_backend
    world = build_demo_world(
        payout_latency_seconds=settings.payout_latency_seconds,
        compute=build_compute_backend(
            backend,
            remote_timeout_seconds=settings.modal_timeout_seconds,
            shards_per_scenario=settings.modal_shards_per_scenario,
        ),
        simulation=workload_config(
            "stress" if backend == "modal" else "quick", seed=settings.simulation_seed
        ),
    )
    # The seed's historical audit events (the incident itself) are emitted while the world is
    # built, outside the recovery run; only the run's own spans are checked below.
    memory.clear()
    recorder = HeaderRecorder()
    advisor = RecoveryAdvisor(
        world.service,
        AdvisorConfig.from_settings(settings, mode="pydantic", model_name=model_name),
        settings=settings,
        http_client=recorder.client(settings.agent_request_timeout_seconds),
    )
    out = io.StringIO()
    with capture_run_messages() as messages:
        outcome = await run_demo(
            world, auto_approve=True, out=out, show_timeline=False, advisor=advisor
        )
    telemetry.flush()
    if args.verbose:
        print(out.getvalue())

    advisory = outcome.advisory
    plan = outcome.plan
    state = world.service.get_transaction_state(TRANSACTION_ID)
    aggregate = world.repository.get(TRANSACTION_ID)
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append((name, bool(ok), detail))

    # --- orchestration, route, model --------------------------------------------------
    orchestrator = advisory.orchestrator if advisory else None
    reason = f" ({advisory.fallback_reason})" if advisory and advisory.fallback_reason else ""
    check(
        "orchestrator = pydantic_ai",
        orchestrator is Orchestrator.PYDANTIC_AI,
        f"{orchestrator}{reason}",
    )
    root, _ = gateway_endpoint(settings.gateway_base_url, route)
    root = root or "https://gateway"
    via = [d for d in recorder.destinations if d.startswith(f"{root}/{route}/")]
    check(
        f"model calls went via Gateway route {route}",
        bool(advisory and advisory.via_gateway and advisory.gateway_route == route)
        and bool(recorder.destinations)
        and len(via) == len(recorder.destinations),
        f"{len(via)}/{len(recorder.destinations)} requests to {sorted(set(recorder.destinations))}",
    )
    statuses = sorted({code for code, _ in recorder.responses})
    reported = sorted({m.model_name or "?" for m in messages if isinstance(m, ModelResponse)})
    check(
        "model = configured identifier",
        bool(advisory and advisory.model == model_name),
        f"{advisory.model if advisory else None}; provider reported {reported}; HTTP {statuses}",
    )

    # --- deterministic plan and compute --------------------------------------------------
    compute = plan.compute if plan else None
    compute_detail = "no plan"
    if compute is not None:
        compute_detail = (
            f"backend={compute.backend}, jobs={compute.parallel_jobs}, "
            f"outcomes={compute.simulated_trials:,}"
        )
        if compute.fallback_from:
            compute_detail += f", fallback from {compute.fallback_from}: {compute.fallback_reason}"
    check(
        "compute backend = modal (no fallback)",
        compute is not None and compute.backend == "modal" and compute.fallback_from is None,
        compute_detail,
    )
    check(
        "transaction = SK-10421",
        bool(plan and plan.transaction_id == TRANSACTION_ID),
        plan.transaction_id if plan else "no plan",
    )
    check(
        "executable plan = deterministic MOMO_B",
        bool(plan and plan.rail_id is RailId.MOMO_B and plan == aggregate.current_plan),
        f"{plan.rail_id if plan else None} (plan {plan.plan_id if plan else None})",
    )
    check(
        "incremental fee = £0.18",
        bool(plan and plan.incremental_fee.amount == Decimal("0.18")),
        f"{plan.incremental_fee if plan else None}",
    )
    check(
        "amount/source unchanged",
        bool(
            plan
            and str(plan.amount) == "1830.00 GHS"
            and plan.source is FundsLocation.GH_SETTLEMENT_ACCOUNT
        ),
        f"{plan.amount if plan else None} from {plan.source if plan else None}",
    )
    advice = advisory.advice if advisory else None
    check(
        "advice consistent with plan",
        bool(
            advice
            and plan
            and advice.recommended_route is plan.rail_id
            and advice.incremental_fee_gbp == "0.18"
            and advice.approval_required
        ),
        f"advice route={advice.recommended_route if advice else None}, "
        f"fee={advice.incremental_fee_gbp if advice else None}",
    )
    approval = aggregate.approvals.get(plan.plan_id) if plan else None
    check(
        "human approval bound to plan hash",
        bool(approval and approval.approved and approval.plan_hash == plan.plan_hash),
        f"approver={approval.approver if approval else None}",
    )
    executed = [r.rail_id.value for r in world.gateway.requests]
    check("only the outstanding payout executed", executed == ["MOMO_B"], f"payouts={executed}")

    # --- ledger outcome -------------------------------------------------------------------
    check("recipient credited = yes", state.recipient_credited, str(state.recipient_credited))
    check("sender debit count = 1", state.sender_debit_count == 1, str(state.sender_debit_count))
    check(
        "recipient credit count = 1",
        state.recipient_credit_count == 1,
        str(state.recipient_credit_count),
    )
    duplicates = max(0, state.sender_debit_count - 1)
    check("duplicate sender debits = 0", duplicates == 0, str(duplicates))
    check(
        "final state = RECONCILED",
        state.recovery_state.value == "RECONCILED",
        str(state.recovery_state),
    )

    # --- model boundary -------------------------------------------------------------------
    payload = ModelMessagesTypeAdapter.dump_json(messages).decode()
    requests = sum(1 for m in messages if not isinstance(m, ModelResponse))
    check(
        "model inputs PII-free",
        _pii_free(payload),
        f"{len(messages)} messages, {len(payload):,} bytes serialised, {requests} requests",
    )

    # --- Logfire ----------------------------------------------------------------------------
    spans = memory.get_finished_spans()
    names = {s.name for s in spans}
    traces = {format(s.context.trace_id, "032x") for s in spans}
    missing_spans = (APP_SPANS | PYDANTIC_AI_SPANS) - names
    check(
        "app + Pydantic AI spans emitted",
        not missing_spans,
        f"{len(spans)} spans" + (f"; missing {sorted(missing_spans)}" if missing_spans else ""),
    )
    check("one trace id", traces == {outcome.trace_id}, f"{sorted(traces)}")
    attrs = json.dumps([dict(s.attributes or {}) for s in spans], default=str)
    check("no PII in span attributes", _pii_free(attrs), f"{len(attrs):,} bytes of attributes")
    if status.exporting:
        if os.getenv("LOGFIRE_READ_TOKEN"):
            ok, detail = await verify_trace_in_logfire(outcome.trace_id or "", APP_SPANS)
        else:
            ok = not export_errors.messages
            detail = (
                "flushed with no exporter errors (read token absent: arrival not queried)"
                if ok
                else "; ".join(export_errors.messages[:3])
            )
        check("Logfire export", ok, detail)
    else:
        check("Logfire export", False, "LOGFIRE_TOKEN not set: spans stayed local")

    if advisory and advisory.usage:
        u = advisory.usage
        print(
            f"\nagent: {' -> '.join(advisory.tool_calls)} | {u.requests} model requests, "
            f"{u.tool_calls} tool calls, {u.input_tokens:,} in / {u.output_tokens:,} out tokens, "
            f"{advisory.elapsed_seconds:.1f}s, {advisory.rejected_drafts} rejected drafts"
        )
    if compute:
        print(
            f"modal: {compute.parallel_jobs} jobs, {compute.simulated_trials:,} outcomes, "
            f"{compute.elapsed_seconds:.2f}s wall"
        )
    print(f"trace id: {outcome.trace_id}")
    print()
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<40} {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", help="override SIKARESCUE_AGENT_MODEL (gateway/... only)")
    parser.add_argument("--compute", choices=("local", "modal"), help="override compute backend")
    parser.add_argument("--verbose", action="store_true", help="print the full demo transcript")
    pydantic_ai.BANNER_ENABLED = False
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(asyncio.run(main(parser.parse_args())))
