"""Recovery decision context: compact, evidence-preserving, PII-free, and checkable."""

from __future__ import annotations

import json

import pytest

from sikarescue.agent.advisor import deterministic_advice
from sikarescue.agent.recovery_agent import advice_problems
from sikarescue.compute.scenarios import workload_config
from sikarescue.demo_data.sk10421 import RECIPIENT_NAME, TRANSACTION_ID, build_demo_world
from sikarescue.errors import PIILeakError
from sikarescue.models import (
    AdviceReasonCode,
    AttemptOutcome,
    FundsLocation,
    OperationType,
    RailId,
    RecoveryAdvice,
    RejectionReason,
    ScenarioId,
)
from sikarescue.services.model_boundary import (
    DECISION_INVARIANTS,
    build_decision_context,
    build_verbose_decision_context,
    release_to_model,
)


async def _packets(world):
    plan = await world.service.create_recovery_plan(TRANSACTION_ID)
    aggregate = world.repository.get(TRANSACTION_ID)
    return (
        plan,
        build_decision_context(aggregate, plan),
        build_verbose_decision_context(aggregate, plan),
    )


async def test_compact_packet_preserves_every_decision_fact(world):
    plan, packet, _ = await _packets(world)

    assert packet.funds_location is FundsLocation.GH_SETTLEMENT_ACCOUNT
    assert packet.sender_debited and not packet.safe_to_restart_from_origin
    assert [e.operation for e in packet.completed_effects] == [
        OperationType.SENDER_DEBIT,
        OperationType.FX_CONVERSION,
        OperationType.GH_SETTLEMENT,
    ]
    assert packet.outstanding_obligation.amount == "1830.00 GHS"
    assert packet.outstanding_obligation.operation is OperationType.RECIPIENT_CREDIT
    assert packet.failure.rail_id is RailId.MOMO_A
    assert packet.failure.outcome is AttemptOutcome.DEFINITIVE_FAILED
    assert packet.failure.value_moved is False
    assert [(r.rail_id, r.rank) for r in packet.candidate_routes] == [
        (RailId.MOMO_B, 1),
        (RailId.BANK_MOMO_BRIDGE, 2),
    ]
    assert {r.rail_id: r.reasons for r in packet.rejected_routes} == {
        RailId.MOMO_A: (
            RejectionReason.RAIL_UNAVAILABLE,
            RejectionReason.FAILED_EARLIER_FOR_TRANSACTION,
        ),
        RailId.TOKEN_BRIDGE: (RejectionReason.POLICY_DENIED,),
    }
    best = packet.candidate_routes[0]
    assert best.incremental_fee_gbp == "0.18" and best.quoted_arrival_seconds == 74
    assert [s.scenario for s in best.stress] == [
        ScenarioId.CONGESTION,
        ScenarioId.CORRELATED_FAILURE,
    ]
    assert packet.compute.simulated_outcomes == plan.compute.simulated_trials
    selected = packet.selected_plan
    assert (selected.plan_id, selected.rail_id, selected.incremental_fee_gbp) == (
        plan.plan_id,
        RailId.MOMO_B,
        "0.18",
    )
    assert selected.plan_hash_prefix == plan.plan_hash[:12] and selected.approval_required
    assert AdviceReasonCode.SENDER_ALREADY_DEBITED in packet.reason_codes
    assert packet.invariants == DECISION_INVARIANTS
    assert any("Never replay a completed value-moving effect" in i for i in packet.invariants)


async def test_compact_packet_is_much_smaller_than_the_naive_dump(world):
    _, packet, verbose = await _packets(world)
    compact_bytes = len(packet.model_dump_json())
    verbose_bytes = len(json.dumps(verbose, default=str))
    assert verbose_bytes > 4 * compact_bytes, (compact_bytes, verbose_bytes)


async def test_both_packets_pass_the_pii_gate_and_a_leak_is_refused(world):
    _, packet, verbose = await _packets(world)
    instruction = world.repository.get(TRANSACTION_ID).instruction
    assert release_to_model(packet, instruction) is packet
    assert release_to_model(verbose, instruction) is verbose
    with pytest.raises(PIILeakError):
        release_to_model(verbose | {"note": f"recipient is {RECIPIENT_NAME}"}, instruction)


@pytest.mark.parametrize("workload", ["quick", "stress"])
async def test_deterministic_advice_is_consistent_with_the_plan(workload):
    world = build_demo_world(simulation=workload_config(workload, trials_per_scenario=200))
    _, packet, _ = await _packets(world)
    advice = deterministic_advice(packet)
    assert advice_problems(advice, packet) == []
    assert RecoveryAdvice.model_validate_json(advice.model_dump_json()) == advice


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"plan_id": "plan_000000000000"}, "plan_id"),
        ({"recommended_route": "BANK_MOMO_BRIDGE"}, "recommended_route"),
        ({"incremental_fee_gbp": "0.09"}, "incremental_fee_gbp"),
        ({"estimated_arrival_seconds": 25}, "estimated_arrival_seconds"),
        ({"simulated_reliability": 0.999}, "simulated_reliability"),
        ({"funds_location": "SENDER_ACCOUNT"}, "funds_location"),
        ({"rejected_routes": []}, "rejected_routes"),
    ],
)
async def test_advice_problems_catch_every_contradicted_fact(world, changes, field):
    _, packet, _ = await _packets(world)
    tampered = RecoveryAdvice.model_validate(
        deterministic_advice(packet).model_dump(mode="json") | changes
    )
    problems = advice_problems(tampered, packet)
    assert problems and field in problems[0]


async def test_reason_codes_must_be_supported_by_the_analysis(world):
    _, packet, _ = await _packets(world)
    dropped = packet.reason_codes[0]
    narrower = packet.model_copy(update={"reason_codes": packet.reason_codes[1:]})
    advice = deterministic_advice(packet)  # still cites the code the analysis no longer supports
    problems = advice_problems(advice, narrower)
    assert problems == [f"reason_codes not supported by the analysis: {dropped.value}"]
