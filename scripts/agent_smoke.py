"""LIVE smoke test A + D: the Pydantic AI agent through Pydantic AI Gateway, traced in Logfire.

Runs the complete SK-10421 recovery with the agent path, then requires that:
  * the advice really came from the model (orchestrator = pydantic_ai, not a fallback);
  * the model call went through the Gateway (unless --allow-direct is given);
  * the transaction reconciled with exactly one sender debit and one recipient credit;
  * (with LOGFIRE_READ_TOKEN) the trace is actually queryable in the Logfire project.
Nothing is claimed that was not observed. Exit code 0 only if every check passed.

  uv run python scripts/agent_smoke.py
  uv run python scripts/agent_smoke.py --compute modal
  uv run python scripts/agent_smoke.py --model google:gemini-3.5-flash --allow-direct
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys

try:
    import pydantic_ai

    from sikarescue import telemetry
    from sikarescue.agent.advisor import AdvisorConfig, Orchestrator, RecoveryAdvisor
    from sikarescue.agent.live import HeaderRecorder, preflight, verify_trace_in_logfire
    from sikarescue.cli.demo import run_demo
    from sikarescue.compute.backend import build_compute_backend
    from sikarescue.compute.scenarios import workload_config
    from sikarescue.config import Settings
    from sikarescue.demo_data.sk10421 import build_demo_world
except ImportError:
    sys.exit("Run inside the project environment:  uv run python scripts/agent_smoke.py")

TRACE_SPANS = {
    "sikarescue_recovery",
    "agent_run_started",
    "invoke_agent recovery_agent",
    "agent_tool_evaluate_routes",
    "recovery_plan_created",
    "recovery_advice_generated",
    "reconciliation_completed",
}


async def main(args: argparse.Namespace) -> int:
    settings = Settings()
    model_name = args.model or settings.agent_model
    lines, missing = preflight(settings, model_name)
    print("== Live smoke: Pydantic AI agent via Gateway + Logfire ==")
    print("\n".join(lines))
    via_gateway = model_name.startswith("gateway/")
    if missing:
        print(f"\nNOT RUN: missing {', '.join(missing)}. No live call was attempted.")
        return 2
    if not via_gateway and not args.allow_direct:
        print("\nNOT RUN: model is not a gateway/... model (pass --allow-direct to test anyway).")
        return 2

    status = telemetry.configure_telemetry(
        token=settings.logfire_token.get_secret_value() if settings.logfire_token else None,
        environment=settings.environment,
    )
    print(f"Logfire                  : {status.detail}")
    compute = build_compute_backend(
        args.compute or settings.compute_backend,
        remote_timeout_seconds=settings.modal_timeout_seconds,
        shards_per_scenario=settings.modal_shards_per_scenario,
    )
    workload = "stress" if (args.compute or settings.compute_backend) == "modal" else "quick"
    world = build_demo_world(
        compute=compute, simulation=workload_config(workload, seed=settings.simulation_seed)
    )
    recorder = HeaderRecorder()
    advisor = RecoveryAdvisor(
        world.service,
        AdvisorConfig.from_settings(settings, mode="pydantic", model_name=model_name),
        settings=settings,
        http_client=recorder.client(settings.agent_request_timeout_seconds),
    )
    out = io.StringIO()
    outcome = await run_demo(
        world, auto_approve=True, out=out, show_timeline=False, advisor=advisor
    )
    telemetry.flush()
    if args.verbose:
        print(out.getvalue())
    advisory = outcome.advisory
    state = world.service.get_transaction_state("SK-10421")

    checks: list[tuple[str, bool, str]] = []
    real_model = advisory is not None and advisory.orchestrator is Orchestrator.PYDANTIC_AI
    checks.append(
        (
            "advice produced by the model",
            real_model,
            f"orchestrator={advisory.orchestrator if advisory else None}"
            + (f" ({advisory.fallback_reason})" if advisory and advisory.fallback_reason else ""),
        )
    )
    if real_model and advisory is not None and advisory.usage is not None:
        u = advisory.usage
        print(
            f"\nagent: {' -> '.join(advisory.tool_calls)} | {u.requests} requests, "
            f"{u.input_tokens:,} in / {u.output_tokens:,} out tokens, "
            f"{advisory.elapsed_seconds:.1f}s, {advisory.rejected_drafts} rejected drafts"
        )
        advice = advisory.advice
        print(f"advice recommends {advice.recommended_route}: {advice.operator_message}")
    if via_gateway:
        statuses = [code for code, _ in recorder.responses]
        checks.append(
            (
                "model calls went through Pydantic AI Gateway",
                bool(real_model and advisory and advisory.via_gateway),
                f"via_gateway={advisory.via_gateway if advisory else False}, "
                f"Gateway HTTP statuses seen={statuses}",
            )
        )
    else:
        print("\nNOTE: direct provider (--allow-direct): the Gateway was NOT tested in this run.")
    checks.append(
        (
            "recovery reconciled exactly once",
            state.recovery_state.value == "RECONCILED"
            and state.sender_debit_count == 1
            and state.recipient_credit_count == 1,
            f"state={state.recovery_state}, debits={state.sender_debit_count}, "
            f"credits={state.recipient_credit_count}",
        )
    )
    if recorder.gateway_headers():
        print(f"gateway response headers: {recorder.gateway_headers()}")
    if outcome.trace_id:
        print(f"trace id: {outcome.trace_id}")
        if status.exporting:
            ok, detail = await verify_trace_in_logfire(outcome.trace_id, TRACE_SPANS)
            checks.append(("trace visible in Logfire project", ok, detail))
        else:
            checks.append(("trace visible in Logfire project", False, "LOGFIRE_TOKEN not set"))

    print()
    for name, ok, detail in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name:<46} {detail}")
    return 0 if all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", help="override SIKARESCUE_AGENT_MODEL")
    parser.add_argument("--compute", choices=("local", "modal"), help="override compute backend")
    parser.add_argument("--allow-direct", action="store_true", help="permit a non-Gateway model")
    parser.add_argument("--verbose", action="store_true", help="print the full demo transcript")
    pydantic_ai.BANNER_ENABLED = False
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(asyncio.run(main(parser.parse_args())))
