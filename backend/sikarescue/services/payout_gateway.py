"""Simulated external payout provider. No real network, no real money.

Honours provider-side idempotency: resubmitting the same idempotency key returns the
original response without moving value again (as real payout APIs typically do).
"""

from __future__ import annotations

import asyncio
from collections import defaultdict, deque

from sikarescue.models import (
    AttemptOutcome,
    DomainModel,
    FailureDetail,
    FailureStage,
    FundsLocation,
    Money,
    RailId,
    new_id,
)
from sikarescue.models.common import OpaqueToken, TransactionId


class PayoutRequest(DomainModel):
    idempotency_key: str
    transaction_id: TransactionId
    rail_id: RailId
    source: FundsLocation
    amount: Money
    recipient_token: OpaqueToken


class PayoutResponse(DomainModel):
    outcome: AttemptOutcome
    provider_reference: str | None = None
    failure: FailureDetail | None = None


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
        self.requests: list[PayoutRequest] = []
        self.value_movements: list[PayoutRequest] = []

    def script(self, rail_id: RailId, *outcomes: AttemptOutcome) -> None:
        """Queue outcomes for the next submissions on a rail (default: SUCCEEDED)."""
        self._scripted[rail_id].extend(outcomes)

    def submissions_for(self, rail_id: RailId) -> int:
        return sum(1 for r in self.requests if r.rail_id is rail_id)

    async def submit(self, request: PayoutRequest) -> PayoutResponse:
        self.requests.append(request)
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
