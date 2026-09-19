"""State is derived from journal evidence; recovery starts where the funds are (2, K, N)."""

from __future__ import annotations

from sikarescue.demo_data.sk10421 import PAYOUT_AMOUNT
from sikarescue.models import (
    AttemptOutcome,
    FailureStage,
    FundsLocation,
    OperationType,
    RailId,
    RecoveryState,
    SettlementLegStatus,
)


def test_seeded_state_matches_the_incident(world, txn_id):
    state = world.service.get_transaction_state(txn_id)
    assert state.recovery_state is RecoveryState.FAILED
    assert state.sender_debited is True
    assert state.fx_completed and state.settlement_completed
    assert state.recipient_credited is False
    assert state.funds_location is FundsLocation.GH_SETTLEMENT_ACCOUNT
    assert state.failed_leg is RailId.MOMO_A
    assert state.safe_to_restart_from_origin is False
    assert state.manual_review_required is False
    assert state.completed_effect_keys == (
        "SK-10421:sender_debit",
        "SK-10421:fx",
        "SK-10421:gh_settlement",
    )


def test_seeded_momo_a_503_is_definitive_pre_acceptance(world, txn_id):  # K
    aggregate = world.repository.get(txn_id)
    momo_a = next(a for a in aggregate.journal.attempts() if a.rail_id is RailId.MOMO_A)
    assert momo_a.outcome is AttemptOutcome.DEFINITIVE_FAILED
    assert momo_a.failure.http_status == 503
    assert momo_a.failure.stage is FailureStage.PRE_ACCEPTANCE


def test_funds_location_stays_at_settlement_after_definitive_failure(world, txn_id):  # N
    state = world.service.get_transaction_state(txn_id)
    assert state.last_payout_outcome is AttemptOutcome.DEFINITIVE_FAILED
    assert state.funds_location is FundsLocation.GH_SETTLEMENT_ACCOUNT


def test_outstanding_obligation_is_only_the_recipient_credit(world, txn_id):  # 2
    obligation = world.service.get_transaction_state(txn_id).outstanding_obligation
    assert obligation is not None
    assert obligation.operation is OperationType.RECIPIENT_CREDIT
    assert obligation.effect_key == "SK-10421:recipient_credit"
    assert obligation.source is FundsLocation.GH_SETTLEMENT_ACCOUNT
    assert obligation.amount == PAYOUT_AMOUNT


async def test_plan_starts_from_the_funds_location_not_the_origin(world, txn_id):  # 2
    plan = await world.service.create_recovery_plan(txn_id)
    assert plan.source is FundsLocation.GH_SETTLEMENT_ACCOUNT
    assert plan.obligation.operation is OperationType.RECIPIENT_CREDIT
    assert plan.amount == PAYOUT_AMOUNT


def test_legs_view_shows_three_successes_then_the_failure(world, txn_id):
    legs = world.service.get_transaction_state(txn_id).legs
    assert [(leg.rail_id, leg.status) for leg in legs] == [
        (RailId.UK_BANK_DEBIT, SettlementLegStatus.SUCCESS),
        (RailId.GBP_GHS_FX, SettlementLegStatus.SUCCESS),
        (RailId.GH_SETTLEMENT, SettlementLegStatus.SUCCESS),
        (RailId.MOMO_A, SettlementLegStatus.FAILED),
    ]
    assert legs[-1].failure_summary == "HTTP 503 PROVIDER_UNAVAILABLE (pre-acceptance)"
