"""LIVE Gateway guardrail demonstration with SYNTHETIC PII (Ama Mensah, +233 20 555 0142,
GH-9821-SYNTH-0042 are seeded test values, not a real person).

The production agent never sends these: it only sends allowlisted DTOs. This script
deliberately sends them to ONE Gateway route on which you have enabled a guardrail
(Logfire -> Gateway: prebuilt phone-number protection plus custom regex for the name and
reference, action redact or block), then reports only what it observes:

  1. control request (PII already removed) must succeed, proving the route works;
  2. probe request (synthetic PII) is sent; the model is asked to echo the fields back;
  3. verdict: BLOCKED (Gateway refused it), REDACTED (echo masked / guardrail header seen),
     NOT_PROTECTED (the provider echoed the PII) or INCONCLUSIVE.
Client-side tracing records metadata only (no prompt content) for this run.

  uv run python scripts/gateway_guardrail_demo.py --route <guardrail-route-slug>
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

try:
    import pydantic_ai
    from pydantic_ai import ModelRequest, ModelResponse, TextPart
    from pydantic_ai.direct import model_request
    from pydantic_ai.exceptions import ModelHTTPError
    from pydantic_ai.settings import ModelSettings

    from sikarescue import telemetry
    from sikarescue.agent.guardrail_probe import (
        PROBE_INSTRUCTIONS,
        GuardrailVerdict,
        build_control_probe,
        build_guardrail_probe,
        classify,
    )
    from sikarescue.agent.live import HeaderRecorder, preflight
    from sikarescue.agent.model import build_model, describe_model
    from sikarescue.config import Settings
    from sikarescue.demo_data.sk10421 import TRANSACTION_ID, build_demo_world
except ImportError:
    sys.exit("Run inside the project environment:  uv run python scripts/gateway_guardrail_demo.py")


async def ask(model, prompt: str, recorder: HeaderRecorder) -> tuple[str | None, int | None]:
    request = ModelRequest.user_text_prompt(prompt, instructions=PROBE_INSTRUCTIONS)
    try:
        response: ModelResponse = await model_request(
            model, [request], model_settings=ModelSettings(max_tokens=200, timeout=30)
        )
    except ModelHTTPError as exc:
        return None, exc.status_code
    text = "".join(p.content for p in response.parts if isinstance(p, TextPart))
    status = recorder.responses[-1][0] if recorder.responses else None
    return text, status


async def main(args: argparse.Namespace) -> int:
    settings = Settings()
    route = args.route or os.getenv("SIKARESCUE_GATEWAY_GUARDRAIL_ROUTE")
    model_name = args.model or settings.agent_model
    lines, missing = preflight(settings, model_name)
    print("== Gateway guardrail demo (SYNTHETIC PII) ==")
    print("\n".join(lines))
    print(f"guardrail route          : {route or 'NOT set'}")
    if missing or not route or not model_name.startswith("gateway/"):
        steps = [*missing]
        if not route:
            steps.append(
                "--route <slug> or SIKARESCUE_GATEWAY_GUARDRAIL_ROUTE (a route with a guardrail)"
            )
        if not model_name.startswith("gateway/"):
            steps.append("a gateway/<provider>:<model> model")
        print(f"\nNOT RUN: missing {'; '.join(steps)}. No request was sent.")
        return 2

    # Metadata-only client tracing for this run: the probe's prompt is never recorded here.
    telemetry.configure_telemetry(
        token=settings.logfire_token.get_secret_value() if settings.logfire_token else None,
        environment=settings.environment,
        capture_model_content=False,
    )
    instruction = build_demo_world().repository.get(TRANSACTION_ID).instruction
    recorder = HeaderRecorder()
    model = build_model(describe_model(model_name, route), settings, http_client=recorder.client())

    with telemetry.span("gateway_guardrail_demo", gateway_route=route, model=model_name) as span:
        control_echo, control_status = await ask(model, build_control_probe(instruction), recorder)
        control = "ok" if control_echo else "FAILED"
        print(f"\n1. control (no PII)   : HTTP {control_status} -> {control}")
        recorder.responses.clear()
        echo, probe_status = await ask(model, build_guardrail_probe(instruction), recorder)
        print(f"2. probe (synthetic PII): HTTP {probe_status}")
        guard_headers = recorder.guardrail_headers()
        if recorder.gateway_headers():
            print(f"   gateway headers     : {recorder.gateway_headers()}")
        observation = classify(
            instruction=instruction,
            echo=echo,
            probe_status=probe_status,
            guardrail_headers=guard_headers,
            control_ok=control_echo is not None,
        )
        span.set(verdict=observation.verdict.value, probe_status=probe_status)
    telemetry.flush()
    if echo is not None and observation.verdict is not GuardrailVerdict.NOT_PROTECTED:
        print(f"   model echo          : {' | '.join(echo.strip().splitlines())[:200]}")
    elif echo is not None:
        print(f"   model echo contained: {', '.join(observation.leaked)} (synthetic values)")
    print(f"\n3. verdict: {observation.verdict}: {observation.detail}")
    return 0 if observation.verdict in (GuardrailVerdict.BLOCKED, GuardrailVerdict.REDACTED) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--route", help="Gateway route slug with a guardrail configured")
    parser.add_argument("--model", help="override SIKARESCUE_AGENT_MODEL (must be gateway/...)")
    pydantic_ai.BANNER_ENABLED = False
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(asyncio.run(main(parser.parse_args())))
