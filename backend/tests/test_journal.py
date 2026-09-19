"""Append-only journal: completed value movement can never be replayed (1, A-D, N)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sikarescue.demo_data.sk10421 import (
    FX_RATE,
    PAYOUT_AMOUNT,
    SEND_AMOUNT,
    TRANSACTION_ID,
    seed_transaction,
)
from sikarescue.errors import DuplicateEffectError, JournalIntegrityError
from sikarescue.models import (
    AttemptOutcome,
    FinancialEffect,
    FundsLocation,
    OperationAttempt,
    OperationType,
    RailId,
    effect_key,
)
from sikarescue.models.enums import OPERATION_FLOW

NOW = datetime(2026, 9, 19, 11, 0, tzinfo=UTC)
_counter = iter(range(0x100, 0x10000))


def _succeeded_attempt(op: OperationType, rail: RailId, amount) -> OperationAttempt:
    source, destination = OPERATION_FLOW[op]
    return OperationAttempt(
        attempt_id=f"att_{next(_counter):012x}",
        transaction_id=TRANSACTION_ID,
        operation=op,
        rail_id=rail,
        source=source,
        destination=destination,
        amount=amount,
        outcome=AttemptOutcome.SUCCEEDED,
        idempotency_key=f"replay-{op}-{rail}",
        started_at=NOW,
        completed_at=NOW,
    )


def _replay(journal, op: OperationType, rail: RailId, source_amount, dest_amount, rate=None):
    attempt = _succeeded_attempt(op, rail, source_amount)
    journal.record_attempt(attempt)
    source, destination = OPERATION_FLOW[op]
    journal.post_effect(
        FinancialEffect(
            effect_key=effect_key(TRANSACTION_ID, op),
            transaction_id=TRANSACTION_ID,
            operation=op,
            rail_id=rail,
            attempt_id=attempt.attempt_id,
            source=source,
            destination=destination,
            source_amount=source_amount,
            destination_amount=dest_amount,
            fx_rate=rate,
            posted_at=NOW,
        )
    )


def test_seed_journal_uses_transaction_wide_effect_keys():
    journal = seed_transaction().journal
    assert [e.effect_key for e in journal.effects()] == [
        "SK-10421:sender_debit",
        "SK-10421:fx",
        "SK-10421:gh_settlement",
    ]


def test_sender_cannot_be_debited_twice():  # requirement 1 / A
    journal = seed_transaction().journal
    with pytest.raises(DuplicateEffectError, match="sender_debit"):
        _replay(journal, OperationType.SENDER_DEBIT, RailId.UK_BANK_DEBIT, SEND_AMOUNT, SEND_AMOUNT)
    assert journal.count_effects(OperationType.SENDER_DEBIT) == 1


def test_completed_fx_cannot_be_replayed():  # B
    journal = seed_transaction().journal
    with pytest.raises(DuplicateEffectError, match=":fx"):
        _replay(
            journal, OperationType.FX_CONVERSION, RailId.GBP_GHS_FX, SEND_AMOUNT, PAYOUT_AMOUNT,
            FX_RATE,
        )  # fmt: skip
    assert journal.count_effects(OperationType.FX_CONVERSION) == 1


def test_completed_gh_settlement_cannot_be_replayed():  # C
    journal = seed_transaction().journal
    with pytest.raises(DuplicateEffectError, match="gh_settlement"):
        _replay(
            journal, OperationType.GH_SETTLEMENT, RailId.GH_SETTLEMENT, PAYOUT_AMOUNT,
            PAYOUT_AMOUNT,
        )  # fmt: skip


def test_recipient_credit_at_most_once_across_rails():  # D
    journal = seed_transaction().journal
    op = OperationType.RECIPIENT_CREDIT
    _replay(journal, op, RailId.MOMO_B, PAYOUT_AMOUNT, PAYOUT_AMOUNT)
    for other_rail in (RailId.BANK_MOMO_BRIDGE, RailId.MOMO_B, RailId.TOKEN_BRIDGE):
        with pytest.raises(DuplicateEffectError, match="recipient_credit"):
            _replay(journal, op, other_rail, PAYOUT_AMOUNT, PAYOUT_AMOUNT)
    assert journal.count_effects(op) == 1
    assert journal.funds_location() is FundsLocation.RECIPIENT_ENDPOINT


def test_failed_attempt_moves_no_value():  # N
    journal = seed_transaction().journal
    momo_a = [a for a in journal.attempts() if a.rail_id is RailId.MOMO_A]
    assert len(momo_a) == 1 and momo_a[0].outcome is AttemptOutcome.DEFINITIVE_FAILED
    assert not journal.has_effect(OperationType.RECIPIENT_CREDIT)
    assert journal.funds_location() is FundsLocation.GH_SETTLEMENT_ACCOUNT


def test_effect_requires_successful_attempt_evidence():
    journal = seed_transaction().journal
    failed_attempt = next(a for a in journal.attempts() if a.rail_id is RailId.MOMO_A)
    source, destination = OPERATION_FLOW[OperationType.RECIPIENT_CREDIT]
    effect = FinancialEffect(
        effect_key=effect_key(TRANSACTION_ID, OperationType.RECIPIENT_CREDIT),
        transaction_id=TRANSACTION_ID,
        operation=OperationType.RECIPIENT_CREDIT,
        rail_id=RailId.MOMO_A,
        attempt_id=failed_attempt.attempt_id,
        source=source,
        destination=destination,
        source_amount=PAYOUT_AMOUNT,
        destination_amount=PAYOUT_AMOUNT,
        posted_at=NOW,
    )
    with pytest.raises(JournalIntegrityError, match="SUCCEEDED attempt"):
        journal.post_effect(effect)


def test_value_must_move_along_the_chain_in_order():
    """A recipient credit cannot be posted while funds have not reached settlement."""
    from sikarescue.services.journal import FinancialJournal

    journal = FinancialJournal(TRANSACTION_ID)
    with pytest.raises(JournalIntegrityError, match="funds are at SENDER_ACCOUNT"):
        _replay(journal, OperationType.RECIPIENT_CREDIT, RailId.MOMO_B, PAYOUT_AMOUNT,
                PAYOUT_AMOUNT)  # fmt: skip


def test_journal_is_append_only_and_revision_counts_entries():
    journal = seed_transaction().journal
    entries = journal.entries
    assert isinstance(entries, tuple)
    assert [e.sequence for e in entries] == list(range(1, len(entries) + 1))
    assert journal.revision == len(entries) == 7  # 3 x (attempt + effect) + failed MOMO_A attempt
