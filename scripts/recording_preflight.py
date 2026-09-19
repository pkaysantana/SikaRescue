"""Recording preflight: run this, then start the server, then record. Fails cleanly, early.

1. Configuration: the same overrides the recording server uses (--agent pydantic --compute
   modal), then Gateway route `sr`, Gemini 3.8 Flash and credentials present (names only).
2. Live Gateway + verifier: the DEFINITIVE incident goes through Pydantic AI via Gateway route
   `sr`, and the deterministic verifier must independently prove the pre-acceptance failure.
3. Modal warm-up: the route simulation (stress workload) and the outage allocation function,
   so the recording never waits on a cold start or falls back.

  uv run python scripts/recording_preflight.py
  uv run python scripts/serve_demo.py --compute modal --agent pydantic --record
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

# The recording server is launched with these overrides; check exactly that configuration.
os.environ["SIKARESCUE_AGENT_MODE"] = "pydantic"
os.environ["SIKARESCUE_COMPUTE_BACKEND"] = "modal"

try:
    import pydantic_ai

    from sikarescue.agent.advisor import AdvisorConfig, Orchestrator
    from sikarescue.agent.evidence import EvidenceExtractor
    from sikarescue.agent.live import preflight, recording_problems
    from sikarescue.compute.backend import build_compute_backend
    from sikarescue.compute.scenarios import workload_config
    from sikarescue.config import Settings
    from sikarescue.demo_data.incidents import IncidentScenario
    from sikarescue.demo_data.outage import build_outage_analyzer
    from sikarescue.demo_data.sk10421 import TRANSACTION_ID, build_demo_world
    from sikarescue.models import AttemptOutcome
except ImportError:
    sys.exit("Run inside the project environment:  uv run python scripts/recording_preflight.py")

WARM_TIMEOUT_SECONDS = 90.0


def step(ok: bool, name: str, detail: str) -> bool:
    print(f"{'PASS' if ok else 'FAIL'}  {name:<34} {detail}")
    return ok


async def main() -> int:
    pydantic_ai.BANNER_ENABLED = False
    for stale in ("GOOGLE_API_KEY", "GEMINI_API_KEY"):
        os.environ.pop(stale, None)
    settings = Settings()
    print("== Recording preflight ==")
    lines, _ = preflight(settings)
    print("\n".join(lines))
    problems = recording_problems(settings)
    if not step(not problems, "recording configuration", "; ".join(problems) or "ready"):
        print("\nNOT READY. Nothing live was attempted.")
        return 2
    step(True, "agent mode", settings.agent_mode)
    step(True, "Gateway route", settings.gateway_route or "")
    step(True, "model", settings.agent_model)

    ok = True
    # --- live Gateway + deterministic verifier, on the definitive incident ------------------
    world = build_demo_world(incident=IncidentScenario.DEFINITIVE)
    aggregate = world.repository.get(world.transaction_id)
    assert aggregate.incident is not None
    started = time.perf_counter()
    extraction = await EvidenceExtractor(
        AdvisorConfig.from_settings(settings), settings=settings
    ).extract(aggregate.incident, aggregate.instruction)
    ok &= step(
        extraction.orchestrator is Orchestrator.PYDANTIC_AI,
        "Pydantic AI extraction",
        f"{extraction.orchestrator} via route {extraction.gateway_route}, provider reported "
        f"{extraction.provider_model} ({time.perf_counter() - started:.1f}s)"
        + (f"; fallback: {extraction.fallback_reason}" if extraction.fallback_reason else ""),
    )
    ok &= step(
        extraction.provider_model == "models/gemini-3.8-flash",
        "provider-reported model",
        str(extraction.provider_model),
    )
    classification = await world.service.classify_provider_incident(
        world.transaction_id, extraction.evidence
    )
    verdict = classification.verdict
    trusted = [r for r in verdict.requirements if r.name.startswith(("trusted_", "provider_"))]
    ok &= step(
        verdict.classification is AttemptOutcome.DEFINITIVE_FAILED
        and all(r.passed for r in trusted),
        "deterministic verifier",
        f"{verdict.classification}; trusted facts: "
        + "; ".join(r.detail for r in trusted if r.passed)[:160],
    )

    # --- Modal warm-up: route simulation + outage allocation -------------------------------
    compute = build_compute_backend(
        "modal",
        remote_timeout_seconds=WARM_TIMEOUT_SECONDS,
        shards_per_scenario=settings.modal_shards_per_scenario,
    )
    warm = build_demo_world(
        compute=compute, simulation=workload_config("stress", seed=settings.simulation_seed)
    )
    started = time.perf_counter()
    await warm.service.evaluate_recovery_routes(TRANSACTION_ID)
    route_run = getattr(compute, "last_failure", None)  # set only if Modal fell back
    ok &= step(
        route_run is None,
        "Modal route simulation warm",
        f"{time.perf_counter() - started:.1f}s"
        + (f"; fell back: {route_run}" if route_run else ""),
    )
    started = time.perf_counter()
    outage = await build_outage_analyzer("modal", timeout_seconds=WARM_TIMEOUT_SECONDS).run()
    ok &= step(
        outage.backend == "modal" and outage.fallback_from is None,
        "Modal outage allocation warm",
        f"{outage.backend}, {outage.parallel_jobs} jobs, {time.perf_counter() - started:.1f}s"
        + (f"; fell back: {outage.fallback_reason}" if outage.fallback_reason else ""),
    )
    print("\nREADY TO RECORD." if ok else "\nNOT READY: fix the FAIL lines before recording.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
