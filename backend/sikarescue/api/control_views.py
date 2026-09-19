"""Presentation of the control-plane artifacts: provider evidence, funds position, effect
graph, safe action frontier, ledger preview, naive-retry counterfactual and the systemic
outage analysis.

Every value comes from the deterministic domain objects; money and percentages are formatted
here so the frontend renders and never computes.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from sikarescue.agent.advisor import Orchestrator
from sikarescue.agent.evidence import EvidenceExtraction
from sikarescue.api.formatting import money, pct
from sikarescue.models import (
    EffectNodeKind,
    FinancialEffectGraph,
    FundsCertainty,
    IncidentClassification,
    LedgerPreview,
    ProviderIncident,
    RailId,
    RetryCounterfactual,
    SafeActionFrontier,
)
from sikarescue.models.outage import OutageAnalysis, UnservedReason


class View(BaseModel):
    model_config = ConfigDict(frozen=True)


RailNames = dict[RailId, str]


def _name(names: RailNames, rail: RailId | None) -> str | None:
    return names.get(rail, rail.value) if rail else None


# ------------------------------------------------------------------ provider evidence

_CHECK_LABELS = {
    "payload_binding": "Bound to this exact payload",
    "provider_match": "Names the provider we called",
    "citations_grounded": "Every citation appears verbatim",
    "transport_consistent": "Matches our client's transport log",
    "internally_consistent": "Consistent with itself and the code catalog",
    "provider_response_received": "Provider response received",
    "explicit_pre_acceptance_rejection": "Explicit rejection before acceptance",
    "documented_rejection_code": "Documented pre-acceptance code",
    "cited_evidence": "Verbatim evidence cited",
}


class EvidenceFieldsView(View):
    extracted_by: str
    provider: str
    provider_code: str | None
    provider_message: str | None
    transport_outcome: str
    acceptance_stage: str
    explicit_rejection: bool | None
    provider_reference: str | None
    fragments: list[str]
    completeness: str


class ExtractionView(View):
    orchestrator: Orchestrator
    ai_used: bool
    label: str
    model: str | None
    provider_model: str | None
    gateway_route: str | None
    fallback_reason: str | None
    trace_id: str | None
    rejected_drafts: int
    elapsed_seconds: float
    evidence: EvidenceFieldsView


class CheckView(View):
    name: str
    label: str
    passed: bool
    detail: str


class VerdictView(View):
    classification: str
    recorded_outcome: str
    checks: list[CheckView]
    requirements: list[CheckView]
    reasons: list[str]
    catalog_meaning: str | None
    evidence_digest_short: str


class IncidentView(View):
    incident_id: str
    rail_id: str
    attempt_id: str
    request_fully_sent: bool
    response_received: bool
    http_status: int | None
    elapsed_ms: int
    timeout_ms: int
    raw_payload: str
    digest_short: str
    extraction: ExtractionView | None
    verdict: VerdictView | None


_EXTRACTION_LABELS = {
    Orchestrator.PYDANTIC_AI: "Pydantic AI extraction",
    Orchestrator.DETERMINISTIC_FALLBACK: "Envelope-only fallback: AI extraction unavailable",
    Orchestrator.DETERMINISTIC: "Envelope-only reader: AI extraction not enabled",
}


def _checks(checks) -> list[CheckView]:
    return [
        CheckView(
            name=c.name, label=_CHECK_LABELS.get(c.name, c.name), passed=c.passed, detail=c.detail
        )
        for c in checks
    ]


def build_incident_view(
    incident: ProviderIncident | None,
    extraction: EvidenceExtraction | None,
    classification: IncidentClassification | None,
    *,
    exporting: bool,
) -> IncidentView | None:
    if incident is None:
        return None
    # Only show the extraction that produced THIS incident's recorded classification.
    if extraction is not None and extraction.evidence.incident_id != incident.incident_id:
        extraction = None
    evidence = classification.evidence if classification else None
    extraction_view = None
    if extraction is not None and evidence is not None:
        used = extraction.orchestrator is Orchestrator.PYDANTIC_AI
        extraction_view = ExtractionView(
            orchestrator=extraction.orchestrator,
            ai_used=used,
            label=_EXTRACTION_LABELS[extraction.orchestrator],
            model=extraction.model if used else None,
            provider_model=extraction.provider_model if used else None,
            gateway_route=extraction.gateway_route if used else None,
            fallback_reason=extraction.fallback_reason,
            trace_id=extraction.trace_id if exporting else None,
            rejected_drafts=extraction.rejected_drafts,
            elapsed_seconds=extraction.elapsed_seconds,
            evidence=EvidenceFieldsView(
                extracted_by=evidence.extracted_by.value,
                provider=evidence.provider,
                provider_code=evidence.provider_code,
                provider_message=evidence.provider_message,
                transport_outcome=evidence.transport_outcome.value,
                acceptance_stage=evidence.acceptance_stage.value,
                explicit_rejection=evidence.explicit_rejection,
                provider_reference=evidence.provider_reference,
                fragments=list(evidence.evidence_fragments),
                completeness=evidence.completeness.value,
            ),
        )
    verdict = classification.verdict if classification else None
    return IncidentView(
        incident_id=incident.incident_id,
        rail_id=incident.rail_id.value,
        attempt_id=incident.attempt_id,
        request_fully_sent=incident.request_fully_sent,
        response_received=incident.response_received,
        http_status=incident.http_status,
        elapsed_ms=incident.elapsed_ms,
        timeout_ms=incident.timeout_ms,
        raw_payload=incident.raw_payload,
        digest_short=incident.raw_payload_digest[:12],
        extraction=extraction_view,
        verdict=VerdictView(
            classification=verdict.classification.value,
            recorded_outcome=classification.recorded_outcome.value,
            checks=_checks(verdict.checks),
            requirements=_checks(verdict.requirements),
            reasons=list(verdict.reasons),
            catalog_meaning=verdict.catalog_meaning,
            evidence_digest_short=verdict.evidence_digest[:12],
        )
        if verdict is not None and classification is not None
        else None,
    )


# ------------------------------------------------------------------ funds position + graph


class FundsPositionView(View):
    amount: str
    last_confirmed_location: str
    position_status: str
    certainty: FundsCertainty
    available_for_automatic_action: bool
    label: str  # "Funds are here" only when proven
    reason: str | None
    derived_from: list[str]


def build_funds_position_view(frontier: SafeActionFrontier) -> FundsPositionView:
    position = frontier.funds_position
    proven = position.certainty is FundsCertainty.PROVEN
    return FundsPositionView(
        amount=money(position.amount),
        last_confirmed_location=position.last_confirmed_location.value,
        position_status=position.position_status.value,
        certainty=position.certainty,
        available_for_automatic_action=position.available_for_automatic_action,
        label="Funds are here" if proven else "Last confirmed here",
        reason=position.reason,
        derived_from=list(position.derived_from_effect_ids),
    )


_NODE_LABELS = {
    EffectNodeKind.PAYMENT_INTENT: "Payment intent",
    EffectNodeKind.SENDER_DEBIT: "Sender debit",
    EffectNodeKind.FX_CONVERSION: "GBP → GHS FX",
    EffectNodeKind.GH_SETTLEMENT: "Ghana settlement",
    EffectNodeKind.RECIPIENT_PAYOUT: "Recipient payout",
}


class EffectNodeView(View):
    node_id: str
    kind: str
    label: str
    status: str
    source: str
    destination: str
    source_amount: str | None
    destination_amount: str | None
    rail_name: str | None
    attempts: list[str]
    outstanding: str | None
    depends_on: list[str]


def build_graph_view(graph: FinancialEffectGraph, names: RailNames) -> list[EffectNodeView]:
    return [
        EffectNodeView(
            node_id=n.node_id,
            kind=n.kind.value,
            label=_NODE_LABELS[n.kind],
            status=n.status.value,
            source=n.source.value,
            destination=n.destination.value,
            source_amount=money(n.source_amount) if n.source_amount else None,
            destination_amount=money(n.destination_amount) if n.destination_amount else None,
            rail_name=_name(names, n.rail_id),
            attempts=[
                f"{_name(names, a.rail_id)}: {a.outcome.value}"
                + (f" ({a.failure_summary})" if a.failure_summary else "")
                for a in n.attempts
            ],
            outstanding=money(n.outstanding) if n.outstanding else None,
            depends_on=list(n.depends_on),
        )
        for n in graph.nodes
    ]


# ------------------------------------------------------------------ safe action frontier


class FrontierActionView(View):
    action_id: str
    kind: str
    label: str
    rail_name: str | None
    moves_value: bool
    eligible: bool | None
    rejection_details: list[str]
    simulated: bool
    simulated_reliability: str | None
    score: str | None
    rank: int | None
    selected: bool
    implemented: bool
    note: str | None


class FrontierView(View):
    basis: str
    payout_actions_permitted: bool
    candidates: int
    rejected_before_simulation: int
    eligible: int
    simulated: int
    selected: int
    actions: list[FrontierActionView]
    reason: str
    plan_id: str | None


def build_frontier_view(frontier: SafeActionFrontier, names: RailNames) -> FrontierView:
    counts = frontier.counts
    return FrontierView(
        basis=frontier.basis,
        payout_actions_permitted=frontier.payout_actions_permitted,
        candidates=counts.candidates,
        rejected_before_simulation=counts.rejected_before_simulation,
        eligible=counts.eligible,
        simulated=counts.simulated,
        selected=counts.selected,
        actions=[
            FrontierActionView(
                action_id=a.action_id,
                kind=a.kind.value,
                label=a.label,
                rail_name=_name(names, a.rail_id),
                moves_value=a.moves_value,
                eligible=None
                if a.hard_constraint_status is None
                else a.hard_constraint_status.value == "PASSED",
                rejection_details=list(a.rejection_details),
                simulated=a.simulated,
                simulated_reliability=pct(a.simulated_reliability),
                score=f"{a.score:.3f}" if a.score is not None else None,
                rank=a.rank,
                selected=a.selected,
                implemented=a.implemented,
                note=a.note,
            )
            for a in frontier.actions
        ],
        reason=frontier.reason,
        plan_id=frontier.plan_id,
    )


# ------------------------------------------------------------------ ledger preview


class LedgerRowView(View):
    label: str
    current: str
    proposed: str
    changed: bool


class PreviewView(View):
    plan_id: str
    valid: bool
    invalidation_reasons: list[str]
    rows: list[LedgerRowView]
    proposed_effect_key: str | None
    proposed_rail: str | None


def build_preview_view(preview: LedgerPreview, names: RailNames) -> PreviewView:
    return PreviewView(
        plan_id=preview.plan_id,
        valid=preview.valid,
        invalidation_reasons=list(preview.invalidation_reasons),
        rows=[
            LedgerRowView(label=c.label, current=c.current, proposed=c.proposed, changed=c.changed)
            for c in preview.changes
        ],
        proposed_effect_key=preview.proposed_effect_key,
        proposed_rail=_name(names, preview.proposed_rail),
    )


# ------------------------------------------------------------------ naive retry


_STEP_LABELS = {
    "SENDER_DEBIT": "Debit the sender",
    "FX_CONVERSION": "Convert GBP → GHS",
    "GH_SETTLEMENT": "Settle into Ghana",
    "RECIPIENT_CREDIT": "Pay the recipient",
}


class CounterfactualStepView(View):
    label: str
    rail_name: str | None
    amount: str
    already_completed: bool
    risk: str | None


class CounterfactualView(View):
    naive_steps: list[CounterfactualStepView]
    naive_repeated_effects: int
    naive_extra_sender_debit: str | None
    duplicate_recipient_credit_risk: bool
    sikarescue_steps: list[CounterfactualStepView]
    sikarescue_action: str


def build_counterfactual_view(result: RetryCounterfactual, names: RailNames) -> CounterfactualView:
    def steps(items) -> list[CounterfactualStepView]:
        return [
            CounterfactualStepView(
                label=_STEP_LABELS[s.operation.value],
                rail_name=_name(names, s.rail_id),
                amount=money(s.amount),
                already_completed=s.already_completed,
                risk=s.risk,
            )
            for s in items
        ]

    return CounterfactualView(
        naive_steps=steps(result.naive_steps),
        naive_repeated_effects=result.naive_repeated_effects,
        naive_extra_sender_debit=money(result.naive_extra_sender_debit)
        if result.naive_extra_sender_debit
        else None,
        duplicate_recipient_credit_risk=result.duplicate_recipient_credit_risk,
        sikarescue_steps=steps(result.sikarescue_steps),
        sikarescue_action=result.sikarescue_action,
    )


# ------------------------------------------------------------------ systemic outage

_UNSERVED_LABELS = {
    UnservedReason.POLICY_LIMIT: "Over the corridor payout limit",
    UnservedReason.NO_ELIGIBLE_RAIL: "No available, compatible rail",
    UnservedReason.LIQUIDITY_EXHAUSTED: "Fallback liquidity exhausted",
    UnservedReason.CAPACITY_EXHAUSTED: "Fallback capacity exhausted",
}


class OutageRailView(View):
    rail_id: str
    rail_name: str
    status: str
    eligible: bool
    rejection_reasons: list[str]
    policy_detail: str
    liquidity: str
    capacity: int
    fee: str


class OutageAllocationView(View):
    rail_id: str
    rail_name: str
    obligations: int
    amount: str
    liquidity: str
    liquidity_utilisation: float
    liquidity_utilisation_text: str
    capacity: int
    capacity_utilisation: float
    capacity_utilisation_text: str
    incremental_fee: str


class OutageUnservedView(View):
    reason: str
    label: str
    obligations: int
    amount: str


class OutageScenarioView(View):
    scenario_id: str
    label: str
    description: str
    recoverable_obligations: int
    recoverable_share: str
    recoverable_amount: str
    unserved_obligations: int
    unserved_amount: str
    unserved: list[OutageUnservedView]
    allocations: list[OutageAllocationView]
    rails: list[OutageRailView]
    aggregate_incremental_fee: str
    compute_seconds: float
    checks: list[str]


class OutagePortfolioView(View):
    size: int
    seed: int
    total_amount: str
    mobile_money: int
    bank_account: int
    over_policy_limit: int
    oldest_minutes: int
    digest_short: str


class OutageView(View):
    failed_rail: str
    corridor: str
    portfolio: OutagePortfolioView
    scenarios: list[OutageScenarioView]
    backend: str
    configured_backend: str
    fallback_from: str | None
    fallback_reason: str | None
    parallel_jobs: int
    wall_seconds: float
    compute_wall_seconds: float
    kernel_seconds: float
    verification_seconds: float
    function_ref: str | None
    generated_at: datetime


def build_outage_view(analysis: OutageAnalysis, names: RailNames) -> OutageView:
    p = analysis.portfolio
    return OutageView(
        failed_rail=analysis.failed_rail.value,
        corridor=analysis.corridor.replace("->", " → "),
        portfolio=OutagePortfolioView(
            size=p.size,
            seed=p.seed,
            total_amount=money(p.total_amount),
            mobile_money=p.mobile_money,
            bank_account=p.bank_account,
            over_policy_limit=p.over_policy_limit,
            oldest_minutes=p.oldest_minutes,
            digest_short=p.digest[:12],
        ),
        scenarios=[
            OutageScenarioView(
                scenario_id=s.scenario.scenario_id,
                label=s.scenario.label,
                description=s.scenario.description,
                recoverable_obligations=s.recoverable_obligations,
                recoverable_share=pct(s.recoverable_obligations / s.affected_obligations) or "",
                recoverable_amount=money(s.recoverable_amount),
                unserved_obligations=s.unserved_obligations,
                unserved_amount=money(s.unserved_amount),
                unserved=[
                    OutageUnservedView(
                        reason=b.reason.value,
                        label=_UNSERVED_LABELS[b.reason],
                        obligations=b.obligations,
                        amount=money(b.amount),
                    )
                    for b in s.unserved
                ],
                allocations=[
                    OutageAllocationView(
                        rail_id=a.rail_id.value,
                        rail_name=_name(names, a.rail_id) or a.rail_id.value,
                        obligations=a.obligations,
                        amount=money(a.amount),
                        liquidity=money(a.liquidity),
                        liquidity_utilisation=a.liquidity_utilisation,
                        liquidity_utilisation_text=pct(a.liquidity_utilisation) or "",
                        capacity=a.capacity,
                        capacity_utilisation=a.capacity_utilisation,
                        capacity_utilisation_text=pct(a.capacity_utilisation) or "",
                        incremental_fee=money(a.incremental_fee),
                    )
                    for a in s.allocations
                ],
                rails=[
                    OutageRailView(
                        rail_id=r.rail_id.value,
                        rail_name=_name(names, r.rail_id) or r.rail_id.value,
                        status=r.status.value,
                        eligible=r.eligible,
                        rejection_reasons=[x.value for x in r.rejection_reasons],
                        policy_detail=r.policy_detail,
                        liquidity=money(r.liquidity),
                        capacity=r.capacity,
                        fee=money(r.fee),
                    )
                    for r in s.rails
                ],
                aggregate_incremental_fee=money(s.aggregate_incremental_fee),
                compute_seconds=s.compute_seconds,
                checks=list(s.checks),
            )
            for s in analysis.scenarios
        ],
        backend=analysis.backend,
        configured_backend=analysis.configured_backend,
        fallback_from=analysis.fallback_from,
        fallback_reason=analysis.fallback_reason,
        parallel_jobs=analysis.parallel_jobs,
        wall_seconds=analysis.wall_seconds,
        compute_wall_seconds=analysis.compute_wall_seconds,
        kernel_seconds=analysis.kernel_seconds,
        verification_seconds=analysis.verification_seconds,
        function_ref=analysis.function_ref,
        generated_at=analysis.generated_at,
    )
