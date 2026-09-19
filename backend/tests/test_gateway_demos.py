"""Gateway demo plumbing, offline: the guardrail probe, its classifier, header capture, and
the live scripts' refusal to run (or to claim anything) without credentials.

The live behaviour itself is exercised by `scripts/agent_smoke.py`,
`scripts/gateway_optimization_demo.py` and `scripts/gateway_guardrail_demo.py`.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from sikarescue.agent.guardrail_probe import (
    GuardrailVerdict,
    build_control_probe,
    build_guardrail_probe,
    classify,
)
from sikarescue.agent.live import HeaderRecorder
from sikarescue.demo_data.sk10421 import (
    RECIPIENT_NAME,
    RECIPIENT_PHONE,
    RECIPIENT_REFERENCE,
)
from sikarescue.errors import PIILeakError
from sikarescue.services.model_boundary import assert_no_recipient_pii

REPO_ROOT = Path(__file__).resolve().parents[2]
CREDENTIAL_VARS = (
    "PYDANTIC_AI_GATEWAY_API_KEY",
    "PAIG_API_KEY",
    "LOGFIRE_TOKEN",
    "LOGFIRE_READ_TOKEN",
    "SIKARESCUE_GATEWAY_GUARDRAIL_ROUTE",
)


@pytest.fixture
def instruction(world, txn_id):
    return world.repository.get(txn_id).instruction


def test_probe_is_deliberately_pii_bearing_and_control_is_clean(instruction):
    probe = build_guardrail_probe(instruction)
    assert all(v in probe for v in (RECIPIENT_NAME, RECIPIENT_PHONE, RECIPIENT_REFERENCE))
    with pytest.raises(PIILeakError):
        assert_no_recipient_pii(probe, instruction)  # the production gate would refuse it
    assert_no_recipient_pii(build_control_probe(instruction), instruction)


def test_production_path_never_loads_the_probe():
    code = (
        "import sys, sikarescue.cli.demo, sikarescue.agent.advisor; "
        "print('sikarescue.agent.guardrail_probe' in sys.modules)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120
    )
    assert result.stdout.strip() == "False", result.stderr


@pytest.mark.parametrize(
    ("echo", "status", "headers", "control_ok", "verdict"),
    [
        (None, 403, {}, True, GuardrailVerdict.BLOCKED),
        (None, 422, {"x-guardrail": "phone"}, True, GuardrailVerdict.BLOCKED),
        (None, 401, {}, True, GuardrailVerdict.INCONCLUSIVE),  # auth, not a guardrail
        (
            "name: [REDACTED]\nphone: [PHONE_NUMBER]\nreference: [REDACTED]",
            200,
            {},
            True,
            GuardrailVerdict.REDACTED,
        ),
        ("name: MISSING\nphone: MISSING", 200, {"x-protections-fired": "pii"}, True, "REDACTED"),
        ("I can't help with that.", 200, {}, True, GuardrailVerdict.INCONCLUSIVE),
        (
            f"name: {RECIPIENT_NAME}\nphone: +233205550142\nreference: x",
            200,
            {},
            True,
            GuardrailVerdict.NOT_PROTECTED,
        ),
        ("anything", 200, {}, False, GuardrailVerdict.CONTROL_FAILED),
    ],
    ids=[
        "blocked",
        "blocked-with-header",
        "auth-error",
        "masked",
        "header",
        "declined",
        "leaked",
        "control-failed",
    ],
)
def test_guardrail_classification_is_strictly_observational(
    instruction, echo, status, headers, control_ok, verdict
):
    observation = classify(
        instruction=instruction,
        echo=echo,
        probe_status=status,
        guardrail_headers=headers,
        control_ok=control_ok,
    )
    assert observation.verdict == verdict
    if verdict == GuardrailVerdict.NOT_PROTECTED:
        assert observation.leaked == ("name", "phone")


def test_header_recorder_keeps_gateway_headers_and_drops_credentials():
    recorder = HeaderRecorder()
    response = SimpleNamespace(
        status_code=200,
        headers={
            "Authorization": "Bearer secret",
            "Set-Cookie": "session=1",
            "X-Request-Id": "abc",
            "x-pydantic-guardrails": "phone:redact",
            "content-type": "application/json",
        },
        request=SimpleNamespace(
            url=SimpleNamespace(
                scheme="https",
                host="gateway-eu.pydantic.dev",
                path="/proxy/sikarescue-gemini/chat/completions",
            )
        ),
    )
    asyncio.run(recorder(response))
    assert recorder.gateway_headers() == {
        "x-request-id": "abc",
        "x-pydantic-guardrails": "phone:redact",
    }
    assert recorder.guardrail_headers() == {"x-pydantic-guardrails": "phone:redact"}
    # Where the request really went is recorded (no query string, never credentials).
    assert recorder.destinations == [
        "https://gateway-eu.pydantic.dev/proxy/sikarescue-gemini/chat/completions"
    ]


def _script(name: str, *args: str, tmp_path: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in CREDENTIAL_VARS}
    env |= {
        "PYTHONIOENCODING": "utf-8",
        "SIKARESCUE_PAYOUT_LATENCY_SECONDS": "0",
        "SIKARESCUE_AGENT_MODEL": "gateway/anthropic:claude-haiku-4-5",
        "SIKARESCUE_COMPUTE_BACKEND": "local",
    }
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / name), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=tmp_path,  # no .env is picked up
        timeout=180,
    )


@pytest.mark.parametrize(
    "script",
    [
        "agent_smoke.py",
        "gateway_ping.py",
        "gateway_guardrail_demo.py",
        "gateway_optimization_demo.py",
    ],
)
def test_live_scripts_refuse_without_credentials(script, tmp_path):
    result = _script(script, tmp_path=tmp_path)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "NOT RUN: missing PYDANTIC_AI_GATEWAY_API_KEY" in result.stdout
    assert "PASS" not in result.stdout


def test_optimization_demo_offline_reports_payload_sizes_only(tmp_path):
    result = _script("gateway_optimization_demo.py", "--offline", tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "smaller, same decision facts" in result.stdout
    assert "no token, latency or validity figures" in result.stdout


def test_cli_agent_flag_without_gateway_key_falls_back_and_reconciles(tmp_path):
    result = _script(
        "demo_recovery.py", "--approve", "--no-timeline", "--agent", "pydantic", tmp_path=tmp_path
    )
    assert result.returncode == 0, result.stderr
    assert "orchestrator             : deterministic_fallback" in result.stdout
    assert "missing PYDANTIC_AI_GATEWAY_API_KEY" in result.stdout
    assert "state                    : RECONCILED" in result.stdout
    assert "trace id                 : " in result.stdout
