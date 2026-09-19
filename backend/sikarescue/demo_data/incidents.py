"""Synthetic MOMO_A provider incidents for SK-10421, and MOMO_A's documented result codes.

Two scenarios, both clearly synthetic and free of personal data (recipient token only):

DEFINITIVE  HTTP 200 with top-level "status": "COMPLETED", which a status-code reader would take
            as success. The body shows the instruction was rejected during validation, before
            it was queued, under a documented pre-acceptance code (MA-4017).
UNKNOWN     The request was fully sent and acknowledged at TCP level, then no response arrived
            before the client's read timeout. A later status probe hit an edge proxy 504. No
            evidence can exclude that the provider accepted and credited the payout.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from sikarescue.models import (
    Money,
    ProviderCodeEntry,
    ProviderCodeKind,
    ProviderEvidenceCatalog,
    ProviderIncident,
    RailId,
    sha256_text,
)


class IncidentScenario(StrEnum):
    DEFINITIVE = "definitive"
    UNKNOWN = "unknown"


SCENARIO_LABELS = {
    IncidentScenario.DEFINITIVE: "Definitive failure",
    IncidentScenario.UNKNOWN: "Unknown outcome",
}

INCIDENT_ID = "inc_000000010421"
TIMEOUT_MS = 30_000


def momo_a_catalog() -> ProviderEvidenceCatalog:
    """MOMO_A's documented result codes (synthetic integration spec)."""
    pre = ProviderCodeKind.PRE_ACCEPTANCE_REJECTION
    return ProviderEvidenceCatalog(
        rail_id=RailId.MOMO_A,
        entries=(
            ProviderCodeEntry(
                code="MA-4017",
                kind=pre,
                meaning="beneficiary wallet not provisioned for inbound transfers; "
                "rejected at validation, never queued",
            ),
            ProviderCodeEntry(
                code="MA-4221", kind=pre, meaning="instruction failed schema validation"
            ),
            ProviderCodeEntry(
                code="MA-4290", kind=pre, meaning="rate limited; instruction not accepted"
            ),
            ProviderCodeEntry(
                code="MA-2001", kind=ProviderCodeKind.SUCCESS, meaning="transfer completed"
            ),
            ProviderCodeEntry(
                code="MA-5020",
                kind=ProviderCodeKind.POST_ACCEPTANCE_PENDING,
                meaning="accepted and queued; wallet credit pending",
            ),
            ProviderCodeEntry(
                code="MA-5040",
                kind=ProviderCodeKind.POST_ACCEPTANCE_PENDING,
                meaning="accepted; downstream wallet system timed out",
            ),
        ),
    )


def _client_log(attempt_id: str, key: str, amount: Money, token: str) -> str:
    return (
        "# SikaRescue payout client - SYNTHETIC incident capture (no real provider)\n"
        f"attempt_id: {attempt_id}\n"
        "rail: MOMO_A (synthetic mobile-money provider)\n"
        f"idempotency_key: {key}\n"
        f"request: POST /v3/disbursements amount={amount.amount} {amount.currency.value} "
        f"beneficiary={token}\n"
    )


def _definitive_payload(attempt_id: str, key: str, amount: Money, token: str) -> str:
    return _client_log(attempt_id, key, amount, token) + (
        "client: request fully sent; response received after 412 ms (read timeout 30000 ms)\n"
        "\n"
        "HTTP/1.1 200 OK\n"
        "content-type: application/json\n"
        "x-momo-a-trace: 7c1f9a20-e5d1\n"
        "x-api-version: 3.4\n"
        "\n"
        "{\n"
        '  "status": "COMPLETED",\n'
        '  "request_ref": "mareq_5d02b7c1",\n'
        '  "outcome": {\n'
        '    "disposition": "NOT_ACCEPTED",\n'
        '    "phase": "pre-queue validation",\n'
        '    "reason": {\n'
        '      "code": "MA-4017",\n'
        '      "text": "Beneficiary wallet not provisioned for inbound transfers. Instruction '
        'rejected during validation; it was not queued and no funds were reserved or moved."\n'
        "    }\n"
        "  },\n"
        '  "transfer": null,\n'
        '  "processed_at": "2026-09-19T09:31:22Z"\n'
        "}\n"
    )


def _unknown_payload(attempt_id: str, key: str, amount: Money, token: str) -> str:
    return _client_log(attempt_id, key, amount, token) + (
        "client: request fully sent at 09:31:20.114Z; TCP acknowledged by provider edge\n"
        "client: no response bytes received before read timeout (30000 ms)\n"
        "client: connection closed by client after timeout\n"
        "\n"
        "status probe 09:31:52Z: GET /v3/disbursements?client_ref=<idempotency_key>\n"
        "HTTP/1.1 504 Gateway Timeout\n"
        "content-type: text/html\n"
        "server: edge-proxy\n"
        "\n"
        "<html><body><h1>504 Gateway Timeout</h1><p>The upstream service did not respond in "
        "time. Your request may or may not have been processed.</p></body></html>\n"
    )


def build_incident(
    scenario: IncidentScenario,
    *,
    transaction_id: str,
    attempt_id: str,
    idempotency_key: str,
    amount: Money,
    recipient_token: str,
    dispatched_at: datetime,
) -> ProviderIncident:
    definitive = scenario is IncidentScenario.DEFINITIVE
    build = _definitive_payload if definitive else _unknown_payload
    payload = build(attempt_id, idempotency_key, amount, recipient_token)
    return ProviderIncident(
        incident_id=INCIDENT_ID,
        transaction_id=transaction_id,
        rail_id=RailId.MOMO_A,
        attempt_id=attempt_id,
        idempotency_key=idempotency_key,
        amount=amount,
        dispatched_at=dispatched_at,
        request_fully_sent=True,
        response_received=definitive,
        http_status=200 if definitive else None,
        elapsed_ms=412 if definitive else TIMEOUT_MS,
        timeout_ms=TIMEOUT_MS,
        raw_payload=payload,
        raw_payload_digest=sha256_text(payload),
    )


def observed_at(incident: ProviderIncident) -> datetime:
    return incident.dispatched_at + timedelta(milliseconds=incident.elapsed_ms)
