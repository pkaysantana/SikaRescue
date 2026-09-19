"""Helpers for the LIVE smoke scripts in `scripts/` (never used by the unit suite).

They report configuration by NAME only (never values), record Gateway response headers so a
guardrail or routing decision is observable, and verify from Logfire itself that a trace
arrived, instead of assuming it did.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sikarescue.agent.model import describe_model, missing_configuration
from sikarescue.config import Settings

# Response headers worth showing (Gateway/guardrail/usage metadata). Never auth or cookies.
_HEADER_HINTS = ("guard", "protect", "dlp", "redact", "pydantic", "logfire", "gateway", "cost")
_HIDDEN_HEADERS = ("authorization", "cookie", "set-cookie", "api-key", "x-api-key")


def preflight(settings: Settings, model_name: str | None = None) -> tuple[list[str], list[str]]:
    """(report lines, missing setup steps) for the configured model and telemetry."""
    name = model_name or settings.agent_model
    choice = describe_model(name, settings.gateway_route)
    lines = [
        f"model                    : {choice.label}",
        "PYDANTIC_AI_GATEWAY_API_KEY: " + ("set" if settings.gateway_api_key else "NOT set"),
        "PYDANTIC_AI_GATEWAY_BASE_URL: "
        + (settings.gateway_base_url or "(inferred from the key's region)"),
        f"SIKARESCUE_GATEWAY_ROUTE : {settings.gateway_route or '(provider default route)'}",
        "LOGFIRE_TOKEN            : " + ("set" if settings.logfire_token else "NOT set"),
        "LOGFIRE_READ_TOKEN       : "
        + ("set" if os.getenv("LOGFIRE_READ_TOKEN") else "NOT set (trace arrival unverifiable)"),
    ]
    missing = missing_configuration(choice, settings)
    return lines, missing


@dataclass
class HeaderRecorder:
    """httpx/httpx2 response hook: keeps status + selected headers of every model response."""

    responses: list[tuple[int, dict[str, str]]] = field(default_factory=list)

    async def __call__(self, response: Any) -> None:
        headers = {
            k.lower(): v
            for k, v in response.headers.items()
            if k.lower() not in _HIDDEN_HEADERS
            and (k.lower().startswith("x-") or any(h in k.lower() for h in _HEADER_HINTS))
        }
        self.responses.append((response.status_code, headers))

    def client(self, timeout_seconds: float = 30.0) -> Any:
        import httpx2  # the HTTP client Pydantic AI's Gateway provider uses

        return httpx2.AsyncClient(event_hooks={"response": [self]}, timeout=timeout_seconds)

    def gateway_headers(self) -> dict[str, str]:
        merged: dict[str, str] = {}
        for _, headers in self.responses:
            merged.update(headers)
        return merged

    def guardrail_headers(self) -> dict[str, str]:
        """Only headers whose NAME says they report protections (not e.g. request ids)."""
        return {
            k: v
            for k, v in self.gateway_headers().items()
            if any(h in k for h in ("guard", "protect", "dlp", "redact"))
        }


async def verify_trace_in_logfire(
    trace_id: str, expected_spans: set[str], *, wait_seconds: float = 30.0
) -> tuple[bool, str]:
    """Query Logfire for the trace. Needs LOGFIRE_READ_TOKEN; never assumes success."""
    read_token = os.getenv("LOGFIRE_READ_TOKEN")
    if not read_token:
        return (
            False,
            "not verified: LOGFIRE_READ_TOKEN is not set (Logfire -> Settings -> Read tokens)",
        )
    from logfire.query_client import AsyncLogfireQueryClient

    sql = f"SELECT span_name FROM records WHERE trace_id = '{trace_id}'"  # hex id, not user input
    deadline = asyncio.get_running_loop().time() + wait_seconds
    names: set[str] = set()
    async with AsyncLogfireQueryClient(read_token=read_token) as client:
        while True:
            result = await client.query_json_rows(
                sql, min_timestamp=datetime.now(UTC) - timedelta(hours=1)
            )
            names = {row["span_name"] for row in result["rows"]}
            if expected_spans <= names or asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(3)
    missing = expected_spans - names
    if not names:
        return False, f"trace {trace_id} NOT found in Logfire after {wait_seconds:g}s"
    if missing:
        return False, f"trace found ({len(names)} span names) but missing: {sorted(missing)}"
    return True, f"trace {trace_id} found in Logfire with all {len(expected_spans)} expected spans"
