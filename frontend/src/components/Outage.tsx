import { useState } from "react";

import { grouped } from "../format";
import type { OutageAllocation, OutageScenario, OutageView } from "../types";
import { Spinner } from "./Workflow";

function Bar({ value, text }: { value: number; text: string }) {
  return (
    <span className="bar" role="img" aria-label={text}>
      <span className="bar-fill" style={{ width: `${Math.min(value, 1) * 100}%` }} />
    </span>
  );
}

function AllocationRow({ a }: { a: OutageAllocation }) {
  return (
    <tr>
      <th scope="row">{a.rail_name}</th>
      <td className="num">{grouped(a.obligations)}</td>
      <td>
        <Bar value={a.liquidity_utilisation} text={`liquidity ${a.liquidity_utilisation_text}`} />
        <span className="bar-text">
          {a.amount} of {a.liquidity}
        </span>
      </td>
      <td>
        <Bar value={a.capacity_utilisation} text={`capacity ${a.capacity_utilisation_text}`} />
        <span className="bar-text">
          {grouped(a.obligations)} of {grouped(a.capacity)} slots
        </span>
      </td>
      <td className="num">{a.incremental_fee}</td>
    </tr>
  );
}

function ScenarioDetail({ s }: { s: OutageScenario }) {
  return (
    <div className="outage-detail">
      <h3>{s.label}</h3>
      <p className="muted">{s.description}</p>
      <table className="alloc">
        <thead>
          <tr>
            <th scope="col">Fallback rail</th>
            <th scope="col">Recoveries</th>
            <th scope="col">Liquidity used</th>
            <th scope="col">Capacity used</th>
            <th scope="col">Fee</th>
          </tr>
        </thead>
        <tbody>
          {s.allocations.map((a) => (
            <AllocationRow key={a.rail_id} a={a} />
          ))}
        </tbody>
      </table>
      {s.unserved.length > 0 && (
        <ul className="unserved">
          {s.unserved.map((u) => (
            <li key={u.reason}>
              <strong>{grouped(u.obligations)}</strong> {u.label.toLowerCase()} ({u.amount})
            </li>
          ))}
        </ul>
      )}
      <p className="muted small">Rejected before compute: {rejectedRails(s)}.</p>
    </div>
  );
}

const REJECTION_TEXT: Record<string, string> = {
  RAIL_UNAVAILABLE: "rail down",
  POLICY_DENIED: "not permitted by corridor policy",
};

function rejectedRails(s: OutageScenario): string {
  const rejected = s.rails.filter((r) => !r.eligible);
  const why = (reasons: string[]) => reasons.map((x) => REJECTION_TEXT[x] ?? x).join(", ");
  return rejected.map((r) => `${r.rail_name} (${why(r.rejection_reasons)})`).join("; ") || "none";
}

interface OutageProps {
  outage: OutageView | null;
  running: boolean;
  disabled: boolean;
  onRun: () => void;
}

export function OutagePanel({ outage, running, disabled, onRun }: OutageProps) {
  const [selected, setSelected] = useState(0);
  const scenario = outage?.scenarios[Math.min(selected, outage.scenarios.length - 1)];
  return (
    <main className="outage">
      <header className="outage-head">
        <h1>Systemic outage: MOMO_A fails for the whole corridor</h1>
        <p className="lead">
          How many stranded payouts can each fallback rail safely absorb? Rail status, corridor
          policy and recipient compatibility are decided locally. The allocation kernel then runs
          one job per scenario, and every allocation is re-verified before any figure is shown.
        </p>
        <button className="primary" onClick={onRun} disabled={disabled || running}>
          {running ? (
            <Spinner label="Allocating 5 scenarios" />
          ) : outage ? (
            "Run the analysis again"
          ) : (
            "Run fleet outage analysis"
          )}
        </button>
      </header>

      {outage && scenario && (
        <>
          <dl className="metrics outage-facts">
            <div>
              <dt>Stranded payouts</dt>
              <dd>{grouped(outage.portfolio.size)}</dd>
            </div>
            <div>
              <dt>Value</dt>
              <dd>{outage.portfolio.total_amount}</dd>
            </div>
            <div>
              <dt>Mobile money / bank</dt>
              <dd>
                {grouped(outage.portfolio.mobile_money)} / {grouped(outage.portfolio.bank_account)}
              </dd>
            </div>
            <div>
              <dt>Over policy limit</dt>
              <dd>{grouped(outage.portfolio.over_policy_limit)}</dd>
            </div>
          </dl>
          <p className="compute-where">
            {outage.backend === "modal"
              ? `Ran on Modal: ${outage.parallel_jobs} parallel jobs`
              : outage.fallback_from
                ? `${outage.fallback_from} unavailable: ran locally instead`
                : `Ran locally: ${outage.parallel_jobs} scenarios`}
            <span className="muted">
              {" "}
              in {outage.compute_wall_seconds.toFixed(2)} s (kernel {outage.kernel_seconds.toFixed(2)} s,
              local verification {outage.verification_seconds.toFixed(2)} s)
            </span>
          </p>
          {outage.fallback_reason && <p className="warning-note">Reason: {outage.fallback_reason}</p>}

          <table className="scenarios">
            <thead>
              <tr>
                <th scope="col">Scenario</th>
                <th scope="col">Recoverable</th>
                <th scope="col">Unserved</th>
                <th scope="col">Main reason unserved</th>
                <th scope="col">Recovery fees</th>
              </tr>
            </thead>
            <tbody>
              {outage.scenarios.map((s, i) => {
                const main = [...s.unserved].sort((a, b) => b.obligations - a.obligations)[0];
                return (
                  <tr
                    key={s.scenario_id}
                    className={i === selected ? "row--selected" : undefined}
                    onClick={() => setSelected(i)}
                  >
                    <th scope="row">
                      <button className="linklike" onClick={() => setSelected(i)} aria-pressed={i === selected}>
                        {s.label}
                      </button>
                    </th>
                    <td className="num">
                      {grouped(s.recoverable_obligations)} <span className="muted">({s.recoverable_share})</span>
                    </td>
                    <td className="num">{grouped(s.unserved_obligations)}</td>
                    <td>{main ? main.label : "none"}</td>
                    <td className="num">{s.aggregate_incremental_fee}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>

          <ScenarioDetail s={scenario} />

          <details className="plan-reasons">
            <summary>What was verified locally</summary>
            <ul>
              {scenario.checks.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
          </details>
          <p className="synthetic">
            Synthetic portfolio (seed {outage.portfolio.seed}, digest{" "}
            <span className="mono">{outage.portfolio.digest_short}</span>). Fleet liquidity and
            capacity are illustrative, not measured.
          </p>
        </>
      )}
    </main>
  );
}
