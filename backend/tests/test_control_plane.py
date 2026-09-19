"""Derived control-plane artifacts: effect graph, funds position, safe action frontier, ledger
preview and the naive-retry counterfactual. All derived, all pure, none authoritative."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from sikarescue.demo_data.incidents import IncidentScenario
from sikarescue.demo_data.sk10421 import build_demo_world
from sikarescue.models import (
    AttemptOutcome,
    Currency,
    EffectNodeKind,
    EffectNodeStatus,
    EvidenceSource,
    ExtractedFailureEvidence,
    FailureEvidence,
    FrontierAction,
    FrontierActionKind,
    FundsCertainty,
    FundsLocation,
    FundsPosition,
    Money,
    OperationType,
    PositionStatus,
    RailId,
    SafeActionFrontier,
)

from helpers import DEFINITIVE_EXTRACTION, UNKNOWN_EXTRACTION, plan_and_approve

GHS_1830 = Money.of("1830.00", Currency.GHS)
PAYOUT_KINDS = {FrontierActionKind.PAYOUT}


async def _classified(scenario: IncidentScenario):
    world = build_demo_world(incident=scenario)
    incident = world.repository.get(world.transaction_id).incident
    assert incident is not None
    draft = DEFINITIVE_EXTRACTION if scenario is IncidentScenario.DEFINITIVE else UNKNOWN_EXTRACTION
    evidence = FailureEvidence.bind(
        incident, ExtractedFailureEvidence.model_validate(draft), EvidenceSource.PYDANTIC_AI
    )
    await world.service.classify_provider_incident(world.transaction_id, evidence)
    return world


def _state_fingerprint(world) -> tuple:
    """Everything a pure read must leave untouched."""
    aggregate = world.repository.get(world.transaction_id)
    return (
        aggregate.state,
        aggregate.revision,
        tuple(aggregate.journal.entries),
        len(aggregate.audit),
        dict(aggregate.plan_status),
        dict(aggregate.approvals),
        dict(aggregate.executions),
        aggregate.current_plan_id,
        dict(world.liquidity._balances),
        tuple(world.gateway.requests),
    )


# ============================================================ financial effect graph


async def test_effect_graph_derives_from_the_journal(world, txn_id):
    graph = world.service.get_effect_graph(txn_id)
    journal = world.repository.get(txn_id).journal
    kinds = [n.kind for n in graph.nodes]
    assert kinds == [
        EffectNodeKind.PAYMENT_INTENT,
        EffectNodeKind.SENDER_DEBIT,
        EffectNodeKind.FX_CONVERSION,
        EffectNodeKind.GH_SETTLEMENT,
        EffectNodeKind.RECIPIENT_PAYOUT,
    ]
    assert graph.edges == tuple(
        (graph.nodes[i].node_id, graph.nodes[i + 1].node_id) for i in range(4)
    )
    for node in graph.nodes[1:4]:  # every posted node carries exactly the journal's facts
        effect = journal.effect(node.operation)
        assert node.status is EffectNodeStatus.COMPLETE and node.node_id == effect.effect_key
        assert (node.source_amount, node.destination_amount, node.rail_id) == (
            effect.source_amount,
            effect.destination_amount,
            effect.rail_id,
        )
    fx = graph.node(EffectNodeKind.FX_CONVERSION)
    assert fx.fx_rate == Decimal("15.25") and fx.destination_amount == GHS_1830
    payout = graph.node(EffectNodeKind.RECIPIENT_PAYOUT)
    assert payout.status is EffectNodeStatus.FAILED and payout.source_amount is None
    assert [(a.rail_id, a.outcome) for a in payout.attempts] == [
        (RailId.MOMO_A, AttemptOutcome.DEFINITIVE_FAILED)
    ]
    assert payout.outstanding == GHS_1830
    assert graph.node(EffectNodeKind.PAYMENT_INTENT).status is EffectNodeStatus.OPEN

    plan = await plan_and_approve(world)
    await world.service.execute_recovery(plan.plan_id)
    graph = world.service.get_effect_graph(txn_id)
    payout = graph.node(EffectNodeKind.RECIPIENT_PAYOUT)
    assert payout.status is EffectNodeStatus.COMPLETE and payout.rail_id is RailId.MOMO_B
    assert [a.rail_id for a in payout.attempts] == [RailId.MOMO_A, RailId.MOMO_B]
    assert payout.outstanding is None
    assert graph.node(EffectNodeKind.PAYMENT_INTENT).status is EffectNodeStatus.FULFILLED


async def test_effect_graph_shows_unclassified_and_unknown_payouts():
    pending = build_demo_world(incident=IncidentScenario.UNKNOWN)
    node = pending.service.get_effect_graph(pending.transaction_id).node(
        EffectNodeKind.RECIPIENT_PAYOUT
    )
    assert node.status is EffectNodeStatus.AWAITING_EVIDENCE and node.attempts == ()
    world = await _classified(IncidentScenario.UNKNOWN)
    node = world.service.get_effect_graph(world.transaction_id).node(
        EffectNodeKind.RECIPIENT_PAYOUT
    )
    assert node.status is EffectNodeStatus.UNKNOWN


# ============================================================ funds position


async def test_funds_position_after_a_definitive_failure_is_available():
    world = await _classified(IncidentScenario.DEFINITIVE)
    position = world.service.get_transaction_state(world.transaction_id).funds_position
    assert position.amount == GHS_1830
    assert position.last_confirmed_location is FundsLocation.GH_SETTLEMENT_ACCOUNT
    assert position.position_status is PositionStatus.AVAILABLE
    assert position.available_for_automatic_action is True
    assert position.certainty is FundsCertainty.PROVEN
    assert position.derived_from_effect_ids == tuple(
        f"{world.transaction_id}:{s}" for s in ("sender_debit", "fx", "gh_settlement")
    )


async def test_funds_position_after_an_unknown_outcome_is_uncertain():
    world = await _classified(IncidentScenario.UNKNOWN)
    position = world.service.get_transaction_state(world.transaction_id).funds_position
    assert position.last_confirmed_location is FundsLocation.GH_SETTLEMENT_ACCOUNT
    assert position.position_status is PositionStatus.UNCERTAIN
    assert position.available_for_automatic_action is False
    assert position.certainty is FundsCertainty.UNCERTAIN
    assert "may already have been credited" in position.reason


async def test_funds_position_is_final_after_recovery(world, txn_id):
    plan = await plan_and_approve(world)
    await world.service.execute_recovery(plan.plan_id)
    position = world.service.get_transaction_state(txn_id).funds_position
    assert position.position_status is PositionStatus.FINAL
    assert position.last_confirmed_location is FundsLocation.RECIPIENT_ENDPOINT
    assert not position.available_for_automatic_action


def test_funds_position_cannot_claim_availability_it_does_not_have(world, txn_id):
    position = world.service.get_transaction_state(txn_id).funds_position
    for bad in (
        {"position_status": PositionStatus.UNCERTAIN},  # still PROVEN + available
        {"certainty": FundsCertainty.UNCERTAIN},  # AVAILABLE but uncertain
        {
            "position_status": PositionStatus.UNCERTAIN,
            "certainty": FundsCertainty.UNCERTAIN,
            "reason": "unknown outcome",
        },  # uncertain yet available for automatic action
    ):
        with pytest.raises(ValidationError):
            FundsPosition.model_validate(position.model_dump() | bad)


# ============================================================ safe action frontier


async def test_unknown_frontier_contains_no_payout_actions():
    world = await _classified(IncidentScenario.UNKNOWN)
    frontier = world.service.get_safe_action_frontier(world.transaction_id)
    assert frontier.basis == "UNCERTAIN_FUNDS" and not frontier.payout_actions_permitted
    kinds = {a.kind for a in frontier.actions}
    assert kinds == {
        FrontierActionKind.QUERY_PROVIDER,
        FrontierActionKind.WAIT_FOR_PROVIDER_EVIDENCE,
        FrontierActionKind.MANUAL_REVIEW,
    }
    assert not any(a.moves_value for a in frontier.actions)
    assert frontier.counts.candidates == 0 and frontier.plan_id is None
    # The artifact itself refuses a payout action while funds are uncertain.
    payout = FrontierAction(
        action_id="payout_momo_b",
        kind=FrontierActionKind.PAYOUT,
        label="pay",
        rail_id=RailId.MOMO_B,
        moves_value=True,
    )
    with pytest.raises(ValidationError, match="cannot contain a payout"):
        SafeActionFrontier.model_validate(
            frontier.model_dump() | {"actions": (*frontier.actions, payout)}
        )


async def test_pending_evidence_frontier_offers_classification_only():
    world = build_demo_world(incident=IncidentScenario.DEFINITIVE)
    frontier = world.service.get_safe_action_frontier(world.transaction_id)
    assert frontier.basis == "PENDING_EVIDENCE" and not frontier.payout_actions_permitted
    assert frontier.actions[0].kind is FrontierActionKind.CLASSIFY_PROVIDER_EVIDENCE
    assert not {a.kind for a in frontier.actions} & PAYOUT_KINDS


async def test_definitive_failure_frontier_contains_payout_actions():
    world = await _classified(IncidentScenario.DEFINITIVE)
    before = world.service.get_safe_action_frontier(world.transaction_id)
    assert before.basis == "CANDIDATES" and before.payout_actions_permitted
    c = before.counts
    assert (c.candidates, c.rejected_before_simulation, c.eligible, c.simulated, c.selected) == (
        4,
        2,
        2,
        0,
        0,
    )

    plan = await world.service.create_recovery_plan(world.transaction_id)
    frontier = world.service.get_safe_action_frontier(world.transaction_id)
    c = frontier.counts
    assert frontier.basis == "PLAN" and frontier.plan_id == plan.plan_id
    assert (c.candidates, c.rejected_before_simulation, c.eligible, c.simulated, c.selected) == (
        4,
        2,
        2,
        2,
        1,
    )
    selected = next(a for a in frontier.actions if a.selected)
    assert (selected.rail_id, selected.rank) == (RailId.MOMO_B, 1)
    rejected = {a.rail_id for a in frontier.actions if a.rejection_reasons}
    assert rejected == {RailId.MOMO_A, RailId.TOKEN_BRIDGE}
    assert not any(a.simulated for a in frontier.actions if a.rejection_reasons)


async def test_building_the_frontier_is_pure():
    world = await _classified(IncidentScenario.DEFINITIVE)
    await world.service.create_recovery_plan(world.transaction_id)
    before = _state_fingerprint(world)
    for _ in range(3):
        world.service.get_safe_action_frontier(world.transaction_id)
    assert _state_fingerprint(world) == before


async def test_settled_frontier_requires_no_action(world, txn_id):
    plan = await plan_and_approve(world)
    await world.service.execute_recovery(plan.plan_id)
    frontier = world.service.get_safe_action_frontier(txn_id)
    assert frontier.basis == "SETTLED"
    assert [a.kind for a in frontier.actions] == [FrontierActionKind.NO_ACTION_REQUIRED]


# ============================================================ ledger preview


def _row(preview, label):
    return next(c for c in preview.changes if c.label == label)


async def test_ledger_preview_shows_current_vs_proposed(world, txn_id):
    plan = await world.service.create_recovery_plan(txn_id)
    preview = world.service.preview_recovery(plan.plan_id)
    assert preview.valid and preview.proposed_rail is RailId.MOMO_B
    assert preview.proposed_effect_key == f"{txn_id}:recipient_credit"
    assert (_row(preview, "Sender debits").current, _row(preview, "Sender debits").proposed) == (
        "1",
        "1",
    )
    assert not _row(preview, "FX conversions").changed
    settlement = _row(preview, "Held at GH_SETTLEMENT_ACCOUNT")
    assert (settlement.current, settlement.proposed) == ("GHS 1,830.00", "GHS 0.00")
    recipient = _row(preview, "Held at RECIPIENT_ENDPOINT")
    assert (recipient.current, recipient.proposed) == ("GHS 0.00", "GHS 1,830.00")
    credits = _row(preview, "Recipient credits")
    assert (credits.current, credits.proposed) == ("0", "1")
    assert _row(preview, "Duplicate sender debits").proposed == "0"
    assert _row(preview, "Outstanding obligation").proposed == "none"
    assert preview.proposed.funds_location is FundsLocation.RECIPIENT_ENDPOINT


async def test_ledger_preview_never_mutates_state(world, txn_id):
    plan = await plan_and_approve(world)
    before = _state_fingerprint(world)
    for _ in range(3):
        assert world.service.preview_recovery(plan.plan_id).valid
    assert _state_fingerprint(world) == before
    # ...and execution afterwards is unaffected by the dry run.
    await world.service.execute_recovery(plan.plan_id)
    assert world.repository.get(txn_id).journal.count_effects(OperationType.RECIPIENT_CREDIT) == 1


async def test_ledger_preview_matches_what_execution_actually_posts(world, txn_id):
    plan = await plan_and_approve(world)
    projected = world.service.preview_recovery(plan.plan_id).proposed
    await world.service.execute_recovery(plan.plan_id)
    from sikarescue.services.control_plane import ledger_snapshot

    aggregate = world.repository.get(txn_id)
    actual = ledger_snapshot(aggregate, aggregate.journal)
    ignore = {"revision"}  # execution also appends non-financial markers
    assert projected.model_dump(exclude=ignore) == actual.model_dump(exclude=ignore)


async def test_stale_plan_invalidates_the_ledger_preview(world, txn_id):
    plan = await plan_and_approve(world)
    quote = world.registry.quote(RailId.MOMO_B)
    world.registry.replace_quote(quote.model_copy(update={"quote_id": "qte_0000000000cc"}))
    before = _state_fingerprint(world)
    preview = world.service.preview_recovery(plan.plan_id)
    assert not preview.valid and preview.proposed is None and preview.changes == ()
    assert any("quote changed" in r for r in preview.invalidation_reasons)
    assert _state_fingerprint(world) == before  # invalidated, but nothing was marked stale


# ============================================================ naive retry counterfactual


async def test_naive_retry_comparison_is_pure_and_names_the_repeats(world, txn_id):
    await world.service.create_recovery_plan(txn_id)
    before = _state_fingerprint(world)
    result = world.service.compare_naive_retry(txn_id)
    assert _state_fingerprint(world) == before
    assert [s.already_completed for s in result.naive_steps] == [True, True, True, False]
    assert result.naive_repeated_effects == 3
    assert result.naive_extra_sender_debit == Money.of("120.00", Currency.GBP)
    assert "charges the sender again" in result.naive_steps[0].risk
    assert "MOMO_A" in result.naive_steps[3].risk  # retries the rail that already failed
    assert result.duplicate_recipient_credit_risk is False
    ours = result.sikarescue_steps
    assert len(ours) == 1 and result.sikarescue_repeated_effects == 0
    assert (ours[0].operation, ours[0].rail_id, ours[0].amount) == (
        OperationType.RECIPIENT_CREDIT,
        RailId.MOMO_B,
        GHS_1830,
    )


async def test_naive_retry_after_unknown_flags_duplicate_credit_risk():
    world = await _classified(IncidentScenario.UNKNOWN)
    before = _state_fingerprint(world)
    result = world.service.compare_naive_retry(world.transaction_id)
    assert _state_fingerprint(world) == before
    assert result.duplicate_recipient_credit_risk is True
    assert "may credit the recipient twice" in result.naive_steps[3].risk
    assert result.sikarescue_steps == () and "Manual review" in result.sikarescue_action
