"""Failure evidence: Pydantic AI extracts, the deterministic FailureVerifier decides, and it
fails closed. No extraction, however confident, can authorise a payout by itself."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic_ai import ModelResponse
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.function import AgentInfo, FunctionModel

from sikarescue.agent.advisor import AdvisorConfig, Orchestrator
from sikarescue.agent.evidence import EvidenceExtractor
from sikarescue.demo_data.incidents import IncidentScenario, momo_a_catalog
from sikarescue.demo_data.sk10421 import build_demo_world
from sikarescue.errors import (
    ApprovalRequiredError,
    EvidencePendingError,
    IllegalTransitionError,
)
from sikarescue.models import (
    AttemptOutcome,
    EvidenceSource,
    ExtractedFailureEvidence,
    FailureEvidence,
    FundsCertainty,
    OperationType,
    PositionStatus,
    RailId,
    RecoveryState,
    SettlementLegStatus,
)
from sikarescue.services.evidence import envelope_evidence, verify_failure_evidence

from helpers import (
    DEFINITIVE_EXTRACTION,
    UNKNOWN_EXTRACTION,
    plan_and_approve,
    scripted_extraction,
)

CATALOG = momo_a_catalog()


def _world(scenario: IncidentScenario):
    return build_demo_world(incident=scenario)


def _incident(world):
    incident = world.repository.get(world.transaction_id).incident
    assert incident is not None
    return incident


def _evidence(world, draft: dict[str, Any]) -> FailureEvidence:
    extraction = ExtractedFailureEvidence.model_validate(draft)
    return FailureEvidence.bind(_incident(world), extraction, EvidenceSource.PYDANTIC_AI)


def _verdict(scenario: IncidentScenario, draft: dict[str, Any]):
    world = _world(scenario)
    return verify_failure_evidence(_incident(world), _evidence(world, draft), CATALOG)


def _extractor(model) -> EvidenceExtractor:
    return EvidenceExtractor(AdvisorConfig(mode="pydantic"), model=model)


# ============================================================ the deterministic verifier


def test_known_pre_acceptance_evidence_becomes_definitive_failed():
    verdict = _verdict(IncidentScenario.DEFINITIVE, DEFINITIVE_EXTRACTION)
    assert verdict.classification is AttemptOutcome.DEFINITIVE_FAILED
    assert all(c.passed for c in (*verdict.checks, *verdict.requirements))
    assert verdict.catalog_meaning and "never queued" in verdict.catalog_meaning


def test_ambiguous_timeout_evidence_becomes_unknown():
    verdict = _verdict(IncidentScenario.UNKNOWN, UNKNOWN_EXTRACTION)
    assert verdict.classification is AttemptOutcome.UNKNOWN
    assert all(c.passed for c in verdict.checks)  # honest evidence, just insufficient
    failed = {r.name for r in verdict.requirements if not r.passed}
    assert "provider_response_received" in failed
    assert "may already have been credited" in verdict.reasons[-1]


def test_confident_hallucinated_rejection_on_a_timeout_is_still_unknown():
    # The model claims a definitive pre-acceptance rejection for a payload with no response.
    hallucinated = DEFINITIVE_EXTRACTION | {
        "provider_code": None,
        "provider_message": None,
        "provider_reference": None,
        "evidence_fragments": ["Your request may or may not have been processed."],
    }
    verdict = _verdict(IncidentScenario.UNKNOWN, hallucinated)
    assert verdict.classification is AttemptOutcome.UNKNOWN
    failed = {c.name for c in verdict.checks if not c.passed}
    assert {"transport_consistent", "internally_consistent"} <= failed


@pytest.mark.parametrize(
    ("change", "failed_check"),
    [
        ({"acceptance_stage": "POST_ACCEPTANCE"}, "internally_consistent"),  # MA-4017 is pre
        ({"explicit_rejection": False}, "internally_consistent"),
        ({"evidence_fragments": ["the payout was rejected"]}, "citations_grounded"),
        ({"provider_code": "MA-4018"}, "citations_grounded"),  # not in the payload
        ({"provider": "MOMO_B"}, "provider_match"),
        ({"transport_outcome": "TIMEOUT"}, "transport_consistent"),
    ],
)
def test_contradictory_or_ungrounded_evidence_fails_closed(change, failed_check):
    verdict = _verdict(IncidentScenario.DEFINITIVE, DEFINITIVE_EXTRACTION | change)
    assert verdict.classification is AttemptOutcome.UNKNOWN
    assert failed_check in {c.name for c in verdict.checks if not c.passed}


def test_evidence_bound_to_another_payload_fails_closed():
    definitive, unknown = _world(IncidentScenario.DEFINITIVE), _world(IncidentScenario.UNKNOWN)
    foreign = _evidence(unknown, UNKNOWN_EXTRACTION)
    verdict = verify_failure_evidence(_incident(definitive), foreign, CATALOG)
    assert verdict.classification is AttemptOutcome.UNKNOWN
    assert not next(c for c in verdict.checks if c.name == "payload_binding").passed


def test_undocumented_code_or_missing_catalog_is_unknown():
    world = _world(IncidentScenario.DEFINITIVE)
    evidence = _evidence(world, DEFINITIVE_EXTRACTION)
    assert (
        verify_failure_evidence(_incident(world), evidence, None).classification
        is AttemptOutcome.UNKNOWN
    )


def test_envelope_only_fallback_can_never_prove_a_definitive_failure():
    world = _world(IncidentScenario.DEFINITIVE)
    evidence = envelope_evidence(_incident(world))
    assert evidence.extracted_by is EvidenceSource.ENVELOPE_ONLY
    verdict = verify_failure_evidence(_incident(world), evidence, CATALOG)
    assert verdict.classification is AttemptOutcome.UNKNOWN


# ============================================================ recording the classification


async def test_planning_is_refused_until_evidence_is_classified():
    world = _world(IncidentScenario.DEFINITIVE)
    state = world.service.get_transaction_state(world.transaction_id)
    assert state.evidence_pending and state.funds_certainty is FundsCertainty.UNCERTAIN
    assert state.funds_position.position_status is PositionStatus.IN_FLIGHT
    assert state.legs[-1].status is SettlementLegStatus.AWAITING_EVIDENCE
    with pytest.raises(EvidencePendingError):
        await world.service.create_recovery_plan(world.transaction_id)
    aggregate = world.repository.get(world.transaction_id)
    assert aggregate.state is RecoveryState.FAILED  # refused, not escalated
    assert world.gateway.requests == []


async def test_extraction_cannot_directly_authorise_a_payout():
    world = _world(IncidentScenario.DEFINITIVE)
    classification = await world.service.classify_provider_incident(
        world.transaction_id, _evidence(world, DEFINITIVE_EXTRACTION)
    )
    assert classification.recorded_outcome is AttemptOutcome.DEFINITIVE_FAILED
    aggregate = world.repository.get(world.transaction_id)
    # Classification only records what happened: no plan, no approval, no value movement.
    assert aggregate.state is RecoveryState.DIAGNOSING
    assert aggregate.plans == {} and aggregate.approvals == {}
    assert world.gateway.requests == [] and len(aggregate.journal.effects()) == 3
    # A payout still needs the deterministic plan AND a human approval of its exact hash.
    plan = await world.service.create_recovery_plan(world.transaction_id)
    with pytest.raises(ApprovalRequiredError):
        await world.service.execute_recovery(plan.plan_id)
    assert world.gateway.requests == []
    # The extraction schema has no field that could authorise, rank or execute anything.
    assert not {"approve", "approved", "plan_id", "rail_id", "execute", "recommended_route"} & (
        set(ExtractedFailureEvidence.model_fields)
    )


async def test_definitive_classification_then_recovers_with_one_debit_and_one_credit():
    world = _world(IncidentScenario.DEFINITIVE)
    await world.service.classify_provider_incident(
        world.transaction_id, _evidence(world, DEFINITIVE_EXTRACTION)
    )
    plan = await plan_and_approve(world)
    assert plan.rail_id is RailId.MOMO_B
    await world.service.execute_recovery(plan.plan_id)
    result = await world.service.reconcile_transaction(world.transaction_id)
    assert result.reconciled
    assert (result.sender_debit_count, result.recipient_credit_count) == (1, 1)
    assert result.duplicate_sender_debits == 0


async def test_unknown_classification_goes_to_manual_review_and_moves_nothing():
    world = _world(IncidentScenario.UNKNOWN)
    classification = await world.service.classify_provider_incident(
        world.transaction_id, _evidence(world, UNKNOWN_EXTRACTION)
    )
    assert classification.recorded_outcome is AttemptOutcome.UNKNOWN
    state = world.service.get_transaction_state(world.transaction_id)
    assert state.recovery_state is RecoveryState.MANUAL_REVIEW
    assert state.funds_position.position_status is PositionStatus.UNCERTAIN
    with pytest.raises(IllegalTransitionError):
        await world.service.create_recovery_plan(world.transaction_id)
    assert world.gateway.requests == []


async def test_success_evidence_is_never_booked_as_a_credit():
    world = _world(IncidentScenario.DEFINITIVE)
    incident = _incident(world)
    # A (synthetic) success claim that is fully grounded in a success payload.
    payload_success = incident.model_copy(
        update={"raw_payload": incident.raw_payload.replace("MA-4017", "MA-2001")}
    )
    success = ExtractedFailureEvidence.model_validate(
        DEFINITIVE_EXTRACTION
        | {
            "provider_code": "MA-2001",
            "provider_message": None,
            "acceptance_stage": "POST_ACCEPTANCE",
            "explicit_rejection": False,
            "evidence_fragments": ['"code": "MA-2001"'],
        }
    )
    rebound = payload_success.model_copy(
        update={"raw_payload_digest": _digest(payload_success.raw_payload)}
    )
    evidence = FailureEvidence.bind(rebound, success, EvidenceSource.PYDANTIC_AI)
    verdict = verify_failure_evidence(rebound, evidence, CATALOG)
    assert verdict.classification is AttemptOutcome.SUCCEEDED
    # Recorded through the service, it becomes UNKNOWN -> manual booking, never an effect.
    aggregate = world.repository.get(world.transaction_id)
    aggregate.incident = rebound
    classification = await world.service.classify_provider_incident(world.transaction_id, evidence)
    assert classification.verdict.classification is AttemptOutcome.SUCCEEDED
    assert classification.recorded_outcome is AttemptOutcome.UNKNOWN
    assert aggregate.state is RecoveryState.MANUAL_REVIEW
    assert not aggregate.journal.has_effect(OperationType.RECIPIENT_CREDIT)


async def test_classification_is_write_once():
    world = _world(IncidentScenario.DEFINITIVE)
    first = await world.service.classify_provider_incident(
        world.transaction_id, _evidence(world, DEFINITIVE_EXTRACTION)
    )
    second = await world.service.classify_provider_incident(
        world.transaction_id,
        _evidence(world, UNKNOWN_EXTRACTION),  # cannot overwrite
    )
    assert second == first
    attempts = world.repository.get(world.transaction_id).journal.attempts()
    assert sum(a.rail_id is RailId.MOMO_A for a in attempts) == 1


def _digest(text: str) -> str:
    from sikarescue.models import sha256_text

    return sha256_text(text)


# ============================================================ the extraction agent


async def test_agent_extraction_is_bound_to_the_payload_and_classified_deterministically():
    world = _world(IncidentScenario.DEFINITIVE)
    aggregate = world.repository.get(world.transaction_id)
    extraction = await _extractor(scripted_extraction(DEFINITIVE_EXTRACTION)).extract(
        _incident(world), aggregate.instruction
    )
    assert extraction.orchestrator is Orchestrator.PYDANTIC_AI
    evidence = extraction.evidence
    assert evidence.extracted_by is EvidenceSource.PYDANTIC_AI
    assert evidence.raw_payload_digest == _incident(world).raw_payload_digest  # set by code
    verdict = verify_failure_evidence(_incident(world), evidence, CATALOG)
    assert verdict.classification is AttemptOutcome.DEFINITIVE_FAILED


async def test_ungrounded_citations_are_sent_back_and_corrected():
    world = _world(IncidentScenario.DEFINITIVE)
    invented = DEFINITIVE_EXTRACTION | {"evidence_fragments": ["provider confirmed no debit"]}
    extraction = await _extractor(scripted_extraction(invented, DEFINITIVE_EXTRACTION)).extract(
        _incident(world), world.repository.get(world.transaction_id).instruction
    )
    assert extraction.orchestrator is Orchestrator.PYDANTIC_AI
    assert extraction.rejected_drafts == 1


async def test_persistently_ungrounded_extraction_falls_back_closed():
    world = _world(IncidentScenario.DEFINITIVE)
    invented = DEFINITIVE_EXTRACTION | {"provider_code": "MA-9999"}
    extraction = await _extractor(scripted_extraction(invented)).extract(
        _incident(world), world.repository.get(world.transaction_id).instruction
    )
    assert extraction.orchestrator is Orchestrator.DETERMINISTIC_FALLBACK
    assert "not in the payload" in extraction.fallback_reason
    assert extraction.evidence.extracted_by is EvidenceSource.ENVELOPE_ONLY
    verdict = verify_failure_evidence(_incident(world), extraction.evidence, CATALOG)
    assert verdict.classification is AttemptOutcome.UNKNOWN


async def test_model_outage_falls_back_closed_to_unknown():
    def broken(messages, info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=503, model_name="gemini", body=None)

    world = _world(IncidentScenario.DEFINITIVE)
    extraction = await _extractor(FunctionModel(broken)).extract(
        _incident(world), world.repository.get(world.transaction_id).instruction
    )
    assert extraction.orchestrator is Orchestrator.DETERMINISTIC_FALLBACK
    assert "HTTP 503" in extraction.fallback_reason
    verdict = verify_failure_evidence(_incident(world), extraction.evidence, CATALOG)
    assert verdict.classification is AttemptOutcome.UNKNOWN


async def test_deterministic_mode_never_calls_a_model():
    world = _world(IncidentScenario.DEFINITIVE)
    extraction = await EvidenceExtractor(AdvisorConfig(mode="deterministic")).extract(
        _incident(world), world.repository.get(world.transaction_id).instruction
    )
    assert extraction.orchestrator is Orchestrator.DETERMINISTIC
    assert extraction.evidence.extracted_by is EvidenceSource.ENVELOPE_ONLY


def test_synthetic_payloads_carry_no_recipient_personal_data():
    from sikarescue.services.model_boundary import assert_no_recipient_pii

    for scenario in IncidentScenario:
        world = _world(scenario)
        aggregate = world.repository.get(world.transaction_id)
        assert_no_recipient_pii(_incident(world).raw_payload, aggregate.instruction)
