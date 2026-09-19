"""Phase 7A correctness hardening: rank selection, value continuity, provider-response
consistency, request fingerprints, plan freshness and funds certainty."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from sikarescue.compute.backend import LocalRouteComputeBackend, RouteComputeBackend
from sikarescue.demo_data.sk10421 import (
    FX_RATE,
    PAYOUT_AMOUNT,
    SEND_AMOUNT,
    TRANSACTION_ID,
    build_demo_world,
)
from sikarescue.errors import (
    IdempotencyConflictError,
    IllegalTransitionError,
    JournalIntegrityError,
    StalePlanError,
)
from sikarescue.models import (
    AttemptOutcome,
    Currency,
    EndpointType,
    ExecutionStatus,
    FailureDetail,
    FailureStage,
    FinancialEffect,
    FundsCertainty,
    FundsLocation,
    Money,
    OperationAttempt,
    OperationType,
    RailId,
    RailQuote,
    RecoveryPlan,
    RecoveryState,
    effect_key,
    new_id,
    payout_execution_key,
)
from sikarescue.models.enums import OPERATION_FLOW
from sikarescue.services.journal import FinancialJournal
from sikarescue.services.payout_gateway import (
    IdempotencyKeyReuseError,
    PayoutRequest,
    PayoutResponse,
    SimulatedPayoutGateway,
)

from helpers import plan_and_approve

NOW = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)
GBP, GHS = Currency.GBP, Currency.GHS


# ============================================================== 1. rank-1 selection


class ReversingBackend(RouteComputeBackend):
    """Returns correct, verifiable results in REVERSED order (ranks untouched)."""

    name = "local"

    def __init__(self) -> None:
        self.inner = LocalRouteComputeBackend()
        self.returned_order: list[int | None] = []

    async def evaluate(self, request):
        batch = await self.inner.evaluate(request)
        reordered = tuple(reversed(batch.evaluations))
        self.returned_order = [e.rank for e in reordered if e.passed]
        return batch.model_copy(update={"evaluations": reordered})


async def test_plan_selects_rank_one_not_the_first_position(txn_id):
    backend = ReversingBackend()
    world = build_demo_world(compute=backend)
    plan = await world.service.create_recovery_plan(txn_id)
    assert backend.returned_order == [2, 1]  # rank 2 really did come first
    assert plan.rail_id is RailId.MOMO_B and plan.selected_rank == 1
    assert plan.evaluations[0].rank == 1  # stored in canonical order


async def test_plan_must_be_rank_one(world, txn_id):
    plan = await world.service.create_recovery_plan(txn_id)
    fields = {n: getattr(plan, n) for n in RecoveryPlan.model_fields if n != "plan_hash"}
    with pytest.raises(ValidationError, match="rank-1"):
        RecoveryPlan.build(**(fields | {"selected_rank": 2}))
    for field, value in (
        ("quote_id", "qte_ffffffffffff"),
        ("route_set_fingerprint", "0" * 64),
        ("selected_rank", 2),
    ):
        with pytest.raises(ValidationError, match="plan_hash"):
            RecoveryPlan.model_validate(plan.model_dump() | {field: value})


# ============================================================== 2. value continuity


def _post(journal, op, rail, source_amount, destination_amount, rate=None) -> None:
    source, destination = OPERATION_FLOW[op]
    attempt = OperationAttempt(
        attempt_id=new_id("att"),
        transaction_id=TRANSACTION_ID,
        operation=op,
        rail_id=rail,
        source=source,
        destination=destination,
        amount=source_amount,
        outcome=AttemptOutcome.SUCCEEDED,
        idempotency_key=effect_key(TRANSACTION_ID, op),
        started_at=NOW,
        completed_at=NOW,
    )
    journal.record_attempt(attempt)
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
            destination_amount=destination_amount,
            fx_rate=rate,
            posted_at=NOW,
        )
    )


def _journal_through_fx() -> FinancialJournal:
    journal = FinancialJournal(TRANSACTION_ID, principal=SEND_AMOUNT)
    _post(journal, OperationType.SENDER_DEBIT, RailId.UK_BANK_DEBIT, SEND_AMOUNT, SEND_AMOUNT)
    _post(
        journal, OperationType.FX_CONVERSION, RailId.GBP_GHS_FX, SEND_AMOUNT, PAYOUT_AMOUNT, FX_RATE
    )
    return journal


def test_consistent_chain_is_accepted():
    journal = _journal_through_fx()
    _post(journal, OperationType.GH_SETTLEMENT, RailId.GH_SETTLEMENT, PAYOUT_AMOUNT, PAYOUT_AMOUNT)
    _post(journal, OperationType.RECIPIENT_CREDIT, RailId.MOMO_B, PAYOUT_AMOUNT, PAYOUT_AMOUNT)
    assert journal.funds_location() is FundsLocation.RECIPIENT_ENDPOINT


def test_fx_cannot_consume_less_than_the_debit_delivered():
    journal = FinancialJournal(TRANSACTION_ID, principal=SEND_AMOUNT)
    _post(journal, OperationType.SENDER_DEBIT, RailId.UK_BANK_DEBIT, SEND_AMOUNT, SEND_AMOUNT)
    hundred = Money.of("100.00", GBP)
    converted = Money.of((hundred.amount * FX_RATE).quantize(Decimal("0.01")), GHS)
    with pytest.raises(JournalIntegrityError, match="amount discontinuity"):
        _post(journal, OperationType.FX_CONVERSION, RailId.GBP_GHS_FX, hundred, converted, FX_RATE)


def test_payout_cannot_source_more_than_settlement_delivered():
    journal = _journal_through_fx()
    _post(journal, OperationType.GH_SETTLEMENT, RailId.GH_SETTLEMENT, PAYOUT_AMOUNT, PAYOUT_AMOUNT)
    two_thousand = Money.of("2000.00", GHS)
    with pytest.raises(JournalIntegrityError, match="amount discontinuity"):
        _post(journal, OperationType.RECIPIENT_CREDIT, RailId.MOMO_B, two_thousand, two_thousand)


def test_currency_must_carry_over_between_effects():
    journal = _journal_through_fx()  # FX delivered GHS
    with pytest.raises(JournalIntegrityError, match="currency discontinuity"):
        _post(journal, OperationType.GH_SETTLEMENT, RailId.GH_SETTLEMENT, SEND_AMOUNT, SEND_AMOUNT)


def test_first_effect_must_consume_the_principal():
    journal = FinancialJournal(TRANSACTION_ID, principal=SEND_AMOUNT)
    wrong = Money.of("100.00", GBP)
    with pytest.raises(JournalIntegrityError, match="payment principal"):
        _post(journal, OperationType.SENDER_DEBIT, RailId.UK_BANK_DEBIT, wrong, wrong)


# ============================================================== 5. provider response consistency

PRE = FailureDetail(stage=FailureStage.PRE_ACCEPTANCE, http_status=503, message="rejected")
UNDETERMINED = FailureDetail(stage=FailureStage.UNDETERMINED, http_status=504, message="timeout")


@pytest.mark.parametrize(
    ("outcome", "reference", "failure"),
    [
        (AttemptOutcome.SUCCEEDED, None, None),  # success without provider evidence
        (AttemptOutcome.SUCCEEDED, "prv_1", PRE),  # success that also failed
        (AttemptOutcome.DEFINITIVE_FAILED, None, None),  # failure without evidence
        (AttemptOutcome.DEFINITIVE_FAILED, None, UNDETERMINED),  # "definitive" yet uncertain
        (AttemptOutcome.UNKNOWN, None, None),  # uncertainty not stated
        (AttemptOutcome.UNKNOWN, None, PRE),  # pre-acceptance proves no value moved
    ],
)
def test_contradictory_provider_responses_are_rejected(outcome, reference, failure):
    with pytest.raises(ValidationError):
        PayoutResponse(outcome=outcome, provider_reference=reference, failure=failure)


def test_consistent_provider_responses_are_accepted():
    PayoutResponse(outcome=AttemptOutcome.SUCCEEDED, provider_reference="prv_1")
    PayoutResponse(outcome=AttemptOutcome.DEFINITIVE_FAILED, failure=PRE)
    PayoutResponse(outcome=AttemptOutcome.UNKNOWN, failure=UNDETERMINED)


# ============================================================== 6. request fingerprints


def _request(amount: str = "1830.00") -> PayoutRequest:
    return PayoutRequest(
        idempotency_key=f"{TRANSACTION_ID}:plan:plan_000000000001:payout",
        effect_key=effect_key(TRANSACTION_ID, OperationType.RECIPIENT_CREDIT),
        transaction_id=TRANSACTION_ID,
        rail_id=RailId.MOMO_B,
        source=FundsLocation.GH_SETTLEMENT_ACCOUNT,
        amount=Money.of(amount, GHS),
        recipient_token="rcp_8f3k2m9q",
        endpoint_type=EndpointType.MOBILE_MONEY,
    )


async def test_provider_replays_same_request_and_refuses_a_different_one():
    gateway = SimulatedPayoutGateway()
    first = await gateway.submit(_request())
    assert await gateway.submit(_request()) == first  # same key + same fingerprint: replay
    with pytest.raises(IdempotencyKeyReuseError):
        await gateway.submit(_request(amount="2000.00"))  # same key, different request
    assert len(gateway.value_movements) == 1


async def test_logical_effect_and_physical_attempt_identities_are_separate(world, txn_id):
    plan = await plan_and_approve(world)
    execution = await world.service.execute_recovery(plan.plan_id)
    request = world.gateway.requests[0]
    assert request.effect_key == f"{txn_id}:recipient_credit"  # logical: one per transaction
    assert request.idempotency_key == payout_execution_key(txn_id, plan.plan_id)  # physical
    assert execution.request_fingerprint == request.fingerprint


async def test_replaying_a_key_bound_to_a_different_request_fails_closed(world, txn_id):
    plan = await plan_and_approve(world)
    await world.service.execute_recovery(plan.plan_id)
    aggregate = world.repository.get(txn_id)
    key = payout_execution_key(txn_id, plan.plan_id)
    aggregate.executions[key] = aggregate.executions[key].model_copy(
        update={"request_fingerprint": "f" * 64}
    )
    with pytest.raises(IdempotencyConflictError):
        await world.service.execute_recovery(plan.plan_id)
    assert len(world.gateway.requests) == 1  # nothing was re-dispatched


async def test_provider_key_reuse_refusal_is_unknown_not_a_retryable_failure(world, txn_id):
    plan = await plan_and_approve(world)
    key = payout_execution_key(txn_id, plan.plan_id)
    world.gateway._fingerprints[key] = "e" * 64  # the provider already knows this key
    execution = await world.service.execute_recovery(plan.plan_id)
    state = world.service.get_transaction_state(txn_id)
    assert execution.status is ExecutionStatus.OUTCOME_UNKNOWN
    assert state.recovery_state is RecoveryState.MANUAL_REVIEW
    assert state.recipient_credit_count == 0 and world.gateway.value_movements == []


# ============================================================== 7. plan freshness


def _requote(world, rail: RailId, **changes) -> None:
    quote = world.registry.quote(rail)
    world.registry.replace_quote(quote.model_copy(update=changes))


async def test_changed_quote_id_invalidates_the_approved_plan(world, txn_id):
    plan = await plan_and_approve(world)
    _requote(world, RailId.MOMO_B, quote_id="qte_0000000000ff")  # same fee and latency
    with pytest.raises(StalePlanError) as stale:
        await world.service.execute_recovery(plan.plan_id)
    assert any("quote changed" in r for r in stale.value.reasons)
    assert stale.value.replacement_plan_id  # a NEW plan, needing a NEW approval
    assert world.gateway.requests == []


async def test_changed_competing_route_invalidates_the_approved_plan(world, txn_id):
    plan = await plan_and_approve(world)
    _requote(world, RailId.BANK_MOMO_BRIDGE, incremental_fee=Money.of("0.01", GBP))
    with pytest.raises(StalePlanError) as stale:
        await world.service.execute_recovery(plan.plan_id)
    assert any("competing routes changed" in r for r in stale.value.reasons)
    assert world.gateway.requests == []
    replacement = world.service.get_plan(stale.value.replacement_plan_id)
    assert replacement.selected_rank == 1 and replacement.plan_id != plan.plan_id


def test_quotes_are_valid_entity_ids():
    RailQuote.model_validate(
        {
            "quote_id": "qte_0000000000ff",
            "rail_id": RailId.MOMO_B,
            "incremental_fee": Money.of("0.18", GBP),
            "expected_latency_seconds": 74,
            "quoted_reliability": 0.981,
        }
    )


# ============================================================== 4. funds certainty


def test_settled_funds_are_proven_and_available(world, txn_id):
    state = world.service.get_transaction_state(txn_id)
    assert state.funds_certainty is FundsCertainty.PROVEN
    assert state.available_for_automatic_action and state.uncertainty_reason is None


async def test_unknown_payout_makes_the_position_uncertain_and_never_retries(txn_id):
    world = build_demo_world()
    world.gateway.script(RailId.MOMO_B, AttemptOutcome.UNKNOWN)
    plan = await plan_and_approve(world)
    await world.service.execute_recovery(plan.plan_id)
    state = world.service.get_transaction_state(txn_id)

    assert state.recovery_state is RecoveryState.MANUAL_REVIEW
    assert state.funds_certainty is FundsCertainty.UNCERTAIN
    assert state.available_for_automatic_action is False
    assert state.funds_location is FundsLocation.GH_SETTLEMENT_ACCOUNT  # last CONFIRMED only
    assert "may already have been credited" in state.uncertainty_reason
    with pytest.raises(IllegalTransitionError, match="MANUAL_REVIEW"):
        await world.service.create_recovery_plan(txn_id)
    assert len(world.gateway.requests) == 1  # never auto-retried


async def test_position_is_uncertain_while_a_payout_is_in_flight(txn_id):
    world = build_demo_world(payout_latency_seconds=0.3)
    plan = await plan_and_approve(world)
    execution = asyncio.create_task(world.service.execute_recovery(plan.plan_id))
    await asyncio.sleep(0.1)
    mid_flight = world.service.get_transaction_state(txn_id)
    assert mid_flight.funds_certainty is FundsCertainty.UNCERTAIN
    assert not mid_flight.available_for_automatic_action
    await execution
    assert world.service.get_transaction_state(txn_id).funds_certainty is FundsCertainty.PROVEN
