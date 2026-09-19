"""Pydantic rejects malformed route/evaluation/plan/ledger data (requirement 14, K)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from sikarescue.compute.scenarios import SCENARIOS
from sikarescue.demo_data.sk10421 import TRANSACTION_ID
from sikarescue.models import (
    AttemptOutcome,
    Currency,
    FailureDetail,
    FailureStage,
    FundsLocation,
    HardConstraintStatus,
    Money,
    OperationAttempt,
    OperationType,
    RailId,
    RecoveryPlan,
    RejectionReason,
    RouteEvaluation,
    RouteSimulationResult,
    ScenarioId,
)

NOW = datetime(2026, 9, 19, 10, 0, tzinfo=UTC)
GBP = Currency.GBP


def _sim_result(**overrides) -> RouteSimulationResult:
    fields = {
        "route_id": "rt_MOMO_B",
        "scenario": SCENARIOS[ScenarioId.NORMAL],
        "simulation_count": 2000,
        "successful_runs": 1962,
        "simulated_success_probability": 0.981,
        "p50_latency_seconds": 70.0,
        "p95_latency_seconds": 108.0,
        "recovery_within_sla_probability": 0.96,
        "sla_seconds": 120.0,
        "seed": 10421,
    }
    return RouteSimulationResult(**(fields | overrides))


def _evaluation(**overrides):
    fields = {
        "route_id": "rt_MOMO_B",
        "rail_id": RailId.MOMO_B,
        "hard_constraint_status": HardConstraintStatus.PASSED,
        "estimated_incremental_cost": Money.of("0.18", GBP),
        "expected_latency_seconds": 74,
        "quoted_reliability": 0.981,
        "score": {"reliability": 0.8, "cost": 0.8, "latency": 0.7, "route_quality": 1.0,
                  "total": 0.8},
        "rank": 1,
        "compute_backend": "local",
        "scenario_results": (_sim_result(),),
        "simulated_success_probability": 0.981,
        "p50_latency_seconds": 70.0,
        "p95_latency_seconds": 108.0,
        "simulated_within_sla_probability": 0.96,
    }  # fmt: skip
    return RouteEvaluation(**(fields | overrides))


def test_valid_evaluation_builds():
    assert _evaluation().passed


@pytest.mark.parametrize(
    "overrides",
    [
        {"quoted_reliability": 1.5},  # probability > 1
        {"expected_latency_seconds": 0},  # non-positive latency
        {"route_id": "momo-b"},  # malformed route id
        {"rail_id": "MOMO_Z"},  # unknown rail
        {"compute_backend": ""},  # empty backend
        {"unexpected_field": True},  # extra fields forbidden
        {"rejection_reasons": (RejectionReason.POLICY_DENIED,)},  # passing route with reasons
        {"score": None},  # passing route must be scored
        {"hard_constraint_status": HardConstraintStatus.REJECTED},  # rejected but scored/ranked
        {"p50_latency_seconds": 90.0, "p95_latency_seconds": 60.0},  # p95 < p50
        {"scenario_results": ()},  # passing route must carry simulation results
        {"simulated_success_probability": 0.999},  # must match the primary scenario result
        {"scenario_results": (_sim_result(route_id="rt_TOKEN_BRIDGE"),)},  # another route's
    ],
)
def test_malformed_evaluation_rejected(overrides):
    with pytest.raises(ValidationError):
        _evaluation(**overrides)


REJECTED_UNSIMULATED = {
    "hard_constraint_status": HardConstraintStatus.REJECTED,
    "score": None,
    "rank": None,
    "scenario_results": (),
    "simulated_success_probability": None,
    "p50_latency_seconds": None,
    "p95_latency_seconds": None,
    "simulated_within_sla_probability": None,
}


def test_rejected_evaluation_requires_reason():
    with pytest.raises(ValidationError):
        _evaluation(**REJECTED_UNSIMULATED)
    ok = _evaluation(**REJECTED_UNSIMULATED, rejection_reasons=(RejectionReason.POLICY_DENIED,))
    assert not ok.passed


def test_rejected_route_can_never_carry_simulation_results():
    with pytest.raises(ValidationError, match="never simulated"):
        _evaluation(
            **(REJECTED_UNSIMULATED | {"scenario_results": (_sim_result(),)}),
            rejection_reasons=(RejectionReason.POLICY_DENIED,),
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"successful_runs": 2001},  # more successes than runs
        {"simulated_success_probability": 0.5},  # inconsistent with the counts
        {"recovery_within_sla_probability": 0.99},  # within-SLA must be a subset of successes
        {"p50_latency_seconds": None},  # successes imply latency percentiles
        {"p50_latency_seconds": 120.0},  # p95 below p50
        {"synthetic": False},  # simulated data must be labelled synthetic
    ],
)
def test_malformed_simulation_result_rejected(overrides):
    with pytest.raises(ValidationError):
        _sim_result(**overrides)


@pytest.mark.parametrize("amount", [0.18, "-1.00", "1.001", "abc"])
def test_money_rejects_float_negative_and_excess_precision(amount):
    with pytest.raises(ValidationError):
        Money(amount=amount, currency=GBP)


def test_money_accepts_decimal_string_and_int():
    assert Money(amount="120.00", currency=GBP).amount == Decimal("120.00")
    assert Money(amount=120, currency=GBP).amount == Decimal(120)


def _attempt(outcome: AttemptOutcome, failure: FailureDetail | None) -> OperationAttempt:
    return OperationAttempt(
        attempt_id="att_00000000abcd",
        transaction_id=TRANSACTION_ID,
        operation=OperationType.RECIPIENT_CREDIT,
        rail_id=RailId.MOMO_B,
        source=FundsLocation.GH_SETTLEMENT_ACCOUNT,
        destination=FundsLocation.RECIPIENT_ENDPOINT,
        amount=Money.of("1830.00", Currency.GHS),
        outcome=outcome,
        idempotency_key="k",
        failure=failure,
        started_at=NOW,
        completed_at=NOW,
    )


def test_definitive_failure_must_be_pre_acceptance():
    """A 5xx after acceptance does NOT prove no value moved: it must be UNKNOWN."""
    post = FailureDetail(stage=FailureStage.POST_ACCEPTANCE, http_status=503, message="x")
    with pytest.raises(ValidationError, match="PRE_ACCEPTANCE"):
        _attempt(AttemptOutcome.DEFINITIVE_FAILED, post)
    assert _attempt(AttemptOutcome.UNKNOWN, post).outcome is AttemptOutcome.UNKNOWN
    pre = FailureDetail(stage=FailureStage.PRE_ACCEPTANCE, http_status=503, message="x")
    assert _attempt(AttemptOutcome.DEFINITIVE_FAILED, pre).failure.http_status == 503


def test_attempt_failure_details_consistency():
    with pytest.raises(ValidationError):
        _attempt(AttemptOutcome.UNKNOWN, None)
    pre = FailureDetail(stage=FailureStage.PRE_ACCEPTANCE, message="x")
    with pytest.raises(ValidationError):
        _attempt(AttemptOutcome.SUCCEEDED, pre)


def test_attempt_must_follow_corridor_flow():
    with pytest.raises(ValidationError):
        OperationAttempt.model_validate(
            _attempt(AttemptOutcome.SUCCEEDED, None).model_dump()
            | {"source": FundsLocation.SENDER_ACCOUNT}
        )


async def test_plan_hash_detects_tampering(world, txn_id):
    plan = await world.service.create_recovery_plan(txn_id)
    data = plan.model_dump()
    with pytest.raises(ValidationError, match="plan_hash"):
        RecoveryPlan.model_validate(data | {"rail_id": RailId.BANK_MOMO_BRIDGE})
    with pytest.raises(ValidationError, match="plan_hash"):
        RecoveryPlan.model_validate(data | {"incremental_fee": Money.of("0.01", GBP)})


async def test_plan_can_never_source_from_sender(world, txn_id):
    plan = await world.service.create_recovery_plan(txn_id)
    fields = {
        name: getattr(plan, name) for name in RecoveryPlan.model_fields if name != "plan_hash"
    }
    with pytest.raises(ValidationError, match="never restart from the sender"):
        RecoveryPlan.build(**(fields | {"source": FundsLocation.SENDER_ACCOUNT}))
