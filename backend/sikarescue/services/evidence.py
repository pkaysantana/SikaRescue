"""FailureVerifier: the deterministic authority over what provider evidence proves.

A model may EXTRACT evidence from a messy provider payload; only this module decides what that
evidence proves, and it fails closed. Its inputs are:

  * the incident: the verbatim payload plus OUR client's own transport observations
    (was the request sent, did a response arrive, HTTP status) - never model output;
  * the extracted FailureEvidence, bound to the payload by digest;
  * the provider's documented code catalog (synthetic integration spec).

Every cited string must appear verbatim in the payload, the transport claim must match what
our client observed, and the claims must be consistent with each other and with the catalog.
DEFINITIVE_FAILED additionally requires an explicit, documented pre-acceptance rejection in a
response the provider actually sent. Anything less - a timeout, a lost acknowledgement, a
missing or unknown code, an ungrounded or contradictory claim - is UNKNOWN.
"""

from __future__ import annotations

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


def envelope_evidence(incident: ProviderIncident) -> FailureEvidence:
    """Deterministic fallback extraction: our own transport observations only.

    It never interprets the provider's body, so it can never prove a definitive failure.
    """
    extraction = ExtractedFailureEvidence(
        provider=incident.rail_id.value,
        transport_outcome=observed_transport(incident),
        acceptance_stage=AcceptanceStage.UNKNOWN,
        completeness=EvidenceCompleteness.INSUFFICIENT,
    )
    return FailureEvidence.bind(incident, extraction, EvidenceSource.ENVELOPE_ONLY)


def _check(name: str, passed: bool, ok: str, failed: str) -> EvidenceCheck:
    return EvidenceCheck(name=name, passed=passed, detail=(ok if passed else failed)[:240])


def _contradictions(
    incident: ProviderIncident, evidence: FailureEvidence, catalog: ProviderEvidenceCatalog | None
) -> list[str]:
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


def verify_failure_evidence(
    incident: ProviderIncident,
    evidence: FailureEvidence,
    catalog: ProviderEvidenceCatalog | None,
) -> EvidenceVerdict:
    """Classify an incident from extracted evidence. Pure and deterministic; fails closed."""
    payload = incident.raw_payload
    observed = observed_transport(incident)
    ungrounded = grounding_problems(payload, evidence)
    contradictions = _contradictions(incident, evidence, catalog)
    claimed = evidence.transport_outcome
    transport_consistent = claimed is TransportOutcome.NOT_STATED or (
        (claimed is TransportOutcome.RESPONSE_RECEIVED) == incident.response_received
    )
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
            transport_consistent,
            f"transport claim is consistent with our client's observation ({observed})",
            f"claims {claimed}, but our client observed {observed}",
        ),
        _check(
            "internally_consistent",
            not contradictions,
            "claims are consistent with each other and with the provider's code catalog",
            contradictions[0] if contradictions else "",
        ),
    )
    entry = catalog.lookup(evidence.provider_code) if catalog else None
    requirements = (
        _check(
            "provider_response_received",
            incident.response_received,
            f"our client received the provider's response (HTTP {incident.http_status})",
            "no response to the payout request was received: acceptance cannot be excluded",
        ),
        _check(
            "explicit_pre_acceptance_rejection",
            evidence.acceptance_stage is AcceptanceStage.PRE_ACCEPTANCE
            and evidence.explicit_rejection is True,
            "the provider explicitly rejected the instruction before accepting it",
            "no explicit rejection before acceptance is evidenced",
        ),
        _check(
            "documented_rejection_code",
            entry is not None and entry.kind is ProviderCodeKind.PRE_ACCEPTANCE_REJECTION,
            f"{evidence.provider_code} is a documented pre-acceptance rejection code",
            (
                f"{evidence.provider_code} is not a documented pre-acceptance rejection"
                if evidence.provider_code
                else "no provider result code is evidenced"
            ),
        ),
        _check(
            "cited_evidence",
            bool(evidence.evidence_fragments),
            f"{len(evidence.evidence_fragments)} verbatim fragment(s) cited",
            "no verbatim evidence was cited",
        ),
    )
    integrity_ok = all(c.passed for c in checks)
    if integrity_ok and all(r.passed for r in requirements):
        assert entry is not None
        classification = AttemptOutcome.DEFINITIVE_FAILED
        reasons: tuple[str, ...] = (
            f"{entry.code}: {entry.meaning}",
            "The provider rejected the instruction before accepting it, so no value moved.",
        )
    elif (
        integrity_ok
        and incident.response_received
        and entry is not None
        and entry.kind is ProviderCodeKind.SUCCESS
        and evidence.acceptance_stage is AcceptanceStage.POST_ACCEPTANCE
        and evidence.explicit_rejection is False
        and evidence.provider_reference is not None
        and evidence.evidence_fragments
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
