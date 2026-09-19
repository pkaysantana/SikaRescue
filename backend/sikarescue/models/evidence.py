"""Provider incident evidence: raw synthetic payloads, extracted FailureEvidence, verdicts.

The pipeline is deliberately split so no model output can authorise value movement:

    ProviderIncident (raw payload + our own transport observations)
      -> ExtractedFailureEvidence   (Pydantic AI: structure + verbatim citations, nothing else)
      -> FailureEvidence            (bound to the exact payload by its digest)
      -> EvidenceVerdict            (deterministic FailureVerifier; fails closed to UNKNOWN)

Only the verdict can decide an attempt's outcome, and only the deterministic core records it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

from sikarescue.models.common import DomainModel, EntityId, Money, Sha256Hex, TransactionId
from sikarescue.models.enums import AttemptOutcome, FailureStage, RailId


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TransportOutcome(StrEnum):
    RESPONSE_RECEIVED = "RESPONSE_RECEIVED"  # the provider answered the payout request
    TIMEOUT = "TIMEOUT"  # request sent, no answer within the client's window
    CONNECTION_LOST = "CONNECTION_LOST"  # connection dropped after the request was sent
    NOT_STATED = "NOT_STATED"


class AcceptanceStage(StrEnum):
    PRE_ACCEPTANCE = "PRE_ACCEPTANCE"  # rejected before the provider accepted/queued it
    POST_ACCEPTANCE = "POST_ACCEPTANCE"  # the provider accepted/queued/processed it
    UNKNOWN = "UNKNOWN"


class EvidenceCompleteness(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"


class EvidenceSource(StrEnum):
    PYDANTIC_AI = "pydantic_ai"
    # Fallback when no model is used: reads only our own transport observations, never the
    # provider's body, so it can never prove a definitive failure (fails closed to UNKNOWN).
    ENVELOPE_ONLY = "deterministic_envelope"


class ProviderCodeKind(StrEnum):
    PRE_ACCEPTANCE_REJECTION = "PRE_ACCEPTANCE_REJECTION"  # definitive: no value moved
    SUCCESS = "SUCCESS"
    POST_ACCEPTANCE_PENDING = "POST_ACCEPTANCE_PENDING"  # accepted; outcome not final


class ProviderCodeEntry(DomainModel):
    code: str = Field(min_length=1, max_length=40)
    kind: ProviderCodeKind
    meaning: str = Field(max_length=160)


class ProviderEvidenceCatalog(DomainModel):
    """The provider's documented result codes (synthetic integration spec, not model output)."""

    rail_id: RailId
    entries: tuple[ProviderCodeEntry, ...]

    def lookup(self, code: str | None) -> ProviderCodeEntry | None:
        return next((e for e in self.entries if e.code == code), None) if code else None


class ProviderIncident(DomainModel):
    """A payout request our client dispatched, and exactly what came back (synthetic).

    `request_fully_sent`, `response_received`, `http_status` and the timings are OUR client's
    own observations (deterministic). `raw_payload` is the provider's opaque answer plus our
    client log, verbatim; it carries the recipient token only, never personal data.
    """

    incident_id: EntityId
    transaction_id: TransactionId
    rail_id: RailId
    attempt_id: EntityId
    idempotency_key: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    amount: Money
    dispatched_at: datetime
    request_fully_sent: bool
    response_received: bool
    http_status: int | None = Field(default=None, ge=100, le=599)
    elapsed_ms: int = Field(ge=0)
    timeout_ms: int = Field(gt=0)
    raw_payload: str = Field(min_length=1, max_length=6000)
    raw_payload_digest: Sha256Hex
    synthetic: Literal[True] = True

    @model_validator(mode="after")
    def _check(self) -> ProviderIncident:
        if self.raw_payload_digest != sha256_text(self.raw_payload):
            raise ValueError("raw_payload_digest does not match raw_payload")
        if not self.response_received and self.http_status is not None:
            raise ValueError("no response was received, so there is no HTTP status")
        if self.response_received and not self.request_fully_sent:
            raise ValueError("a response implies the request was sent")
        return self


EvidenceFragment = Annotated[str, StringConstraints(min_length=1, max_length=240)]


class ExtractedFailureEvidence(DomainModel):
    """What the extraction model may return: structure and verbatim citations, nothing else.

    It has no field that could authorise, rank or execute anything.
    """

    provider: str = Field(
        max_length=32, description="The payout rail/provider the payload is from, e.g. MOMO_A."
    )
    provider_code: str | None = Field(
        default=None,
        max_length=40,
        description="The provider's result/reason code, copied exactly; null if none.",
    )
    provider_message: str | None = Field(
        default=None,
        max_length=240,
        description="The provider's own explanation, copied or trimmed verbatim; null if none.",
    )
    transport_outcome: TransportOutcome = Field(
        description="RESPONSE_RECEIVED only if the client log shows the provider answered the "
        "payout request itself."
    )
    acceptance_stage: AcceptanceStage = Field(
        description="PRE_ACCEPTANCE only if the payload explicitly says the instruction was "
        "rejected before being accepted or queued; POST_ACCEPTANCE if it says it was accepted, "
        "queued or processed; otherwise UNKNOWN."
    )
    explicit_rejection: bool | None = Field(
        default=None,
        description="true only if the provider explicitly rejected/declined the instruction; "
        "false if it explicitly accepted it; null if not stated.",
    )
    provider_reference: str | None = Field(
        default=None,
        max_length=64,
        description="The provider's own reference for this request or transfer, copied "
        "exactly; null if none.",
    )
    evidence_fragments: tuple[EvidenceFragment, ...] = Field(
        default=(),
        max_length=8,
        description="1-6 short VERBATIM substrings of the payload supporting the fields above.",
    )
    completeness: EvidenceCompleteness = Field(
        description="COMPLETE if every field above is directly stated in the payload."
    )


class FailureEvidence(DomainModel):
    """Extracted evidence, bound to the exact payload it was extracted from."""

    incident_id: EntityId
    raw_payload_digest: Sha256Hex
    extracted_by: EvidenceSource
    provider: str = Field(max_length=32)
    provider_code: str | None = Field(default=None, max_length=40)
    provider_message: str | None = Field(default=None, max_length=240)
    transport_outcome: TransportOutcome
    acceptance_stage: AcceptanceStage
    explicit_rejection: bool | None = None
    provider_reference: str | None = Field(default=None, max_length=64)
    evidence_fragments: tuple[EvidenceFragment, ...] = Field(default=(), max_length=8)
    completeness: EvidenceCompleteness

    @classmethod
    def bind(
        cls,
        incident: ProviderIncident,
        extraction: ExtractedFailureEvidence,
        source: EvidenceSource,
    ) -> FailureEvidence:
        """The digest is computed by code from the payload, never supplied by a model."""
        return cls(
            incident_id=incident.incident_id,
            raw_payload_digest=incident.raw_payload_digest,
            extracted_by=source,
            **extraction.model_dump(),
        )

    @property
    def digest(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return sha256_text(canonical)


class EvidenceCheck(DomainModel):
    name: str = Field(max_length=40)
    passed: bool
    detail: str = Field(max_length=240)


class EvidenceVerdict(DomainModel):
    """The deterministic FailureVerifier's decision about one provider incident."""

    incident_id: EntityId
    raw_payload_digest: Sha256Hex
    evidence_digest: Sha256Hex
    classification: AttemptOutcome
    # Integrity: is the evidence bound to this payload, grounded and self-consistent?
    checks: tuple[EvidenceCheck, ...]
    # What a DEFINITIVE_FAILED classification additionally requires (always evaluated, so an
    # UNKNOWN verdict shows exactly which proof was missing).
    requirements: tuple[EvidenceCheck, ...]
    reasons: tuple[str, ...] = Field(min_length=1)
    catalog_meaning: str | None = None

    @model_validator(mode="after")
    def _fail_closed(self) -> EvidenceVerdict:
        # Only UNKNOWN may be reached with a failing check.
        if self.classification is not AttemptOutcome.UNKNOWN and not all(
            c.passed for c in self.checks
        ):
            raise ValueError(f"{self.classification} requires every integrity check to pass")
        if self.classification is AttemptOutcome.DEFINITIVE_FAILED and not all(
            r.passed for r in self.requirements
        ):
            raise ValueError("DEFINITIVE_FAILED requires every definitive-failure requirement")
        return self

    @property
    def failure_stage(self) -> FailureStage | None:
        if self.classification is AttemptOutcome.DEFINITIVE_FAILED:
            return FailureStage.PRE_ACCEPTANCE
        if self.classification is AttemptOutcome.UNKNOWN:
            return FailureStage.UNDETERMINED
        return None


class IncidentClassification(DomainModel):
    """Write-once record of how an incident was classified and what the journal recorded."""

    incident_id: EntityId
    evidence: FailureEvidence
    verdict: EvidenceVerdict
    recorded_attempt_id: EntityId
    # What the journal recorded. A SUCCEEDED verdict is still recorded as UNKNOWN: a recipient
    # credit is never booked from extracted evidence, only after human confirmation.
    recorded_outcome: AttemptOutcome
    classified_at: datetime
