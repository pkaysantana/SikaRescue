"""Pure derivations over authoritative state: effect graph, ledger preview, safe action
frontier and the naive-retry counterfactual.

Nothing here writes to the aggregate, the journal or any service. Inputs are read, a result
is returned. The ledger preview posts into a `journal.fork()`, never the real journal.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from sikarescue.compute.constraints import hard_constraint_violations
from sikarescue.errors import SikaRescueError
from sikarescue.models import (
    AttemptEvidence,
    AttemptOutcome,
    CandidateRecoveryRoute,
    CounterfactualStep,
    Currency,
    EffectNode,
    EffectNodeKind,
    EffectNodeStatus,
    FinancialEffectGraph,
    FrontierAction,
    FrontierActionKind,
    FrontierCounts,
    FundsLocation,
    HardConstraintStatus,
    LedgerChange,
    LedgerPreview,
    LedgerSnapshot,
    LocationHolding,
    Money,
    OperationType,
    PositionStatus,
    RecoveryPlan,
    RetryCounterfactual,
    SafeActionFrontier,
    effect_key,
    payout_execution_key,
    utcnow,
)
from sikarescue.models.enums import OPERATION_FLOW
from sikarescue.services.diagnosis import (
    derive_funds_position,
    outstanding_obligation,
    summarise_failure,
    unresolved_unknown_attempts,
)
from sikarescue.services.effects import payout_attempt, recipient_credit_effect
from sikarescue.services.journal import FinancialJournal
from sikarescue.services.repository import TransactionAggregate

CHAIN = (
    OperationType.SENDER_DEBIT,
    OperationType.FX_CONVERSION,
    OperationType.GH_SETTLEMENT,
    OperationType.RECIPIENT_CREDIT,
)
_KIND = {
    OperationType.SENDER_DEBIT: EffectNodeKind.SENDER_DEBIT,
    OperationType.FX_CONVERSION: EffectNodeKind.FX_CONVERSION,
    OperationType.GH_SETTLEMENT: EffectNodeKind.GH_SETTLEMENT,
    OperationType.RECIPIENT_CREDIT: EffectNodeKind.RECIPIENT_PAYOUT,
}
LOCATIONS = (
    FundsLocation.SENDER_ACCOUNT,
    FundsLocation.UK_COLLECTION_ACCOUNT,
    FundsLocation.GHS_FX_POOL,
    FundsLocation.GH_SETTLEMENT_ACCOUNT,
    FundsLocation.RECIPIENT_ENDPOINT,
)


def _money(m: Money) -> str:
    symbol = "£" if m.currency is Currency.GBP else f"{m.currency.value} "
    return f"{symbol}{m.amount:,.2f}"


# ============================================================ financial effect graph


def build_effect_graph(aggregate: TransactionAggregate) -> FinancialEffectGraph:
    """Intent -> SenderDebit -> FX -> GhanaSettlement -> RecipientPayout, from the journal."""
    journal = aggregate.journal
    instruction = aggregate.instruction
    txn = aggregate.transaction_id
    original = set(instruction.original_route)
    pending = {r.operation for r in journal.pending_provider_responses()}
    executing = aggregate.has_execution_in_progress()
    unknown = set(unresolved_unknown_attempts(aggregate))
    obligation = outstanding_obligation(aggregate)
    intent_id = f"{txn}:intent"
    nodes = [
        EffectNode(
            node_id=intent_id,
            kind=EffectNodeKind.PAYMENT_INTENT,
            operation=None,
            status=EffectNodeStatus.FULFILLED
            if journal.has_effect(OperationType.RECIPIENT_CREDIT)
            else EffectNodeStatus.OPEN,
            source=FundsLocation.SENDER_ACCOUNT,
            destination=FundsLocation.RECIPIENT_ENDPOINT,
            source_amount=instruction.send_amount,
            destination_amount=instruction.payout_amount,
            fx_rate=instruction.fx_rate,
        )
    ]
    previous = intent_id
    for op in CHAIN:
        source, destination = OPERATION_FLOW[op]
        effect = journal.effect(op)
        attempts = [a for a in journal.attempts() if a.operation is op]
        if effect is not None:
            status = EffectNodeStatus.COMPLETE
        elif op in pending:
            status = EffectNodeStatus.AWAITING_EVIDENCE
        elif op is OperationType.RECIPIENT_CREDIT and executing:
            status = EffectNodeStatus.IN_FLIGHT
        elif any(a.attempt_id in unknown for a in attempts):
            status = EffectNodeStatus.UNKNOWN
        elif attempts:
            status = EffectNodeStatus.FAILED
        else:
            status = EffectNodeStatus.PENDING
        node_id = effect_key(txn, op)
        nodes.append(
            EffectNode(
                node_id=node_id,
                kind=_KIND[op],
                operation=op,
                status=status,
                source=source,
                destination=destination,
                source_amount=effect.source_amount if effect else None,
                destination_amount=effect.destination_amount if effect else None,
                fx_rate=effect.fx_rate if effect else None,
                rail_id=effect.rail_id if effect else None,
                posted_attempt_id=effect.attempt_id if effect else None,
                attempts=tuple(
                    AttemptEvidence(
                        attempt_id=a.attempt_id,
                        rail_id=a.rail_id,
                        outcome=a.outcome,
                        is_recovery=a.plan_id is not None or a.rail_id not in original,
                        plan_id=a.plan_id,
                        provider_reference=a.provider_reference,
                        failure_summary=summarise_failure(a.failure),
                    )
                    for a in attempts
                ),
                outstanding=obligation.amount
                if obligation is not None and op is OperationType.RECIPIENT_CREDIT
                else None,
                depends_on=(previous,),
            )
        )
        previous = node_id
    return FinancialEffectGraph(transaction_id=txn, revision=aggregate.revision, nodes=tuple(nodes))


# ============================================================ ledger snapshot + preview


def ledger_snapshot(aggregate: TransactionAggregate, journal: FinancialJournal) -> LedgerSnapshot:
    """What `journal` says this payment holds where. Works on a fork as well as the real one."""
    instruction = aggregate.instruction
    currency = {
        FundsLocation.SENDER_ACCOUNT: instruction.send_amount.currency,
        FundsLocation.UK_COLLECTION_ACCOUNT: instruction.send_amount.currency,
        FundsLocation.GHS_FX_POOL: instruction.payout_amount.currency,
        FundsLocation.GH_SETTLEMENT_ACCOUNT: instruction.payout_amount.currency,
        FundsLocation.RECIPIENT_ENDPOINT: instruction.payout_amount.currency,
    }
    held: dict[FundsLocation, Decimal] = dict.fromkeys(LOCATIONS, Decimal("0.00"))
    held[FundsLocation.SENDER_ACCOUNT] = instruction.send_amount.amount
    for effect in journal.effects():
        held[effect.source] -= effect.source_amount.amount
        held[effect.destination] += effect.destination_amount.amount
    debits = journal.count_effects(OperationType.SENDER_DEBIT)
    credited = journal.has_effect(OperationType.RECIPIENT_CREDIT)
    settlement = journal.effect(OperationType.GH_SETTLEMENT)
    return LedgerSnapshot(
        revision=journal.revision,
        sender_debit_count=debits,
        fx_count=journal.count_effects(OperationType.FX_CONVERSION),
        settlement_count=journal.count_effects(OperationType.GH_SETTLEMENT),
        recipient_credit_count=journal.count_effects(OperationType.RECIPIENT_CREDIT),
        duplicate_sender_debits=max(0, debits - 1),
        holdings=tuple(
            LocationHolding(location=loc, amount=Money(amount=held[loc], currency=currency[loc]))
            for loc in LOCATIONS
        ),
        funds_location=journal.funds_location(),
        outstanding=settlement.destination_amount if settlement and not credited else None,
    )


def _holding(snapshot: LedgerSnapshot, location: FundsLocation) -> str:
    return _money(next(h.amount for h in snapshot.holdings if h.location is location))


def _changes(current: LedgerSnapshot, proposed: LedgerSnapshot) -> tuple[LedgerChange, ...]:
    rows = (
        ("Sender debits", str(current.sender_debit_count), str(proposed.sender_debit_count)),
        ("FX conversions", str(current.fx_count), str(proposed.fx_count)),
        ("Ghana settlements", str(current.settlement_count), str(proposed.settlement_count)),
        (
            "Held at GH_SETTLEMENT_ACCOUNT",
            _holding(current, FundsLocation.GH_SETTLEMENT_ACCOUNT),
            _holding(proposed, FundsLocation.GH_SETTLEMENT_ACCOUNT),
        ),
        (
            "Held at RECIPIENT_ENDPOINT",
            _holding(current, FundsLocation.RECIPIENT_ENDPOINT),
            _holding(proposed, FundsLocation.RECIPIENT_ENDPOINT),
        ),
        (
            "Outstanding obligation",
            _money(current.outstanding) if current.outstanding else "none",
            _money(proposed.outstanding) if proposed.outstanding else "none",
        ),
        (
            "Recipient credits",
            str(current.recipient_credit_count),
            str(proposed.recipient_credit_count),
        ),
        (
            "Duplicate sender debits",
            str(current.duplicate_sender_debits),
            str(proposed.duplicate_sender_debits),
        ),
    )
    return tuple(
        LedgerChange(label=label, current=before, proposed=after, changed=before != after)
        for label, before, after in rows
    )


def build_ledger_preview(
    aggregate: TransactionAggregate, plan: RecoveryPlan, blockers: Iterable[str]
) -> LedgerPreview:
    """Project `plan`'s execution onto a FORK of the journal. The real journal is untouched."""
    current = ledger_snapshot(aggregate, aggregate.journal)
    base = {
        "transaction_id": aggregate.transaction_id,
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "bound_revision": plan.expected_revision,
        "current": current,
    }
    reasons = list(blockers)
    if not reasons:
        fork = aggregate.journal.fork()
        at = utcnow()
        try:
            attempt = payout_attempt(
                plan,
                attempt_id="att_000000000000",  # placeholder id: the fork is discarded
                execution_key=payout_execution_key(plan.transaction_id, plan.plan_id),
                outcome=AttemptOutcome.SUCCEEDED,
                provider_reference=None,
                failure=None,
                started_at=at,
                completed_at=at,
            )
            fork.record_attempt(attempt)
            effect = recipient_credit_effect(plan, attempt_id=attempt.attempt_id, posted_at=at)
            fork.post_effect(effect)  # the same journal invariants execution would face
        except (SikaRescueError, ValueError) as exc:
            reasons.append(f"the projected effect would be refused by the journal: {exc}"[:200])
        else:
            proposed = ledger_snapshot(aggregate, fork)
            return LedgerPreview(
                **base,
                valid=True,
                proposed=proposed,
                proposed_effect_key=effect.effect_key,
                proposed_rail=plan.rail_id,
                changes=_changes(current, proposed),
            )
    return LedgerPreview(**base, valid=False, invalidation_reasons=tuple(reasons))


# ============================================================ safe action frontier


_WAIT = FrontierAction(
    action_id="wait_for_provider_evidence",
    kind=FrontierActionKind.WAIT_FOR_PROVIDER_EVIDENCE,
    label="Wait for further provider evidence (status update, statement)",
)
_QUERY = FrontierAction(
    action_id="query_provider",
    kind=FrontierActionKind.QUERY_PROVIDER,
    label="Query the provider for the payout's final status",
    implemented=False,
    note="Demo abstraction: no provider status query is implemented.",
)
_REVIEW = FrontierAction(
    action_id="manual_review",
    kind=FrontierActionKind.MANUAL_REVIEW,
    label="Operator confirms the outcome with the provider before anything else moves",
)
_CLASSIFY = FrontierAction(
    action_id="classify_provider_evidence",
    kind=FrontierActionKind.CLASSIFY_PROVIDER_EVIDENCE,
    label="Classify the provider's response (extract evidence, verify deterministically)",
)
_NOTHING = FrontierAction(
    action_id="no_action_required",
    kind=FrontierActionKind.NO_ACTION_REQUIRED,
    label="Recipient credited: nothing left to move",
)
_EMPTY = FrontierCounts(
    candidates=0, rejected_before_simulation=0, eligible=0, simulated=0, selected=0
)


def _counts(actions: tuple[FrontierAction, ...]) -> FrontierCounts:
    payouts = [a for a in actions if a.kind is FrontierActionKind.PAYOUT]
    rejected = [a for a in payouts if a.hard_constraint_status is HardConstraintStatus.REJECTED]
    return FrontierCounts(
        candidates=len(payouts),
        rejected_before_simulation=len(rejected),
        eligible=len(payouts) - len(rejected),
        simulated=sum(a.simulated for a in payouts),
        selected=sum(a.selected for a in payouts),
    )


def _plan_actions(plan: RecoveryPlan) -> tuple[FrontierAction, ...]:
    return tuple(
        FrontierAction(
            action_id=f"payout_{e.rail_id.value.lower()}",
            kind=FrontierActionKind.PAYOUT,
            label=f"Candidate route via {e.rail_id}",
            rail_id=e.rail_id,
            moves_value=True,
            hard_constraint_status=e.hard_constraint_status,
            rejection_reasons=e.rejection_reasons,
            rejection_details=e.rejection_details,
            simulated=bool(e.scenario_results),
            simulated_reliability=e.simulated_success_probability,
            score=e.score.total if e.score else None,
            rank=e.rank,
            selected=e.route_id == plan.route_id,
        )
        for e in plan.evaluations
    )


def _candidate_actions(
    candidates: tuple[CandidateRecoveryRoute, ...],
) -> tuple[FrontierAction, ...]:
    actions = []
    for c in candidates:
        violations = hard_constraint_violations(c)
        actions.append(
            FrontierAction(
                action_id=f"payout_{c.rail.rail_id.value.lower()}",
                kind=FrontierActionKind.PAYOUT,
                label=f"Candidate route via {c.rail.rail_id}",
                rail_id=c.rail.rail_id,
                moves_value=True,
                hard_constraint_status=HardConstraintStatus.REJECTED
                if violations
                else HardConstraintStatus.PASSED,
                rejection_reasons=tuple(r for r, _ in violations),
                rejection_details=tuple(d for _, d in violations),
            )
        )
    return tuple(actions)


def build_safe_action_frontier(
    aggregate: TransactionAggregate,
    *,
    plan: RecoveryPlan | None,
    candidates: tuple[CandidateRecoveryRoute, ...] | None,
) -> SafeActionFrontier:
    """What may be done NOW. `plan` must be the current, fresh plan (or None)."""
    position = derive_funds_position(aggregate)
    base = {
        "transaction_id": aggregate.transaction_id,
        "revision": aggregate.revision,
        "funds_position": position,
    }
    status = position.position_status
    if status is PositionStatus.FINAL:
        return SafeActionFrontier(
            **base,
            basis="SETTLED",
            payout_actions_permitted=False,
            actions=(_NOTHING,),
            counts=_EMPTY,
            reason="The recipient credit is posted: there is nothing left to move.",
        )
    if aggregate.journal.pending_provider_responses():
        return SafeActionFrontier(
            **base,
            basis="PENDING_EVIDENCE",
            payout_actions_permitted=False,
            actions=(_CLASSIFY, _WAIT, _REVIEW),
            counts=_EMPTY,
            reason="The original payout's response is not classified yet, so no payout "
            "action exists: the recipient may already have been credited.",
        )
    if not position.available_for_automatic_action:
        return SafeActionFrontier(
            **base,
            basis="UNCERTAIN_FUNDS",
            payout_actions_permitted=False,
            actions=(_QUERY, _WAIT, _REVIEW),
            counts=_EMPTY,
            reason=f"No payout action exists: {position.reason or 'funds are not available'}.",
        )
    if plan is not None:
        actions = _plan_actions(plan)
        return SafeActionFrontier(
            **base,
            basis="PLAN",
            payout_actions_permitted=True,
            actions=actions,
            counts=_counts(actions),
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            reason="Candidates evaluated against hard constraints first; only eligible ones "
            "were simulated and ranked. Rejected candidates are not actions. The selected plan is "
            "the only executable artifact, and only after approval.",
        )
    actions = _candidate_actions(candidates or ())
    return SafeActionFrontier(
        **base,
        basis="CANDIDATES",
        payout_actions_permitted=True,
        actions=actions,
        counts=_counts(actions),
        reason="Candidate routes evaluated against hard constraints. Eligible candidates are "
        "simulated and ranked only when analysed; none is executable without an approved plan.",
    )


# ============================================================ naive retry counterfactual


def compare_naive_retry(
    aggregate: TransactionAggregate, plan: RecoveryPlan | None
) -> RetryCounterfactual:
    """Dry run of 'just restart the payment' vs SikaRescue. Reads only; executes nothing."""
    journal = aggregate.journal
    instruction = aggregate.instruction
    position = derive_funds_position(aggregate)
    payout_uncertain = bool(unresolved_unknown_attempts(aggregate)) or bool(
        journal.pending_provider_responses()
    )
    amounts = {
        OperationType.SENDER_DEBIT: instruction.send_amount,
        OperationType.FX_CONVERSION: instruction.send_amount,
        OperationType.GH_SETTLEMENT: instruction.payout_amount,
        OperationType.RECIPIENT_CREDIT: instruction.payout_amount,
    }
    risks = {
        OperationType.SENDER_DEBIT: (
            f"charges the sender again (+{_money(instruction.send_amount)})"
        ),
        OperationType.FX_CONVERSION: "converts the same value a second time",
        OperationType.GH_SETTLEMENT: "settles the same value a second time",
    }
    naive: list[CounterfactualStep] = []
    for op, rail in zip(CHAIN, instruction.original_route, strict=True):
        source, destination = OPERATION_FLOW[op]
        done = journal.has_effect(op)
        risk = risks.get(op) if done else None
        if op is OperationType.RECIPIENT_CREDIT:
            if done:
                risk = "credits the recipient a second time"
            elif payout_uncertain:
                risk = "may credit the recipient twice: the earlier payout outcome is unknown"
            elif any(a.rail_id is rail for a in journal.attempts() if a.operation is op):
                risk = f"retries {rail}, which already failed this payout"
        naive.append(
            CounterfactualStep(
                operation=op,
                rail_id=rail,
                source=source,
                destination=destination,
                amount=amounts[op],
                already_completed=done,
                risk=risk,
            )
        )
    obligation = outstanding_obligation(aggregate)
    ours: tuple[CounterfactualStep, ...] = ()
    if position.position_status is PositionStatus.FINAL:
        action = "Nothing to move: the recipient credit is already posted."
    elif not position.available_for_automatic_action or obligation is None:
        action = "Move nothing. Manual review confirms the earlier payout with the provider first."
    else:
        rail = plan.rail_id if plan is not None else None
        ours = (
            CounterfactualStep(
                operation=OperationType.RECIPIENT_CREDIT,
                rail_id=rail,
                source=obligation.source,
                destination=FundsLocation.RECIPIENT_ENDPOINT,
                amount=obligation.amount,
                already_completed=False,
            ),
        )
        via = f" via {rail}" if rail else ""
        action = (
            f"Pay only the outstanding {_money(obligation.amount)} from {obligation.source}{via}."
        )
    debit_done = journal.has_effect(OperationType.SENDER_DEBIT)
    return RetryCounterfactual(
        transaction_id=aggregate.transaction_id,
        revision=aggregate.revision,
        naive_steps=tuple(naive),
        naive_repeated_effects=sum(s.already_completed for s in naive),
        naive_extra_sender_debit=instruction.send_amount if debit_done else None,
        duplicate_recipient_credit_risk=payout_uncertain
        or journal.has_effect(OperationType.RECIPIENT_CREDIT),
        sikarescue_steps=ours,
        sikarescue_repeated_effects=sum(s.already_completed for s in ours),
        sikarescue_action=action,
        position_status=position.position_status,
    )
