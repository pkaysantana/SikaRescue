import type { Ref } from "react";

import { accountName, clock, grouped, stateName } from "../format";
import type { Action, AdviceCard as Advice, DemoView, NextAction, RouteOption } from "../types";
import { LedgerDiff } from "./ControlPlane";

// ------------------------------------------------------------------ stepper

const STEPS: { key: NextAction; label: string }[] = [
  { key: "classify", label: "Classify" },
  { key: "analyse", label: "Analyse" },
  { key: "approve", label: "Approve" },
  { key: "execute", label: "Execute" },
  { key: "done", label: "Reconciled" },
];

export function Stepper({ next }: { next: NextAction }) {
  const current = STEPS.findIndex((s) => s.key === next);
  return (
    <ol className="stepper" aria-label="Recovery steps">
      {STEPS.map((step, i) => {
        const state =
          next === "done" || i < current ? "done" : i === current ? "current" : "upcoming";
        return (
          <li key={step.key} className={`step step--${state}`} aria-current={state === "current"}>
            <span className="step-index">{state === "done" ? "✓" : i + 1}</span>
            <span>{step.label}</span>
          </li>
        );
      })}
    </ol>
  );
}

// ------------------------------------------------------------------ analyse

interface AnalyseProps {
  view: DemoView;
  pending: Action | null;
  onAnalyse: () => void;
}

export function AnalyseStep({ view, pending, onAnalyse }: AnalyseProps) {
  const running = pending === "analyse";
  const onModal = view.configured_compute_backend === "modal";
  return (
    <section className="step-section" aria-labelledby="analyse-title">
      <h2 id="analyse-title">Find a safe recovery route</h2>
      <p className="lead">
        SikaRescue works out where the funds are, removes routes that break hard constraints, and
        stress-tests the rest before proposing a single plan.
      </p>
      {view.next_action === "analyse" && (
        <button className="primary" onClick={onAnalyse} disabled={pending !== null}>
          {running ? <Spinner label="Analysing recovery" /> : "Analyse recovery"}
        </button>
      )}
      {running && (
        <p className="progress-note" role="status">
          {view.agent_mode === "pydantic"
            ? "The Pydantic AI agent is investigating and requesting the deterministic analysis. "
            : "Evaluating routes. "}
          {onModal
            ? "Eligible routes are stress-tested on Modal."
            : "Eligible routes are stress-tested locally."}
          {view.agent_mode === "pydantic" && " This usually takes 20 to 40 seconds."}
        </p>
      )}
    </section>
  );
}

// ------------------------------------------------------------------ routes

function RouteRow({ route }: { route: RouteOption }) {
  if (!route.eligible) {
    return (
      <li className="route route--rejected">
        <div className="route-head">
          <span className="route-name">{route.rail_name}</span>
          <span className="tag tag--rejected">Rejected</span>
        </div>
        <ul className="route-reasons">
          {route.rejection_details.map((detail) => (
            <li key={detail}>{detail}</li>
          ))}
        </ul>
        <p className="route-foot">Removed before simulation: no trials were run on this route.</p>
      </li>
    );
  }
  return (
    <li className={`route route--eligible${route.selected ? " route--selected" : ""}`}>
      <div className="route-head">
        <span className="route-name">{route.rail_name}</span>
        <span className={`tag ${route.selected ? "tag--selected" : "tag--eligible"}`}>
          {route.selected ? "Selected plan" : "Eligible"}
        </span>
        <span className="route-rank">Rank {route.rank}</span>
      </div>
      <dl className="metrics">
        <div>
          <dt>Incremental fee</dt>
          <dd>{route.fee}</dd>
        </div>
        <div>
          <dt>Simulated success</dt>
          <dd>{route.simulated_success}</dd>
        </div>
        <div>
          <dt>Credited within SLA</dt>
          <dd>{route.within_sla}</dd>
        </div>
        <div>
          <dt>p95 arrival</dt>
          <dd>{route.p95_arrival_seconds} s</dd>
        </div>
        <div>
          <dt>Score</dt>
          <dd>{route.score}</dd>
        </div>
      </dl>
      {route.stress.length > 0 && (
        <p className="route-stress">
          Under stress:{" "}
          {route.stress.map((s, i) => (
            <span key={s.scenario}>
              {i > 0 && ", "}
              {s.scenario.toLowerCase()} {s.success}
            </span>
          ))}
        </p>
      )}
    </li>
  );
}

export function RouteList({ view, sectionRef }: { view: DemoView; sectionRef: Ref<HTMLElement> }) {
  const analysis = view.analysis;
  if (!analysis) return null;
  return (
    <section className="step-section" ref={sectionRef} aria-labelledby="routes-title">
      <h2 id="routes-title">Candidate routes</h2>
      <p className="lead">
        Hard constraints run first. Only routes that pass policy, rail status, recipient
        compatibility and liquidity are simulated and scored.
      </p>
      <ul className="routes">
        {analysis.routes.map((route) => (
          <RouteRow key={route.rail_id} route={route} />
        ))}
      </ul>
    </section>
  );
}

// ------------------------------------------------------------------ compute

export function ComputePanel({ view }: { view: DemoView }) {
  const c = view.analysis?.compute;
  if (!c) return null;
  const onModal = c.backend === "modal";
  return (
    <section className="step-section compute" aria-labelledby="compute-title">
      <h2 id="compute-title">Stress simulation</h2>
      <p className="compute-where">
        {onModal && "Ran on Modal"}
        {!onModal && c.fallback_from && `${c.fallback_from} unavailable: ran locally instead`}
        {!onModal && !c.fallback_from && "Ran locally, in-process"}
      </p>
      {c.fallback_reason && <p className="warning-note">Reason: {c.fallback_reason}</p>}
      <p className="compute-total">
        <strong>{grouped(c.simulated_outcomes)}</strong> synthetic recovery outcomes
      </p>
      <p className="muted">
        {c.routes_simulated} eligible routes × {c.scenarios.length} operating scenarios ×{" "}
        {grouped(c.trials_per_scenario)} trials. Rejected routes simulated:{" "}
        {c.rejected_routes_simulated}.
      </p>
      <ul className="chips" aria-label="Scenarios">
        {c.scenarios.map((s) => (
          <li key={s}>{s}</li>
        ))}
      </ul>
      <dl className="metrics metrics--compact">
        <div>
          <dt>Wall time</dt>
          <dd>{c.elapsed_seconds.toFixed(2)} s</dd>
        </div>
        {c.parallel_jobs > 0 && (
          <div>
            <dt>Parallel jobs</dt>
            <dd>{c.parallel_jobs}</dd>
          </div>
        )}
        {c.remote_compute_seconds !== null && (
          <div>
            <dt>Remote CPU time</dt>
            <dd>{c.remote_compute_seconds.toFixed(2)} s</dd>
          </div>
        )}
      </dl>
    </section>
  );
}

// ------------------------------------------------------------------ advice

function MetaRow({ label, value }: { label: string; value: string | null }) {
  if (!value) return null;
  return (
    <div>
      <dt>{label}</dt>
      <dd className="mono">{value}</dd>
    </div>
  );
}

export function AdviceCard({ advice }: { advice: Advice }) {
  return (
    <section className="step-section advice" aria-labelledby="advice-title">
      <div className="advice-head">
        <h2 id="advice-title">AI recovery explanation</h2>
        <span className={`source ${advice.ai_used ? "source--ai" : "source--fallback"}`}>
          {advice.label}
        </span>
      </div>
      {advice.fallback_reason && (
        <p className="warning-note">Why: {advice.fallback_reason}</p>
      )}
      <dl className="advice-body">
        <div>
          <dt>What happened</dt>
          <dd>{advice.incident_summary}</dd>
        </div>
        <div>
          <dt>Why not restart the payment</dt>
          <dd>{advice.why_origin_retry_is_unsafe}</dd>
        </div>
        <div>
          <dt>How the route held up under stress</dt>
          <dd>{advice.stress_summary}</dd>
        </div>
        <div>
          <dt>For the approver</dt>
          <dd>{advice.operator_message}</dd>
        </div>
      </dl>
      <dl className="advice-meta">
        <MetaRow label="Orchestrator" value={advice.orchestrator} />
        <MetaRow label="Model reported by provider" value={advice.provider_model} />
        <MetaRow label="Requested model" value={advice.model} />
        <MetaRow label="Gateway route" value={advice.gateway_route} />
        <MetaRow label="Trace ID" value={advice.trace_id} />
        <MetaRow label="Agent tools" value={advice.tool_calls.join(", ") || null} />
      </dl>
      <p className="note">
        This explanation can't change the plan. Its structured fields (route, fee, arrival,
        reliability, reason codes and rejected routes) are checked against the deterministic plan;
        the narrative wording is not fact-checked. The plan below is what executes.
      </p>
    </section>
  );
}

// ------------------------------------------------------------------ plan + approval

interface PlanProps {
  view: DemoView;
  pending: Action | null;
  onApprove: () => void;
  sectionRef: Ref<HTMLElement>;
}

export function PlanApproval({ view, pending, onApprove, sectionRef }: PlanProps) {
  const plan = view.analysis?.plan;
  if (!plan) return null;
  const approval = view.approval;
  return (
    <section className="step-section" ref={sectionRef} aria-labelledby="plan-title">
      <h2 id="plan-title">Recommended recovery plan</h2>
      <p className="lead">
        Selected by SikaRescue's deterministic planner, not by the AI. Approving it authorises
        exactly this plan and nothing else.
      </p>
      <div className="plan-doc">
        <p className="plan-route">
          <span className="mono">{plan.source}</span> → {plan.rail_name} → recipient wallet
        </p>
        <dl className="plan-grid">
          <div>
            <dt>Source</dt>
            <dd className="mono">{plan.source}</dd>
          </div>
          <div>
            <dt>Destination</dt>
            <dd>{plan.rail_name}</dd>
          </div>
          <div>
            <dt>Amount</dt>
            <dd>{plan.amount}</dd>
          </div>
          <div>
            <dt>Incremental fee</dt>
            <dd>
              {plan.incremental_fee} <span className="muted">absorbed by the operator</span>
            </dd>
          </div>
          <div>
            <dt>Plan revision</dt>
            <dd>{plan.bound_revision}</dd>
          </div>
          <div>
            <dt>Plan hash</dt>
            <dd className="mono" title={plan.plan_hash}>
              {plan.plan_hash_short}
            </dd>
          </div>
        </dl>
        <details className="plan-reasons">
          <summary>Why the planner chose this route</summary>
          <ul>
            {plan.selection_reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </details>
      </div>
      <LedgerDiff preview={view.preview} />
      {approval ? (
        <p className="done-note">
          ✓ Approved by <span className="mono">{approval.approver}</span> (unauthenticated demo
          operator) at{" "}
          {clock(approval.decided_at)} for hash{" "}
          <span className="mono">{approval.plan_hash_short}</span>
        </p>
      ) : (
        view.next_action === "approve" && (
          <button className="primary" onClick={onApprove} disabled={pending !== null}>
            {pending === "approve" ? <Spinner label="Approving" /> : "Approve recovery plan"}
          </button>
        )
      )}
      <p className="muted small">
        The approval is bound to this plan's hash and revision. If the route, quote, fee,
        competing routes or transaction change, the plan goes stale and a new plan needs a new
        approval.
      </p>
    </section>
  );
}

// ------------------------------------------------------------------ execution

const PROGRESSION = ["APPROVED", "RECOVERY_EXECUTING", "RECOVERED", "RECONCILED"];

interface ExecuteProps {
  view: DemoView;
  pending: Action | null;
  onExecute: () => void;
  sectionRef: Ref<HTMLElement>;
}

export function ExecutionPanel({ view, pending, onExecute, sectionRef }: ExecuteProps) {
  const plan = view.analysis?.plan;
  if (!plan || !view.approval) return null;
  const running = pending === "execute";
  const reached = new Map(view.transitions.map((t) => [t.to_state, t.at]));
  return (
    <section className="step-section" ref={sectionRef} aria-labelledby="execute-title">
      <h2 id="execute-title">Execute the outstanding payout</h2>
      <p className="lead">
        Sends only {plan.amount} from <span className="mono">{plan.source}</span> via{" "}
        {plan.rail_name}. Legs that already succeeded are never repeated.
      </p>
      {view.next_action === "execute" && (
        <button className="primary" onClick={onExecute} disabled={pending !== null}>
          {running ? <Spinner label="Executing recovery" /> : "Execute recovery"}
        </button>
      )}
      <ol className="progression" aria-label="Recovery state">
        {PROGRESSION.map((state) => {
          const at = reached.get(state);
          const status = at ? "done" : running && state === "RECOVERY_EXECUTING" ? "active" : "todo";
          return (
            <li key={state} className={`progression-step progression-step--${status}`}>
              <span className="progression-name">{stateName(state)}</span>
              <span className="progression-time mono">
                {at ? clock(at) : status === "active" ? "in progress" : ""}
              </span>
            </li>
          );
        })}
      </ol>
      {view.execution && view.execution.status !== "SUCCEEDED" && (
        <p className="error-note">
          Payout outcome {view.execution.status}: {view.execution.detail}. No automatic retry:
          this needs manual review.
        </p>
      )}
    </section>
  );
}

// ------------------------------------------------------------------ proof

interface ProofProps {
  view: DemoView;
  pending: Action | null;
  onReset: () => void;
  sectionRef: Ref<HTMLElement>;
}

export function ProofPanel({ view, pending, onReset, sectionRef }: ProofProps) {
  const r = view.reconciliation;
  const plan = view.analysis?.plan;
  if (!r || !r.reconciled || !plan) return null;
  const passed = (name: string) => r.checks.find((c) => c.name === name)?.passed ?? false;
  const rows: { label: string; value: string; ok: boolean | null }[] = [
    { label: "Recipient credited", value: view.diagnosis.recipient_credited ? "Yes" : "No", ok: passed("single_recipient_credit") },
    { label: "Sender debit count", value: String(r.sender_debit_count), ok: passed("single_sender_debit") },
    { label: "Recipient credit count", value: String(r.recipient_credit_count), ok: passed("single_recipient_credit") },
    { label: "Duplicate sender debits", value: String(r.duplicate_sender_debits), ok: passed("single_sender_debit") },
    { label: "Final funds location", value: r.funds_location, ok: passed("funds_at_recipient") },
    { label: "Recovery rail", value: plan.rail_name, ok: null },
    { label: "Incremental recovery fee", value: `${plan.incremental_fee}, absorbed by the operator`, ok: null },
  ];
  const allPassed = r.checks.every((c) => c.passed);
  return (
    <section className="proof" ref={sectionRef} aria-labelledby="proof-title">
      <h2 id="proof-title">Recovered: internal reconciliation checks passed</h2>
      <table className="ledger">
        <tbody>
          {rows.map((row) => (
            <tr key={row.label}>
              <th scope="row">{row.label}</th>
              <td className={row.label === "Final funds location" ? "mono" : undefined}>
                {row.value}
                {row.label === "Final funds location" && (
                  <span className="ledger-note">{accountName(row.value)}</span>
                )}
              </td>
              <td className="ledger-check">{row.ok === null ? "" : row.ok ? "✓" : "✗"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="proof-statement">Only the outstanding payout leg was executed.</p>
      <p className="muted">
        {view.payout_calls} payout call, via {plan.rail_name} from{" "}
        <span className="mono">{plan.source}</span>.{" "}
        {allPassed
          ? `All ${r.checks.length} internal reconciliation checks passed against this demo's own journal.`
          : "Some internal reconciliation checks failed."}
      </p>
      <button className="secondary" onClick={onReset} disabled={pending !== null}>
        {pending === "reset" ? <Spinner label="Resetting" /> : "Reset demo"}
      </button>
    </section>
  );
}

export function Spinner({ label }: { label: string }) {
  return (
    <span className="spinner-wrap">
      <span className="spinner" aria-hidden="true" />
      {label}
    </span>
  );
}
