"""Append-only synthetic financial journal.

The journal is the single source of truth for value movement. It enforces:
  * one successful effect per transaction-wide effect key (no replayed debit / FX /
    settlement, at most one recipient credit across every rail and plan);
  * every effect is backed by a recorded SUCCEEDED attempt for the same operation and rail;
  * value moves along the corridor chain in order (an effect's source must be where the
    funds currently are);
  * value is conserved between causal effects: each effect consumes exactly the currency and
    amount the previous effect delivered (FX changes currency only inside its own effect, whose
    source/destination amounts are checked against its rate), and the first effect consumes
    exactly the payment's principal when the journal is told it.
`revision` is the number of entries, so any new financial evidence advances it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sikarescue.errors import DuplicateEffectError, JournalIntegrityError
from sikarescue.models import (
    AttemptOutcome,
    AttemptRecorded,
    EffectPosted,
    FinancialEffect,
    FundsLocation,
    JournalEntry,
    Money,
    OperationAttempt,
    OperationType,
    utcnow,
)
from sikarescue.models.ledger import JournalBody, ProviderResponseReceived


class FinancialJournal:
    def __init__(
        self,
        transaction_id: str,
        clock: Callable[[], datetime] = utcnow,
        *,
        principal: Money | None = None,
    ):
        self.transaction_id = transaction_id
        self.principal = principal  # what the first effect must consume, when known
        self._clock = clock
        self._entries: list[JournalEntry] = []
        self._effects: dict[str, FinancialEffect] = {}
        self._attempts: dict[str, OperationAttempt] = {}

    # --- reads -------------------------------------------------------------------------

    @property
    def revision(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> tuple[JournalEntry, ...]:
        return tuple(self._entries)

    def effects(self) -> tuple[FinancialEffect, ...]:
        return tuple(self._effects.values())

    def attempts(self) -> tuple[OperationAttempt, ...]:
        return tuple(self._attempts.values())

    def effect(self, operation: OperationType) -> FinancialEffect | None:
        return next((e for e in self._effects.values() if e.operation is operation), None)

    def has_effect(self, operation: OperationType) -> bool:
        return self.effect(operation) is not None

    def count_effects(self, operation: OperationType) -> int:
        return sum(1 for e in self._effects.values() if e.operation is operation)

    def pending_provider_responses(self) -> tuple[ProviderResponseReceived, ...]:
        """Provider responses whose attempt has not been classified (recorded) yet."""
        return tuple(
            e.body
            for e in self._entries
            if isinstance(e.body, ProviderResponseReceived)
            and e.body.attempt_id not in self._attempts
        )

    def funds_location(self) -> FundsLocation:
        """Replay successful effects from the sender's account. Failed attempts move nothing."""
        location = FundsLocation.SENDER_ACCOUNT
        for effect in self._effects.values():
            if effect.source is not location:
                raise JournalIntegrityError("journal effects are out of value-chain order")
            location = effect.destination
        return location

    def fork(self) -> FinancialJournal:
        """An independent copy for dry runs (previews). Appends to it never touch this one."""
        clone = FinancialJournal(self.transaction_id, self._clock, principal=self.principal)
        clone._entries = list(self._entries)  # entries are immutable records
        clone._effects = dict(self._effects)
        clone._attempts = dict(self._attempts)
        return clone

    # --- appends -----------------------------------------------------------------------

    def record_attempt(self, attempt: OperationAttempt) -> JournalEntry:
        self._require_own(attempt.transaction_id)
        if attempt.attempt_id in self._attempts:
            raise JournalIntegrityError(f"attempt {attempt.attempt_id} already recorded")
        response = next(
            (r for r in self.pending_provider_responses() if r.attempt_id == attempt.attempt_id),
            None,
        )
        if response is not None and (response.rail_id, response.operation) != (
            attempt.rail_id,
            attempt.operation,
        ):
            raise JournalIntegrityError("attempt does not match its recorded provider response")
        self._attempts[attempt.attempt_id] = attempt
        return self._append(AttemptRecorded(attempt=attempt))

    def post_effect(self, effect: FinancialEffect) -> JournalEntry:
        self._require_own(effect.transaction_id)
        if effect.effect_key in self._effects:
            raise DuplicateEffectError(
                f"effect {effect.effect_key} already posted; completed value movement "
                "cannot be replayed"
            )
        attempt = self._attempts.get(effect.attempt_id)
        if attempt is None or attempt.outcome is not AttemptOutcome.SUCCEEDED:
            raise JournalIntegrityError("an effect requires a recorded SUCCEEDED attempt")
        if (attempt.operation, attempt.rail_id) != (effect.operation, effect.rail_id):
            raise JournalIntegrityError("effect does not match its attempt's operation/rail")
        if attempt.amount != effect.source_amount:
            raise JournalIntegrityError("effect amount does not match its attempt")
        if effect.source is not self.funds_location():
            raise JournalIntegrityError(
                f"funds are at {self.funds_location()}, not {effect.source}"
            )
        self._check_value_continuity(effect)
        self._effects[effect.effect_key] = effect
        return self._append(EffectPosted(effect=effect))

    def _check_value_continuity(self, effect: FinancialEffect) -> None:
        """The effect must consume exactly what the previous causal effect delivered."""
        previous = next(reversed(self._effects.values()), None)
        if previous is None:
            if self.principal is None:
                return
            delivered, origin = self.principal, "the payment principal"
        else:
            delivered, origin = previous.destination_amount, f"{previous.operation}"
        consumed = effect.source_amount
        if consumed.currency is not delivered.currency:
            raise JournalIntegrityError(
                f"currency discontinuity: {effect.operation} consumes {consumed.currency} "
                f"but {origin} delivered {delivered.currency}"
            )
        if consumed.amount != delivered.amount:
            raise JournalIntegrityError(
                f"amount discontinuity: {effect.operation} consumes {consumed} "
                f"but {origin} delivered {delivered}"
            )

    def record(self, body: JournalBody) -> JournalEntry:
        """Append non-value-moving evidence (execution markers, callbacks, reconciliation)."""
        if isinstance(body, (EffectPosted, AttemptRecorded)):
            raise JournalIntegrityError("use post_effect / record_attempt for financial records")
        if isinstance(body, ProviderResponseReceived) and body.attempt_id in self._attempts:
            raise JournalIntegrityError(f"attempt {body.attempt_id} is already classified")
        return self._append(body)

    def _append(self, body: JournalBody) -> JournalEntry:
        entry = JournalEntry(
            sequence=len(self._entries) + 1,
            transaction_id=self.transaction_id,
            recorded_at=self._clock(),
            body=body,
        )
        self._entries.append(entry)
        return entry

    def _require_own(self, transaction_id: str) -> None:
        if transaction_id != self.transaction_id:
            raise JournalIntegrityError("record belongs to a different transaction")
