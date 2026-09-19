import type { Ref } from "react";

import { accountName, sentence } from "../format";
import type { Counterfactual, DemoView, EffectNode, Frontier, Preview } from "../types";

// ------------------------------------------------------------------ funds position

const POSITION_TEXT: Record<string, string> = {
  AVAILABLE: "Available: proven in place",
  IN_FLIGHT: "In flight: outcome not established",
  UNCERTAIN: "Uncertain: may already have moved",
  FINAL: "Final: delivered to the recipient",
};

export function FundsPositionPanel({ view }: { view: DemoView }) {
  const p = view.funds_position;
  const d = view.diagnosis;
  return (
    <section
      className={`position position--${p.position_status.toLowerCase()}`}
      aria-labelledby="position-title"
    >
      <h2 id="position-title">Funds position</h2>
      <p className="position-amount">{p.amount}</p>
      <p className="position-where">
        <span className="position-label">{p.label}:</span>{" "}
        <span className="mono">{p.last_confirmed_location}</span>
        <span className="muted small"> {accountName(p.last_confirmed_location)}</span>
      </p>
      <dl className="safety-facts">
        <div>
          <dt>Position</dt>
          <dd>{POSITION_TEXT[p.position_status]}</dd>
        </div>
        <div>
          <dt>Certainty</dt>
          <dd className="mono">{p.certainty}</dd>
        </div>
        <div>
          <dt>Automatic action</dt>
          <dd>{p.available_for_automatic_action ? "Available" : "Not available"}</dd>
        </div>
        <div>
          <dt>Proven by</dt>
          <dd>{p.derived_from.length} journal effects</dd>
        </div>
        <div>
          <dt>Restart from origin</dt>
          <dd>
            {d.safe_to_restart_from_origin
              ? "Safe: nothing has moved yet"
              : `Not safe: ${d.sender_debit_count} sender debit already posted`}
          </dd>
        </div>
      </dl>
      {p.reason && <p className="position-reason">{sentence(p.reason)}.</p>}
    </section>
  );
}

// ------------------------------------------------------------------ effect graph

const NODE_STATUS: Record<string, string> = {
  OPEN: "Open",
  FULFILLED: "Fulfilled",
  COMPLETE: "Posted",
  PENDING: "Not attempted",
  FAILED: "Failed, no value moved",
  AWAITING_EVIDENCE: "Awaiting evidence",
  IN_FLIGHT: "In flight",
  UNKNOWN: "Outcome unknown",
};

function EffectNodeRow({ node }: { node: EffectNode }) {
  const intent = node.kind === "PAYMENT_INTENT";
  return (
    <li className={`gnode gnode--${node.status.toLowerCase()}`}>
      <div className="gnode-head">
        <span className="gnode-label">{node.label}</span>
        <span className="gnode-status">{NODE_STATUS[node.status] ?? node.status}</span>
      </div>
      <p className="gnode-id mono">{node.node_id}</p>
      {!intent && (
        <p className="gnode-flow mono">
          {node.source} → {node.destination}
        </p>
      )}
      {node.source_amount && (
        <p className="gnode-amounts">
          {node.source_amount}
          {node.destination_amount && node.destination_amount !== node.source_amount
            ? ` → ${node.destination_amount}`
            : ""}
          {node.rail_name && <span className="muted"> via {node.rail_name}</span>}
        </p>
      )}
      {node.outstanding && <p className="gnode-owed">Owed {node.outstanding}</p>}
      {node.attempts.length > 0 && !intent && node.status !== "COMPLETE" && (
        <ul className="gnode-attempts">
          {node.attempts.map((a) => (
            <li key={a}>{a}</li>
          ))}
        </ul>
      )}
    </li>
  );
}

export function EffectGraph({ view }: { view: DemoView }) {
  return (
    <>
      <ol className="graph" aria-label="Financial effect graph">
        {view.effect_graph.map((node) => (
          <EffectNodeRow key={node.node_id} node={node} />
        ))}
      </ol>
      <p className="muted small">
        Derived from the journal on every view. It is not a second source of truth.
      </p>
    </>
  );
}

// ------------------------------------------------------------------ safe action frontier

function Funnel({ f }: { f: Frontier }) {
  const stages = [
    { n: f.candidates, label: "candidate routes" },
    { n: f.rejected_before_simulation, label: "rejected before simulation" },
    { n: f.eligible, label: "eligible" },
    { n: f.simulated, label: f.simulated ? "simulated" : "simulated (after analysis)" },
    { n: f.selected, label: "selected plan" },
  ];
  return (
    <ol className="funnel">
      {stages.map((s) => (
        <li key={s.label}>
          <strong>{s.n}</strong>
          <span>{s.label}</span>
        </li>
      ))}
    </ol>
  );
}

export function FrontierPanel({
  view,
  sectionRef,
}: {
  view: DemoView;
  sectionRef: Ref<HTMLElement>;
}) {
  const f = view.frontier;
  if (f.basis === "SETTLED" || f.basis === "PENDING_EVIDENCE") return null;
  const blocked = !f.payout_actions_permitted;
  return (
    <section className="step-section frontier" ref={sectionRef} aria-labelledby="frontier-title">
      <h2 id="frontier-title">Safe action frontier</h2>
      {blocked ? (
        <p className="frontier-blocked" role="status">
          Manual review required. There is no executable payout.
        </p>
      ) : (
        <Funnel f={f} />
      )}
      <p className="lead">{f.reason}</p>
      <ul className="frontier-actions">
        {f.actions.map((a) => (
          <li key={a.action_id} className={a.selected ? "faction faction--selected" : "faction"}>
            <span className={a.rail_name ? "faction-label" : "faction-label faction-label--wide"}>
              {a.rail_name ?? a.label}
            </span>
            <span className="faction-state">
              {a.kind !== "PAYOUT" && !a.implemented && (
                <span className="tag">Demo abstraction</span>
              )}
              {a.kind === "PAYOUT" && a.eligible === false && (
                <span className="tag tag--rejected">Rejected before simulation</span>
              )}
              {a.kind === "PAYOUT" && a.eligible && !a.simulated && <span className="tag">Eligible</span>}
              {a.simulated && (
                <span className={a.selected ? "tag tag--selected" : "tag"}>
                  {a.selected ? "Selected, rank 1" : `Rank ${a.rank}`}
                  {a.simulated_reliability && `, ${a.simulated_reliability} simulated success`}
                </span>
              )}
            </span>
            {a.kind === "PAYOUT" && a.rejection_details.length > 0 && (
              <span className="faction-note">{a.rejection_details.join("; ")}</span>
            )}
            {a.kind !== "PAYOUT" && a.note && <span className="faction-note">{a.note}</span>}
          </li>
        ))}
      </ul>
    </section>
  );
}

// ------------------------------------------------------------------ why not just retry

export function RetryPanel({ view }: { view: DemoView }) {
  const c: Counterfactual = view.counterfactual;
  const f = view.frontier;
  if (f.basis === "PENDING_EVIDENCE" || f.basis === "SETTLED") return null;
  return (
    <section className="step-section retry" aria-labelledby="retry-title">
      <h2 id="retry-title">Why not just retry?</h2>
      <div className="retry-grid">
        <div className="retry-col retry-col--naive">
          <h3>Restart from origin</h3>
          <ol>
            {c.naive_steps.map((s) => (
              <li key={s.label} className={s.risk ? "rstep rstep--risk" : "rstep"}>
                <span>
                  {s.label} <span className="muted">{s.amount}</span>
                </span>
                {s.risk && <span className="rstep-risk">{s.risk}</span>}
              </li>
            ))}
          </ol>
          <p className="retry-sum">
            Repeats {c.naive_repeated_effects} completed effects
            {c.naive_extra_sender_debit && `, charging the sender ${c.naive_extra_sender_debit} again`}
            .{c.duplicate_recipient_credit_risk && " It may also credit the recipient twice."}
          </p>
        </div>
        <div className="retry-col retry-col--ours">
          <h3>SikaRescue</h3>
          {c.sikarescue_steps.length > 0 ? (
            <ol>
              {c.sikarescue_steps.map((s) => (
                <li key={s.label} className="rstep">
                  <span>
                    {s.label} <span className="muted">{s.amount}</span>
                    {s.rail_name && <span className="muted"> via {s.rail_name}</span>}
                  </span>
                </li>
              ))}
            </ol>
          ) : null}
          <p className="retry-sum">{c.sikarescue_action} No completed effect is repeated.</p>
        </div>
      </div>
      <p className="muted small">A dry run over the journal. Nothing was attempted.</p>
    </section>
  );
}

// ------------------------------------------------------------------ ledger diff

export function LedgerDiff({ preview }: { preview: Preview | null }) {
  if (!preview) return null;
  if (!preview.valid) {
    return (
      <div className="ledger-diff ledger-diff--invalid" role="status">
        <h3>Ledger preview invalidated</h3>
        <ul>
          {preview.invalidation_reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
        <p className="muted small">A stale plan cannot execute: it needs a new plan and a new approval.</p>
      </div>
    );
  }
  return (
    <div className="ledger-diff">
      <h3>Ledger now vs after execution</h3>
      <table className="diff">
        <thead>
          <tr>
            <th scope="col">Journal</th>
            <th scope="col">Now</th>
            <th scope="col">After execution</th>
          </tr>
        </thead>
        <tbody>
          {preview.rows.map((row) => (
            <tr key={row.label} className={row.changed ? "diff-row--changed" : undefined}>
              <th scope="row">{row.label}</th>
              <td>{row.current}</td>
              <td>{row.proposed}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small">
        Projected by posting this plan's effect, built by the same code execution uses, into a
        throwaway copy of the journal. Nothing is written.
      </p>
    </div>
  );
}
