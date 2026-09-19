"""Terminal demo of the seeded SK-10421 recovery.

Default: deterministic core only (no LLM). With `--agent pydantic` (or
SIKARESCUE_AGENT_MODE=pydantic) a Pydantic AI agent investigates and explains the plan; the
deterministic core still plans, requires approval, executes and reconciles. If the model is
unavailable the run continues with `orchestrator = deterministic_fallback`.

Everything shown is SYNTHETIC: no real money, rails, providers or people.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import textwrap
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TextIO

from sikarescue import telemetry
from sikarescue.agent.advisor import AdvisorConfig, AdvisoryOutcome, Orchestrator, RecoveryAdvisor
from sikarescue.compute.backend import build_compute_backend
from sikarescue.compute.scenarios import workload_config
from sikarescue.config import get_settings
from sikarescue.demo_data.sk10421 import TRANSACTION_ID, DemoWorld, build_demo_world
from sikarescue.errors import ConfigurationError, SikaRescueError
from sikarescue.models import (
    Currency,
    ExecutionResult,
    ExecutionStatus,
    Money,
    OperationType,
    ReconciliationResult,
    RecoveryPlan,
    RouteEvaluation,
    SettlementLegStatus,
)
from sikarescue.services.model_boundary import (
    assert_no_recipient_pii,
    build_model_transaction_view,
    redacted_recipient_preview,
)

APPROVER = "cli.operator"
OK, FAIL, WARN = "✓", "✗", "!"
WRAP = 96
LEG_LABELS = {
    OperationType.SENDER_DEBIT: "sender debit",
    OperationType.FX_CONVERSION: "GBP→GHS FX",
    OperationType.GH_SETTLEMENT: "Ghana settlement",
    OperationType.RECIPIENT_CREDIT: "payout",
}
LEG_SYMBOLS = {
    SettlementLegStatus.SUCCESS: OK,
    SettlementLegStatus.FAILED: FAIL,
    SettlementLegStatus.UNKNOWN: "?",
    SettlementLegStatus.IN_PROGRESS: "…",
}


@dataclass
class DemoOutcome:
    world: DemoWorld
    exit_code: int
    plan: RecoveryPlan | None = None
    execution: ExecutionResult | None = None
    reconciliation: ReconciliationResult | None = None
    advisory: AdvisoryOutcome | None = None
    trace_id: str | None = None


def _money(m: Money) -> str:
    return f"£{m.amount:,.2f}" if m.currency is Currency.GBP else f"{m.currency} {m.amount:,.2f}"


def _pct(p: float | None) -> str:
    return "n/a" if p is None else f"{p * 100:.1f}%"


def _secs(s: float | None) -> str:
    return "n/a" if s is None else f"{s:.1f} s"


class _Printer:
    def __init__(self, out: TextIO) -> None:
        self.out = out

    def __call__(self, text: str = "") -> None:
        print(text, file=self.out)

    def section(self, title: str) -> None:
        self()
        self(f"── {title} " + "─" * max(4, 66 - len(title)))

    def field(self, label: str, text: str, width: int = 13) -> None:
        prefix = f"{label:<{width}}: "
        self(textwrap.fill(text, WRAP, initial_indent=prefix, subsequent_indent=" " * len(prefix)))


async def run_demo(
    world: DemoWorld,
    *,
    auto_approve: bool,
    input_fn: Callable[[str], str] = input,
    out: TextIO = sys.stdout,
    show_timeline: bool = True,
    advisor: RecoveryAdvisor | None = None,
) -> DemoOutcome:
    advisor = advisor or RecoveryAdvisor(world.service)  # deterministic unless configured
    with telemetry.span(
        "sikarescue_recovery",
        transaction_id=TRANSACTION_ID,
        agent_mode=advisor.config.mode,
        configured_compute_backend=world.service.compute_backend,
    ) as root:
        outcome = await _run_demo(
            world,
            advisor,
            auto_approve=auto_approve,
            input_fn=input_fn,
            p=_Printer(out),
            show_timeline=show_timeline,
            trace_id=root.trace_id,
        )
        state = world.service.get_transaction_state(TRANSACTION_ID)
        root.set(
            state=state.recovery_state.value,
            orchestrator=outcome.advisory.orchestrator.value if outcome.advisory else None,
            exit_code=outcome.exit_code,
        )
    return outcome


async def _run_demo(
    world: DemoWorld,
    advisor: RecoveryAdvisor,
    *,
    auto_approve: bool,
    input_fn: Callable[[str], str],
    p: _Printer,
    show_timeline: bool,
    trace_id: str | None,
) -> DemoOutcome:
    service = world.service
    aggregate = world.repository.get(TRANSACTION_ID)
    instruction = aggregate.instruction
    p("SikaRescue · failed-payment recovery demo")
    p("SYNTHETIC DATA ONLY: no real money, rails, providers or people.")

    # 1. Incident -------------------------------------------------------------------
    state = service.get_transaction_state(TRANSACTION_ID)
    p.section("1. Incident")
    p(
        f"Payment {instruction.transaction_id}   {_money(instruction.send_amount)}  UK → Ghana"
        f"   ({_money(instruction.payout_amount)} at synthetic FX {instruction.fx_rate:.2f})"
    )
    p(f"Recipient endpoint: mobile money (opaque token {instruction.recipient_token})")
    p()
    for leg in state.legs:
        label = LEG_LABELS[leg.operation]
        p(f"  {LEG_SYMBOLS[leg.status]} {label:<17} {leg.rail_id}")
    failed_attempt = next(
        a for a in aggregate.journal.attempts() if a.rail_id == state.failed_leg and a.failure
    )
    failure = failed_attempt.failure
    assert failure is not None
    p()
    p(f"{state.failed_leg} failure: synthetic HTTP {failure.http_status} {failure.provider_code}")
    p(f"  stage   : {failure.stage} (provider rejected before accepting the request)")
    p(f"  outcome : {failed_attempt.outcome} → no value moved")
    p()
    p(f"Funds currently located: {state.funds_location}")
    p(f"{WARN} Important: sender already debited → do NOT restart from origin")
    p(f"  safe_to_restart_from_origin = {state.safe_to_restart_from_origin}")
    obligation = state.outstanding_obligation
    assert obligation is not None
    p(f"Outstanding obligation: credit the recipient {_money(obligation.amount)} exactly once")
    p(f"  ({obligation.effect_key})")

    # 2. Model boundary -------------------------------------------------------------
    p.section("2. Model boundary (PII guardrail preview)")
    preview = redacted_recipient_preview(instruction)
    p("RAW synthetic PII - stays inside SikaRescue, never sent to a model:")
    for key, value in preview["raw"].items():
        p(f"  {key:<20}: {value}")
    p("MODEL BOUNDARY - what a model would see for the recipient:")
    for key, value in preview["model_boundary"].items():
        p(f"  {key:<20}: {value}")
    view = build_model_transaction_view(aggregate)
    payload = view.model_dump_json()
    assert_no_recipient_pii(payload, instruction)
    p("Allowlisted ModelTransactionView (complete model-facing transaction payload):")
    for key, value in view.model_dump(mode="json").items():
        if key == "legs":
            value = ", ".join(f"{leg['rail_id']}={leg['status']}" for leg in value)
        p(f"  {key:<28}: {value}")
    p(f"{OK} PII scan of model payload: clean ({len(payload)} bytes, no recipient identity)")

    # 3. Planning -------------------------------------------------------------------
    p.section("3. Recovery planning (deterministic)")
    if advisor.config.mode == "pydantic":
        choice = advisor.model_choice()
        label = choice.label if choice else "injected model"
        p(f"Pydantic AI agent ({label})")
        p("  investigates and REQUESTS the analysis; the deterministic core computes it.")
    started = time.perf_counter()
    try:
        advisory = await advisor.advise(TRANSACTION_ID)
    except SikaRescueError as exc:
        p(f"{FAIL} Planning stopped: {exc}")
        _proof(p, world, planning_ms=None, plan=None, advisory=None, trace_id=trace_id)
        return DemoOutcome(world=world, exit_code=1, trace_id=trace_id)
    planning_ms = (time.perf_counter() - started) * 1000
    plan = advisory.plan
    passing = [e for e in plan.evaluations if e.passed]
    rejected = [e for e in plan.evaluations if not e.passed]
    alternatives = sum(1 for e in plan.evaluations if e.rail_id != state.failed_leg)
    p(
        f"Discover : {len(plan.evaluations)} candidate payout routes from {plan.source} "
        f"({alternatives} alternatives + retry of the failed {state.failed_leg})"
    )
    p("Filter   : hard constraints run BEFORE any simulation or scoring")
    for e in rejected:
        p(f"  {FAIL} {e.rail_id:<17} REJECTED: {' / '.join(r.value for r in e.rejection_reasons)}")
        for detail in e.rejection_details:
            p(f"      - {detail}")
    for e in passing:
        p(f"  {OK} {e.rail_id:<17} passed every hard constraint")
    summary = plan.compute
    assert summary is not None
    p(
        f"Simulate : {summary.simulated_trials:,} synthetic recovery outcomes = "
        f"{summary.routes_simulated} routes x {len(summary.scenarios)} scenarios x "
        f"{summary.trials_per_scenario:,} trials"
    )
    p(f"           scenarios: {', '.join(s.value.lower() for s in summary.scenarios)}")
    if summary.parallel_jobs:
        p(
            f"           backend={summary.backend}  parallel jobs={summary.parallel_jobs} "
            f"({summary.shards_per_scenario} shards per route x scenario)  seed={summary.seed}"
        )
        p(
            f"           wall {summary.elapsed_seconds:.2f} s  remote compute "
            f"{summary.remote_compute_seconds or 0:.2f} CPU-s  function {summary.function_ref}"
        )
    else:
        p(
            f"           backend={summary.backend} (in-process)  seed={summary.seed}  "
            f"{summary.elapsed_seconds:.2f} s"
        )
    if summary.fallback_from:
        p(
            f"{WARN} {summary.fallback_from} compute unavailable -> evaluated with "
            f"{summary.backend} instead"
        )
        p(f"           reason: {summary.fallback_reason}")
    p("           rejected routes: 0 trials (never sent for simulation)")
    p("Rank     : synthetic score = 40% simulated reliability + 25% cost")
    p("           + 20% simulated p95 latency + 15% route quality")
    for e in passing:
        _print_route(p, e)

    # 4. Plan -----------------------------------------------------------------------
    p.section("4. Immutable recovery plan")
    p(f"Plan       : {plan.plan_id}   hash {plan.plan_hash[:16]}…")
    p(f"Bound to   : transaction revision {plan.expected_revision}")
    p(f"Route      : {plan.source} → {plan.rail_id} → recipient")
    p(f"Amount     : {_money(plan.amount)} (the outstanding obligation only)")
    p(f"Fee        : {_money(plan.incremental_fee)} incremental, borne by {plan.fee_bearer}")
    p("Why:")
    for reason in plan.selection_reasons:
        p(f"  {OK} {reason}")

    # 5. Advice ---------------------------------------------------------------------
    _print_advice(p, advisory)

    # 6. Approval -------------------------------------------------------------------
    p.section("6. Human approval")
    request = await service.request_recovery_approval(plan.plan_id)
    p(request.summary)
    if auto_approve:
        approved = True
        p(f"Approved via --approve (approver: {APPROVER}).")
    else:
        try:
            answer = input_fn(f"Approve plan {plan.plan_id} ({plan.rail_id})? [y/N] ")
        except EOFError:
            p("No interactive input available: plan left AWAITING_APPROVAL.")
            p("Re-run with --approve to approve non-interactively.")
            return DemoOutcome(
                world=world, exit_code=2, plan=plan, advisory=advisory, trace_id=trace_id
            )
        approved = answer.strip().lower() in {"y", "yes"}
        p(f"Operator answered: {'approve' if approved else 'decline'}")
    if not approved:
        await service.reject_recovery(
            plan.plan_id, plan_hash=plan.plan_hash, approver=APPROVER, comment="declined in CLI"
        )
        p(f"{FAIL} Plan declined → transaction escalated to MANUAL_REVIEW. No money moved.")
        _proof(p, world, planning_ms=planning_ms, plan=plan, advisory=advisory, trace_id=trace_id)
        return DemoOutcome(
            world=world, exit_code=1, plan=plan, advisory=advisory, trace_id=trace_id
        )
    await service.approve_recovery(plan.plan_id, plan_hash=plan.plan_hash, approver=APPROVER)

    # 7. Execute --------------------------------------------------------------------
    p.section("7. Execute the outstanding payout only")
    p(f"Executing remaining leg: {plan.source} → {plan.rail_id} → recipient …")
    execution = await service.execute_recovery(plan.plan_id)
    if execution.status is not ExecutionStatus.SUCCEEDED:
        p(f"{FAIL} Payout outcome {execution.status}: {execution.detail}")
        p("No automatic retry or reroute: value may have moved. Manual review required.")
        _proof(p, world, planning_ms=planning_ms, plan=plan, advisory=advisory, trace_id=trace_id)
        return DemoOutcome(
            world=world,
            exit_code=1,
            plan=plan,
            execution=execution,
            advisory=advisory,
            trace_id=trace_id,
        )
    p(f"{OK} Recipient credited {_money(plan.amount)} via {plan.rail_id}")
    p(f"{OK} Sender NOT debited again (sender debit effect replay is structurally impossible)")

    # 8. Reconcile ------------------------------------------------------------------
    p.section("8. Reconciliation")
    reconciliation = await service.reconcile_transaction(TRANSACTION_ID)
    for check in reconciliation.checks:
        p(f"  {OK if check.passed else FAIL} {check.name:<30} {check.detail}")

    _responsibilities(p, advisory, reconciled=reconciliation.reconciled)
    _proof(p, world, planning_ms=planning_ms, plan=plan, advisory=advisory, trace_id=trace_id)
    if show_timeline:
        _timeline(p, world)
    return DemoOutcome(
        world=world,
        exit_code=0 if reconciliation.reconciled else 1,
        plan=plan,
        execution=execution,
        reconciliation=reconciliation,
        advisory=advisory,
        trace_id=trace_id,
    )


def _print_route(p: _Printer, e: RouteEvaluation) -> None:
    assert e.score is not None
    p()
    p(f"  #{e.rank} {e.rail_id}   score {e.score.total:.3f}")
    p(f"     incremental fee           {_money(e.estimated_incremental_cost)} (operator-borne)")
    p(
        f"     simulated success         {_pct(e.simulated_success_probability)}"
        f"   (provider quote {_pct(e.quoted_reliability)})"
    )
    p(
        f"     latency p50 / p95         {_secs(e.p50_latency_seconds)} / "
        f"{_secs(e.p95_latency_seconds)}"
    )
    sla = e.scenario_results[0].sla_seconds
    p(f"     credited within SLA {sla:.0f}s  {_pct(e.simulated_within_sla_probability)}")
    for r in e.scenario_results[1:]:
        p(
            f"     stress {r.scenario.scenario_id.value.lower():<19}"
            f"success {_pct(r.simulated_success_probability):>6}   "
            f"within SLA {_pct(r.recovery_within_sla_probability):>6}"
        )


def _print_advice(p: _Printer, outcome: AdvisoryOutcome) -> None:
    orchestrator = outcome.orchestrator
    if orchestrator is Orchestrator.PYDANTIC_AI:
        p.section("5. Recovery advice (Pydantic AI · checked against the plan)")
        where = "via Pydantic AI Gateway" if outcome.via_gateway else "direct, NOT via Gateway"
        p(f"Orchestrator : {orchestrator} · {outcome.model} ({where})")
        p(f"Agent steps  : {' → '.join(outcome.tool_calls)}")
        usage = outcome.usage
        if usage is not None:
            p(
                f"               {usage.requests} model requests · {usage.tool_calls} tool calls"
                f" · {usage.input_tokens:,} input / {usage.output_tokens:,} output tokens"
                f" · {outcome.elapsed_seconds:.1f} s"
            )
        if outcome.rejected_drafts:
            p(
                f"{WARN} {outcome.rejected_drafts} advice draft(s) contradicted the plan and were "
                "sent back to the model for correction"
            )
    elif orchestrator is Orchestrator.DETERMINISTIC_FALLBACK:
        p.section("5. Recovery advice (deterministic fallback)")
        p(f"Orchestrator : {orchestrator}")
        p(f"{WARN} Pydantic AI advice unavailable: {outcome.fallback_reason}")
        p("  The same immutable plan is explained by deterministic code; nothing else changes.")
    else:
        p.section("5. Recovery advice (deterministic)")
        p(f"Orchestrator : {orchestrator} (no model requested; --agent pydantic to use one)")
    advice = outcome.advice
    if orchestrator is Orchestrator.PYDANTIC_AI:
        p("Narrative is model-written; every identifier and figure was checked against the plan.")
    else:
        p("Narrative is code-written from the plan's deterministic facts.")
    p.field("Incident", advice.incident_summary)
    p.field("Origin retry", advice.why_origin_retry_is_unsafe)
    p.field(
        "Recommend",
        f"{advice.recommended_route}  fee £{advice.incremental_fee_gbp} (operator)  "
        f"~{advice.estimated_arrival_seconds} s  simulated reliability "
        f"{_pct(advice.simulated_reliability)}",
    )
    p.field("Reasons", ", ".join(c.value for c in advice.reason_codes))
    for route in advice.rejected_routes:
        p.field(
            "Rejected",
            f"{route.rail_id} ({', '.join(r.value for r in route.reasons)}): {route.explanation}",
        )
    p.field("Stress", advice.stress_summary)
    p.field("Operator", advice.operator_message)
    p(f"{OK} Advice is display-only: approval and execution use plan {outcome.plan.plan_id}")


def _responsibilities(p: _Printer, advisory: AdvisoryOutcome, *, reconciled: bool) -> None:
    p.section("Who did what")
    if advisory.orchestrator is Orchestrator.PYDANTIC_AI:
        p("Pydantic AI        : inspected the incident · requested route evaluation")
        p("                     received verified deterministic results")
        p("                     produced structured RecoveryAdvice (validated against the plan)")
    elif advisory.orchestrator is Orchestrator.DETERMINISTIC_FALLBACK:
        p(f"Pydantic AI        : not used: {advisory.fallback_reason}")
    else:
        p("Pydantic AI        : not requested (deterministic mode)")
    summary = advisory.plan.compute
    survivors = sum(1 for e in advisory.plan.evaluations if e.passed)
    if summary is not None and summary.backend == "modal":
        p(
            f"Modal              : stress-tested {survivors} valid routes "
            f"({summary.parallel_jobs} parallel jobs, {summary.simulated_trials:,} outcomes)"
        )
    elif summary is not None:
        fell_back = f" (fallback from {summary.fallback_from})" if summary.fallback_from else ""
        p(
            f"Local compute      : stress-tested {survivors} valid routes in-process"
            f"{fell_back} ({summary.simulated_trials:,} outcomes)"
        )
    p("Deterministic core : created the immutable plan · required human approval")
    p(
        "                     executed only the outstanding payout"
        + (" · reconciled" if reconciled else "")
    )


def _proof(
    p: _Printer,
    world: DemoWorld,
    *,
    planning_ms: float | None,
    plan: RecoveryPlan | None,
    advisory: AdvisoryOutcome | None,
    trace_id: str | None,
) -> None:
    state = world.service.get_transaction_state(TRANSACTION_ID)
    p.section("Proof")
    if advisory is not None:
        p(f"orchestrator             : {advisory.orchestrator}")
        if advisory.model:
            used = "" if advisory.orchestrator is Orchestrator.PYDANTIC_AI else " (not used)"
            p(f"model                    : {advisory.model}{used}")
        p(f"Gateway                  : {_gateway_line(advisory)}")
    p(f"recipient credited       : {'yes' if state.recipient_credited else 'no'}")
    p(f"sender debit count       : {state.sender_debit_count}")
    p(f"recipient credit count   : {state.recipient_credit_count}")
    p(f"duplicate sender debits  : {max(0, state.sender_debit_count - 1)}")
    p(f"funds located            : {state.funds_location}")
    p(f"state                    : {state.recovery_state}")
    summary = plan.compute if plan is not None else None
    if summary is None:
        p(f"compute backend          : {world.service.compute_backend} (configured)")
    elif summary.fallback_from:
        p(f"compute backend          : {summary.backend} (fallback from {summary.fallback_from})")
    else:
        p(f"compute backend          : {summary.backend}")
    if summary is not None and summary.parallel_jobs:
        p(f"parallel compute jobs    : {summary.parallel_jobs}")
    if summary is not None:
        p(f"simulated outcomes       : {summary.simulated_trials:,}")
    if plan is not None:
        rejected = sum(1 for e in plan.evaluations if not e.passed)
        p(
            f"candidate routes         : {len(plan.evaluations)} "
            f"({rejected} rejected by hard constraints)"
        )
    if planning_ms is not None:
        p(f"planning + advice latency: {planning_ms:.0f} ms")
    status = telemetry.status()
    if trace_id:
        p(f"trace id                 : {trace_id}")
    p(f"Logfire                  : {status.detail}")
    if status.project_url:
        p(f"Logfire project          : {status.project_url}")


def _gateway_line(advisory: AdvisoryOutcome) -> str:
    if advisory.orchestrator is Orchestrator.PYDANTIC_AI:
        if advisory.via_gateway:
            return f"enabled (route {advisory.gateway_route or 'default'})"
        return "disabled (direct model provider)"
    if advisory.orchestrator is Orchestrator.DETERMINISTIC_FALLBACK:
        return "not used (no successful model call)"
    return "n/a (no model)"


def _timeline(p: _Printer, world: DemoWorld) -> None:
    p.section("Audit timeline")
    for event in world.service.get_audit_timeline(TRANSACTION_ID):
        p(f"  {event.sequence:>2}. {event.actor:<15} {event.event_type:<26} {event.summary}")


def _ensure_utf8_stdout() -> None:
    encoding = (getattr(sys.stdout, "encoding", None) or "").lower().replace("-", "")
    if encoding != "utf8" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the seeded SK-10421 recovery (synthetic data)."
    )
    parser.add_argument("--approve", action="store_true", help="approve without prompting")
    parser.add_argument("--no-timeline", action="store_true", help="omit the audit timeline")
    parser.add_argument(
        "--agent",
        choices=("deterministic", "pydantic"),
        default=None,
        help="advice orchestrator (default: SIKARESCUE_AGENT_MODE, else deterministic)",
    )
    args = parser.parse_args(argv)
    _ensure_utf8_stdout()
    settings = get_settings()
    try:
        compute = build_compute_backend(
            settings.compute_backend,
            remote_timeout_seconds=settings.modal_timeout_seconds,
            shards_per_scenario=settings.modal_shards_per_scenario,
        )
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    import pydantic_ai

    pydantic_ai.BANNER_ENABLED = False  # keep the demo transcript clean
    telemetry.configure_telemetry(
        token=settings.logfire_token.get_secret_value() if settings.logfire_token else None,
        environment=settings.environment,
        capture_model_content=settings.logfire_capture_model_content,
    )
    world = build_demo_world(
        payout_latency_seconds=settings.payout_latency_seconds,
        payout_timeout_seconds=settings.payout_timeout_seconds,
        compute=compute,
        simulation=workload_config(
            settings.effective_workload,
            seed=settings.simulation_seed,
            trials_per_scenario=settings.simulation_trials_per_scenario,
        ),
    )
    advisor = RecoveryAdvisor(
        world.service,
        AdvisorConfig.from_settings(settings, mode=args.agent or settings.agent_mode),
        settings=settings,
    )
    try:
        outcome = asyncio.run(
            run_demo(
                world,
                auto_approve=args.approve,
                show_timeline=not args.no_timeline,
                advisor=advisor,
            )
        )
    finally:
        telemetry.flush()
    return outcome.exit_code
