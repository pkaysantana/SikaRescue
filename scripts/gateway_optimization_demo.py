"""A/B: recovery decision-context optimisation, measured through Pydantic AI Gateway.

SAME model, SAME task, SAME core facts (a freshly seeded SK-10421 per run), SAME output
schema (RecoveryAdvice, validated against the deterministic plan). Only the evidence the
analysis tool hands the model differs:

  A  optimisation OFF: the naive dump of the same facts (whole journal records, the full plan
     with every evaluation and scenario parameter, the complete audit timeline);
  B  optimisation ON : the compact RecoveryDecisionContext (evidence-preserving: completed
     effects, obligation, failure, candidate + rejected routes with reasons, fees, simulated
     reliability/SLA, stress results, the selected plan and the no-replay invariant).

WHAT THIS IS / IS NOT. The optimisation is APPLICATION-SIDE context design, not a Gateway
feature: no request-path Gateway optimisation is described in the current Pydantic docs
(checked 2026-09-19). The Gateway's role here is transport + telemetry: both variants run
through the same Gateway route, and token counts are the provider-reported usage returned
through it. If your Gateway has a route-level optimisation configured, `--route-b SLUG`
sends variant B through that route instead, so its effect is measured on top.

  uv run python scripts/gateway_optimization_demo.py --offline        # payload bytes only
  uv run python scripts/gateway_optimization_demo.py --repeat 3       # live, through Gateway
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from dataclasses import dataclass

try:
    import pydantic_ai

    from sikarescue import telemetry
    from sikarescue.agent.advisor import AdvisorConfig, Orchestrator, RecoveryAdvisor
    from sikarescue.agent.live import HeaderRecorder, preflight
    from sikarescue.config import Settings
    from sikarescue.demo_data.sk10421 import TRANSACTION_ID, build_demo_world
    from sikarescue.services.model_boundary import (
        build_decision_context,
        build_verbose_decision_context,
    )
except ImportError:
    sys.exit(
        "Run inside the project environment:  uv run python scripts/gateway_optimization_demo.py"
    )

VARIANTS = (("A", "optimisation OFF", "verbose"), ("B", "optimisation ON ", "compact"))


@dataclass
class Run:
    variant: str
    valid: bool
    input_tokens: int | None
    output_tokens: int | None
    requests: int | None
    seconds: float
    rejected_drafts: int
    note: str


async def payload_bytes() -> dict[str, int]:
    world = build_demo_world()
    plan = await world.service.create_recovery_plan(TRANSACTION_ID)
    aggregate = world.repository.get(TRANSACTION_ID)
    return {
        "verbose": len(json.dumps(build_verbose_decision_context(aggregate, plan), default=str)),
        "compact": len(build_decision_context(aggregate, plan).model_dump_json()),
    }


async def run_once(settings: Settings, model: str, mode: str, route: str | None, label: str) -> Run:
    world = build_demo_world()  # identical seeded facts for every run
    recorder = HeaderRecorder()
    advisor = RecoveryAdvisor(
        world.service,
        AdvisorConfig.from_settings(
            settings, mode="pydantic", model_name=model, gateway_route=route, context_mode=mode
        ),
        settings=settings,
        http_client=recorder.client(settings.agent_request_timeout_seconds),
    )
    started = time.perf_counter()
    outcome = await advisor.advise(TRANSACTION_ID)
    seconds = time.perf_counter() - started
    usage = outcome.usage
    valid = outcome.orchestrator is Orchestrator.PYDANTIC_AI
    return Run(
        variant=label,
        valid=valid,
        input_tokens=usage.input_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None,
        requests=usage.requests if usage else None,
        seconds=seconds,
        rejected_drafts=outcome.rejected_drafts,
        note="" if valid else f"fallback: {outcome.fallback_reason}",
    )


def _median(values: list[int | float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return statistics.median(present) if present else None


async def main(args: argparse.Namespace) -> int:
    sizes = await payload_bytes()
    print("== Recovery decision-context optimisation: A/B ==")
    print(
        f"analysis payload bytes   : A (off) {sizes['verbose']:,}   B (on) {sizes['compact']:,}"
        f"   -> {1 - sizes['compact'] / sizes['verbose']:.0%} smaller, same decision facts"
    )
    if args.offline:
        print("offline: no model was called, so no token, latency or validity figures are shown.")
        return 0

    settings = Settings()
    model = args.model or settings.agent_model
    lines, missing = preflight(settings, model)
    print("\n".join(lines))
    if missing:
        print(f"\nNOT RUN: missing {', '.join(missing)}. No live call was attempted.")
        return 2
    if not model.startswith("gateway/") and not args.allow_direct:
        print("\nNOT RUN: model is not a gateway/... model (pass --allow-direct to test anyway).")
        return 2
    telemetry.configure_telemetry(
        token=settings.logfire_token.get_secret_value() if settings.logfire_token else None,
        environment=settings.environment,
    )
    runs: list[Run] = []
    for i in range(args.repeat):
        for letter, label, mode in VARIANTS:
            route = args.route_b if (letter == "B" and args.route_b) else settings.gateway_route
            run = await run_once(settings, model, mode, route, f"{letter} {label}")
            runs.append(run)
            print(
                f"  run {i + 1} {run.variant}: valid={run.valid} in={run.input_tokens} "
                f"out={run.output_tokens} requests={run.requests} {run.seconds:.1f}s {run.note}"
            )
    telemetry.flush()

    print("\nmedian over valid runs (provider-reported usage via the Gateway):")
    header = ("variant", "valid", "input tok", "output tok", "requests", "latency")
    print("{:<20}{:>7}{:>11}{:>12}{:>10}{:>10}".format(*header))
    medians = {}
    for letter, label, _ in VARIANTS:
        group = [r for r in runs if r.variant.startswith(letter)]
        ok = [r for r in group if r.valid]
        m = {
            "in": _median([r.input_tokens for r in ok]),
            "out": _median([r.output_tokens for r in ok]),
            "req": _median([r.requests for r in ok]),
            "sec": _median([r.seconds for r in ok]),
        }
        medians[letter] = m
        fmt = lambda v, spec: "n/a" if v is None else format(v, spec)  # noqa: E731
        print(
            f"{letter + ' ' + label:<20}{len(ok):>4}/{len(group):<2}{fmt(m['in'], ',.0f'):>11}"
            f"{fmt(m['out'], ',.0f'):>12}{fmt(m['req'], '.0f'):>10}{fmt(m['sec'], '.1f'):>9}s"
        )
    a, b = medians["A"], medians["B"]
    if a["in"] and b["in"]:
        print(f"\ninput tokens: B uses {1 - b['in'] / a['in']:.0%} fewer than A (median).")
    else:
        print("\nno valid pair of runs: no token comparison is claimed.")
    return 0 if all(r.valid for r in runs) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true", help="payload sizes only, no model")
    parser.add_argument("--repeat", type=int, default=1, help="runs per variant (default 1)")
    parser.add_argument("--model", help="override SIKARESCUE_AGENT_MODEL")
    parser.add_argument("--route-b", help="Gateway route slug for variant B only")
    parser.add_argument("--allow-direct", action="store_true", help="permit a non-Gateway model")
    pydantic_ai.BANNER_ENABLED = False
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(asyncio.run(main(parser.parse_args())))
