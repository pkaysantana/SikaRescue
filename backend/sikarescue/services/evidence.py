"""FailureVerifier: the deterministic authority over what provider evidence proves.

A model may PROPOSE what a messy provider payload means. It never decides. Every
safety-relevant fact is established here, independently, from trusted inputs only:

  * OUR client's own transport observations (was a response received, HTTP status);
  * a deterministic parser for the provider's documented response schema, reading the
    actual response body (disposition, reason code, phase, whether a transfer was created);
  * the provider's documented code catalog (synthetic integration spec).

The model's structured FailureEvidence is used for display and must be consistent with those
trusted facts (verbatim citations, exact transport claim, no contradiction of the parsed
body). It cannot unlock anything on its own: a DEFINITIVE_FAILED verdict requires every
trusted requirement to hold. The model's proposal of the same rejection is also required, as
corroboration, so the envelope-only fallback (no model) can never prove a definitive
failure. Anything short of that - a timeout, a lost acknowledgement, an unparseable body, an
undocumented code, an ungrounded or contradictory claim - is UNKNOWN.

    LLM may propose semantics. Trusted deterministic evidence decides whether money may move.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sikarescue.models import (
    AcceptanceStage,
    AttemptOutcome,
    EvidenceCheck,
    EvidenceCompleteness,
    EvidenceSource,
    EvidenceVerdict,
    ExtractedFailureEvidence,
    FailureEvidence,
    ProviderCodeKind,
    ProviderEvidenceCatalog,
    ProviderIncident,
    TransportOutcome,
)

REJECTED_DISPOSITION = "NOT_ACCEPTED"


def grounding_problems(
    payload: str, evidence: ExtractedFailureEvidence | FailureEvidence
) -> list[str]:
    """Every cited string that does NOT appear verbatim in the payload."""
    problems = [
        f"fragment not found verbatim in the payload: {fragment[:60]!r}"
        for fragment in evidence.evidence_fragments
        if fragment not in payload
    ]
    for name in ("provider_code", "provider_reference", "provider_message"):
        value = getattr(evidence, name)
        if value is not None and value not in payload:
            problems.append(f"{name} {value[:60]!r} does not appear verbatim in the payload")
    return problems


def observed_transport(incident: ProviderIncident) -> TransportOutcome:
    """What OUR client observed. Authoritative over anything read from the payload."""
    if incident.response_received:
        return TransportOutcome.RESPONSE_RECEIVED
    if incident.elapsed_ms >= incident.timeout_ms:
        return TransportOutcome.TIMEOUT
    return TransportOutcome.CONNECTION_LOST


# ------------------------------------------------------------------ trusted body parser


@dataclass(frozen=True)
class ParsedProviderResponse:
    """Facts read deterministically from the provider's response body. Never model output."""

    http_status: int
    disposition: str
    phase: str | None
    code: str
    transfer_created: bool


def parse_provider_response(incident: ProviderIncident) -> ParsedProviderResponse | None:
    """Parse the synthetic MOMO_A v3 disbursement response. Strict: anything else is None.

    Only called when OUR client observed a response. The HTTP status line must match the
    status our client recorded, and the JSON body must carry the documented fields.
    """
    if not incident.response_received or incident.http_status is None:
        return None
    lines = incident.raw_payload.split("\n")
    status_line = next((i for i, line in enumerate(lines) if line.startswith("HTTP/1.1 ")), None)
    if status_line is None:
        return None
    try:
        status = int(lines[status_line].split()[1])
        blank = lines.index("", status_line)  # end of the response headers
        body = json.loads("\n".join(lines[blank + 1 :]))
    except (ValueError, IndexError):
        return None
    if status != incident.http_status or not isinstance(body, dict):
        return None
    outcome = body.get("outcome")
    reason = outcome.get("reason") if isinstance(outcome, dict) else None
    if not isinstance(outcome, dict) or not isinstance(reason, dict) or "transfer" not in body:
        return None
    disposition, code, phase = outcome.get("disposition"), reason.get("code"), outcome.get("phase")
    if not isinstance(disposition, str) or not isinstance(code, str):
        return None
    return ParsedProviderResponse(
        http_status=status,
        disposition=disposition,
        phase=phase if isinstance(phase, str) else None,
        code=code,
        transfer_created=body["transfer"] is not None,
    )


# ------------------------------------------------------------------ fallback extraction


def envelope_evidence(incident: ProviderIncident) -> FailureEvidence:
    """Deterministic fallback extraction: our own transport observations only.

    It proposes nothing about the provider's body, so a definitive failure is never proven.
    """
    extraction = ExtractedFailureEvidence(
        provider=incident.rail_id.value,
        transport_outcome=observed_transport(incident),
        acceptance_stage=AcceptanceStage.UNKNOWN,
        completeness=EvidenceCompleteness.INSUFFICIENT,
    )
    return FailureEvidence.bind(incident, extraction, EvidenceSource.ENVELOPE_ONLY)


# ------------------------------------------------------------------ the verifier


def _check(name: str, passed: bool, ok: str, failed: str) -> EvidenceCheck:
    return EvidenceCheck(name=name, passed=passed, detail=(ok if passed else failed)[:240])


def _contradictions(
    incident: ProviderIncident, evidence: FailureEvidence, catalog: ProviderEvidenceCatalog | None
) -> list[str]:
    """The proposal's claims against each other and against the documented catalog."""
    found: list[str] = []
    stage = evidence.acceptance_stage
    if stage is AcceptanceStage.PRE_ACCEPTANCE and evidence.explicit_rejection is False:
        found.append("claims a pre-acceptance failure but also that nothing was rejected")
    if stage is AcceptanceStage.PRE_ACCEPTANCE and not incident.response_received:
        found.append("claims pre-acceptance, but no provider response was ever received")
    entry = catalog.lookup(evidence.provider_code) if catalog else None
    if entry is not None:
        kind = entry.kind
        if kind is ProviderCodeKind.PRE_ACCEPTANCE_REJECTION and (
            stage is AcceptanceStage.POST_ACCEPTANCE or evidence.explicit_rejection is False
        ):
            found.append(f"{entry.code} is a pre-acceptance rejection, contradicting the claims")
        if kind is ProviderCodeKind.SUCCESS and (
            stage is AcceptanceStage.PRE_ACCEPTANCE or evidence.explicit_rejection is True
        ):
            found.append(f"{entry.code} documents success, contradicting a rejection claim")
        if kind is ProviderCodeKind.POST_ACCEPTANCE_PENDING and (
            stage is AcceptanceStage.PRE_ACCEPTANCE
        ):
            found.append(f"{entry.code} documents acceptance, contradicting pre-acceptance")
    return found


def _disagreements(evidence: FailureEvidence, parsed: ParsedProviderResponse | None) -> list[str]:
    """Where the proposal contradicts what the trusted parser read from the body."""
    if parsed is None:
        return []
    found: list[str] = []
    if evidence.provider_code is not None and evidence.provider_code != parsed.code:
        found.append(f"proposes code {evidence.provider_code}, but the body says {parsed.code}")
    rejected = parsed.disposition == REJECTED_DISPOSITION
    if evidence.explicit_rejection is True and not rejected:
        found.append(f"proposes an explicit rejection, but the body says {parsed.disposition}")
    if evidence.acceptance_stage is AcceptanceStage.PRE_ACCEPTANCE and (
        not rejected or parsed.transfer_created
    ):
        found.append("proposes pre-acceptance, but the body does not show a rejection")
    return found


def verify_failure_evidence(
    incident: ProviderIncident,
    evidence: FailureEvidence,
    catalog: ProviderEvidenceCatalog | None,
) -> EvidenceVerdict:
    """Classify an incident. Pure and deterministic; fails closed to UNKNOWN."""
    payload = incident.raw_payload
    observed = observed_transport(incident)
    parsed = parse_provider_response(incident)
    ungrounded = grounding_problems(payload, evidence)
    contradictions = _contradictions(incident, evidence, catalog)
    disagreements = _disagreements(evidence, parsed)
    claimed = evidence.transport_outcome
    checks = (
        _check(
            "payload_binding",
            evidence.raw_payload_digest == incident.raw_payload_digest
            and evidence.incident_id == incident.incident_id,
            "evidence is bound to this exact payload (digest match)",
            "evidence is bound to a different payload",
        ),
        _check(
            "provider_match",
            evidence.provider == incident.rail_id.value,
            f"evidence names {incident.rail_id}, the provider we called",
            f"evidence names {evidence.provider!r}, but we called {incident.rail_id}",
        ),
        _check(
            "citations_grounded",
            not ungrounded,
            "every cited fragment, code, reference and message appears verbatim",
            ungrounded[0] if ungrounded else "",
        ),
        _check(
            "transport_consistent",
            claimed in (TransportOutcome.NOT_STATED, observed),
            f"transport claim matches our client's observation ({observed})",
            f"claims {claimed}, but our client observed {observed}",
        ),
        _check(
            "internally_consistent",
            not contradictions,
            "claims are consistent with each other and with the provider's code catalog",
            contradictions[0] if contradictions else "",
        ),
        _check(
            "matches_trusted_parse",
            not disagreements,
            "nothing proposed contradicts what the parser read from the response body",
            disagreements[0] if disagreements else "",
        ),
    )
    entry = catalog.lookup(parsed.code) if catalog and parsed else None
    rejected = parsed is not None and parsed.disposition == REJECTED_DISPOSITION
    requirements = (
        _check(
            "provider_response_received",
            incident.response_received,
            f"our client received the provider's response (HTTP {incident.http_status})",
            "no response to the payout request was received: acceptance cannot be excluded",
        ),
        _check(
            "trusted_body_parsed",
            parsed is not None,
            "the response body parsed against the provider's documented schema",
            "no response body could be parsed against the provider's documented schema",
        ),
        _check(
            "trusted_explicit_rejection",
            rejected,
            f"the body's disposition is {REJECTED_DISPOSITION}"
            + (f" ({parsed.phase})" if parsed and parsed.phase else ""),
            f"the body's disposition is {parsed.disposition}"
            if parsed
            else "no explicit rejection could be read from the body",
        ),
        _check(
            "trusted_pre_acceptance_code",
            entry is not None and entry.kind is ProviderCodeKind.PRE_ACCEPTANCE_REJECTION,
            f"the body's code {parsed.code if parsed else ''} is a documented pre-acceptance "
            "rejection",
            f"the body's code {parsed.code} is not a documented pre-acceptance rejection"
            if parsed
            else "no result code could be read from the body",
        ),
        _check(
            "trusted_no_transfer",
            parsed is not None and not parsed.transfer_created,
            "the body shows no transfer was created",
            "a transfer may have been created" if parsed else "transfer state unknown",
        ),
        _check(
            "extraction_corroborates",
            evidence.acceptance_stage is AcceptanceStage.PRE_ACCEPTANCE
            and evidence.explicit_rejection is True
            and parsed is not None
            and evidence.provider_code == parsed.code
            and bool(evidence.evidence_fragments),
            "the extraction independently proposes the same pre-acceptance rejection",
            "the extraction does not propose this pre-acceptance rejection",
        ),
    )
    integrity_ok = all(c.passed for c in checks)
    if integrity_ok and all(r.passed for r in requirements):
        assert entry is not None and parsed is not None
        classification = AttemptOutcome.DEFINITIVE_FAILED
        reasons: tuple[str, ...] = (
            f"{entry.code}: {entry.meaning}",
            "The body shows the instruction was rejected before acceptance and no transfer "
            "was created, so no value moved.",
        )
    elif (
        integrity_ok
        and parsed is not None
        and entry is not None
        and entry.kind is ProviderCodeKind.SUCCESS
        and parsed.transfer_created
    ):
        classification = AttemptOutcome.SUCCEEDED
        reasons = (
            f"{entry.code}: {entry.meaning}",
            "The provider reports the payout succeeded; it must be confirmed and booked by "
            "an operator, never from extracted evidence.",
        )
    else:
        classification = AttemptOutcome.UNKNOWN
        failed = [c.detail for c in (*checks, *requirements) if not c.passed]
        reasons = (
            *failed[:3],
            "Acceptance cannot be excluded, so the recipient may already have been credited.",
        )
    return EvidenceVerdict(
        incident_id=incident.incident_id,
        raw_payload_digest=incident.raw_payload_digest,
        evidence_digest=evidence.digest,
        classification=classification,
        checks=checks,
        requirements=requirements,
        reasons=reasons,
        catalog_meaning=entry.meaning if entry else None,
    )
