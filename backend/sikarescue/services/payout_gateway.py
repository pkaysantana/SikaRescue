"""Simulated external payout provider. No real network, no real money.

Honours provider-side idempotency: resubmitting the same idempotency key with the SAME request
returns the original response without moving value again (as real payout APIs typically do).
Reusing a key for a DIFFERENT request is refused, never silently replayed or executed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections import defaultdict, deque

from pydantic import Field, model_validator

from sikarescue.models import (
    AttemptOutcome,
    DomainModel,
    EndpointType,
    FailureDetail,
    FailureStage,
    FundsLocation,
    Money,
    RailId,
    new_id,
)
from sikarescue.models.common import OpaqueToken, TransactionId


class IdempotencyKeyReuseError(Exception):
    """The provider saw this idempotency key before, for a different request."""


class PayoutRequest(DomainModel):
    """One physical provider attempt (`idempotency_key`) for one logical effect (`effect_key`)."""

    idempotency_key: str = Field(min_length=1, max_length=128)
    effect_key: str = Field(min_length=1, max_length=128)
    transaction_id: TransactionId
    rail_id: RailId
    source: FundsLocation
    amount: Money
    recipient_token: OpaqueToken
    endpoint_type: EndpointType

    @property
    def fingerprint(self) -> str:
        """Canonical hash of WHAT is being paid (everything except the attempt key itself)."""
        payload = {
            "transaction_id": self.transaction_id,
            "effect_key": self.effect_key,
            "rail_id": self.rail_id.value,
            "source": self.source.value,
            "recipient_token": self.recipient_token,
            "endpoint_type": self.endpoint_type.value,
            "currency": self.amount.currency.value,
            "amount": f"{self.amount.amount:.2f}",
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


class PayoutResponse(DomainModel):
    """A provider answer. Impossible combinations are rejected at construction."""

    outcome: AttemptOutcome
    provider_reference: str | None = Field(default=None, min_length=1, max_length=64)
    failure: FailureDetail | None = None

    @model_validator(mode="after")
    def _consistent(self) -> PayoutResponse:
        if self.outcome is AttemptOutcome.SUCCEEDED:
            if self.provider_reference is None or self.failure is not None:
                raise ValueError("SUCCEEDED needs a provider reference and no failure")
        elif self.failure is None:
            raise ValueError(f"{self.outcome} needs failure evidence")
        elif self.outcome is AttemptOutcome.DEFINITIVE_FAILED:
            if self.failure.stage is not FailureStage.PRE_ACCEPTANCE:
                raise ValueError(
                    "DEFINITIVE_FAILED needs a PRE_ACCEPTANCE failure (no value moved)"
                )
        elif self.failure.stage is FailureStage.PRE_ACCEPTANCE:
            # A pre-acceptance rejection proves no value moved: that is not uncertainty.
            raise ValueError("UNKNOWN cannot carry a PRE_ACCEPTANCE failure")
        return self


SYNTHETIC_FAILURES = {
    AttemptOutcome.DEFINITIVE_FAILED: FailureDetail(
        stage=FailureStage.PRE_ACCEPTANCE,
        http_status=503,
        provider_code="PROVIDER_UNAVAILABLE",
        message="Synthetic 503: provider rejected the request before acceptance; no value moved.",
    ),
    AttemptOutcome.UNKNOWN: FailureDetail(
        stage=FailureStage.UNDETERMINED,
        http_status=504,
        provider_code="GATEWAY_TIMEOUT",
        message="Synthetic timeout after submission; provider outcome unknown.",
    ),
}


class SimulatedPayoutGateway:
    def __init__(self, latency_seconds: float = 0.0):
        self.latency_seconds = latency_seconds
        self._scripted: dict[RailId, deque[AttemptOutcome]] = defaultdict(deque)
        self._responses: dict[str, PayoutResponse] = {}
        self._fingerprints: dict[str, str] = {}
        self.requests: list[PayoutRequest] = []
        self.value_movements: list[PayoutRequest] = []

    def script(self, rail_id: RailId, *outcomes: AttemptOutcome) -> None:
        """Queue outcomes for the next submissions on a rail (default: SUCCEEDED)."""
        self._scripted[rail_id].extend(outcomes)

    def submissions_for(self, rail_id: RailId) -> int:
        return sum(1 for r in self.requests if r.rail_id is rail_id)

    async def submit(self, request: PayoutRequest) -> PayoutResponse:
        self.requests.append(request)
        seen = self._fingerprints.setdefault(request.idempotency_key, request.fingerprint)
        if seen != request.fingerprint:
            raise IdempotencyKeyReuseError(
                "idempotency key reused for a different request; refused without moving value"
            )
        if request.idempotency_key in self._responses:
            return self._responses[request.idempotency_key]
        if self.latency_seconds:
            await asyncio.sleep(self.latency_seconds)
        queue = self._scripted[request.rail_id]
        outcome = queue.popleft() if queue else AttemptOutcome.SUCCEEDED
        if outcome is AttemptOutcome.SUCCEEDED:
            response = PayoutResponse(outcome=outcome, provider_reference=new_id("prv"))
            self.value_movements.append(request)
        else:
            response = PayoutResponse(outcome=outcome, failure=SYNTHETIC_FAILURES[outcome])
        self._responses[request.idempotency_key] = response
        return response
