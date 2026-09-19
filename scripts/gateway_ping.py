"""LIVE minimal Pydantic AI Gateway connectivity check (no agent, no tools, no Modal).

Sends through the configured Gateway route:
  1. text       - "Return the word READY."
  2. structured - one tool-call output using the same JSON-schema features RecoveryAdvice
                  relies on (const, pattern, enum), validated locally with Pydantic.
Reports only what was OBSERVED: destination URL, HTTP status, the model name the provider
reported, latency and token usage. Secrets are never printed.

  uv run python scripts/gateway_ping.py
  uv run python scripts/gateway_ping.py --model gateway/openai-chat:gemini-3.8-flash
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from typing import Literal

try:
    import pydantic_ai
    from pydantic import BaseModel, Field
    from pydantic_ai import ModelRequest, TextPart, ToolCallPart
    from pydantic_ai.direct import model_request
    from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError
    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.settings import ModelSettings
    from pydantic_ai.tools import ToolDefinition

    from sikarescue.agent.live import HeaderRecorder, preflight
    from sikarescue.agent.model import ModelUnavailableError, build_model, describe_model
    from sikarescue.config import Settings
    from sikarescue.models import RailId
except ImportError:
    sys.exit("Run inside the project environment:  uv run python scripts/gateway_ping.py")

_SECRETISH = re.compile(r"pylf_v\d+_\w+|AIza[0-9A-Za-z_-]{10,}|sk-[A-Za-z0-9_-]{10,}")


class ReadyCheck(BaseModel):
    word: Literal["READY"]
    approval_required: Literal[True]
    fee_gbp: str = Field(pattern=r"^\d+\.\d{2}$")
    route: RailId


def _safe(text: object, limit: int = 300) -> str:
    return _SECRETISH.sub("<redacted>", " ".join(str(text).split()))[:limit]


async def _send(model, prompt: str, params: ModelRequestParameters | None, timeout: float):
    started = time.perf_counter()
    response = await model_request(
        model,
        [ModelRequest.user_text_prompt(prompt)],
        model_settings=ModelSettings(max_tokens=512, timeout=timeout),
        model_request_parameters=params,
    )
    return response, time.perf_counter() - started


async def main(args: argparse.Namespace) -> int:
    settings = Settings()
    model_name = args.model or settings.agent_model
    lines, missing = preflight(settings, model_name)
    print("== Pydantic AI Gateway connectivity check ==")
    print("\n".join(lines))
    if missing or not model_name.startswith("gateway/"):
        why = ", ".join(missing) or "a gateway/<provider>:<model> model"
        print(f"\nNOT RUN: missing {why}. No request was sent.")
        return 2
    try:
        choice = describe_model(
            model_name, settings.gateway_route, base_url=settings.gateway_base_url
        )
        recorder = HeaderRecorder()
        model = build_model(choice, settings, http_client=recorder.client(args.timeout))
    except ModelUnavailableError as exc:
        print(f"\nNOT RUN: {exc}")
        return 2
    print(f"effective route          : {choice.route or '(provider default)'}")
    print(f"model identifier         : {model_name}  ({type(model).__name__})")

    checks: list[tuple[str, bool]] = []
    structured = ModelRequestParameters(
        output_mode="tool",
        output_tools=[
            ToolDefinition(
                name="final_result",
                description="Report readiness.",
                parameters_json_schema=ReadyCheck.model_json_schema(),
            )
        ],
        allow_text_output=False,
    )
    probes = [
        ("text", "Return the word READY.", None),
        (
            "structured",
            "Call final_result with word READY, approval_required true, fee_gbp 0.18 and "
            "route MOMO_B.",
            structured,
        ),
    ]
    for label, prompt, params in probes:
        recorder.responses.clear()
        recorder.destinations.clear()
        try:
            response, seconds = await _send(model, prompt, params, args.timeout)
        except ModelHTTPError as exc:
            print(f"\n{label:<10}: FAILED HTTP {exc.status_code}: {_safe(exc.body)}")
            print(f"            destination {recorder.destinations or '(no response)'}")
            checks.append((label, False))
            continue
        except ModelAPIError as exc:
            print(f"\n{label:<10}: FAILED {type(exc).__name__}: {_safe(exc)}")
            checks.append((label, False))
            continue
        status = recorder.responses[-1][0] if recorder.responses else None
        usage = response.usage
        if params is None:
            reply = "".join(p.content for p in response.parts if isinstance(p, TextPart))
            ok = "READY" in reply.upper()
            result = repr(_safe(reply, 40))
        else:
            calls = [p for p in response.parts if isinstance(p, ToolCallPart)]
            try:
                parsed = ReadyCheck.model_validate(calls[0].args_as_dict()) if calls else None
            except ValueError as exc:
                parsed, result = None, f"invalid tool args: {_safe(exc, 160)}"
            else:
                result = parsed.model_dump_json() if parsed else "no tool call returned"
            ok = parsed is not None
        print(
            f"\n{label:<10}: {'OK' if ok else 'UNEXPECTED'}  HTTP {status}  {seconds:.2f} s  "
            f"in={usage.input_tokens} out={usage.output_tokens}"
        )
        print(f"            reply       {result}")
        print(f"            provider model reported: {response.model_name!r}")
        print(
            f"            destination {recorder.destinations[-1] if recorder.destinations else '?'}"
        )
        checks.append((label, ok))
    print()
    for label, ok in checks:
        print(
            f"{'PASS' if ok else 'FAIL'}  {label} request through {choice.route or 'default route'}"
        )
    return 0 if checks and all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", help="override SIKARESCUE_AGENT_MODEL (gateway/... only)")
    parser.add_argument("--timeout", type=float, default=30.0, help="per-request seconds")
    pydantic_ai.BANNER_ENABLED = False
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(asyncio.run(main(parser.parse_args())))
