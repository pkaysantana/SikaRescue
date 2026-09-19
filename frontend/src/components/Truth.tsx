import { useState } from "react";

import { accountName } from "../format";
import type { DemoView, Diagnosis, JourneyLeg } from "../types";
import { EffectGraph } from "./ControlPlane";

export function PaymentSummary({ view }: { view: DemoView }) {
  const t = view.transaction;
  const retired = view.retired_instance_ids.length;
  return (
    <section className="summary" aria-label="Payment">
      <div className="summary-id">
        <span className="muted">Scenario {t.scenario_id}, payment instance</span>
        <strong className="mono">{t.payment_instance_id}</strong>
        {retired > 0 && (
          <span className="summary-retired">
            {retired} earlier {retired === 1 ? "instance" : "instances"} retired after reaching the
            payout provider. Their ids are never reused.
          </span>
        )}
      </div>
      <p className="summary-amount">{t.send_amount}</p>
      <dl className="summary-facts">
        <div>
          <dt>Route</dt>
          <dd>
            {t.origin} → {t.destination}
          </dd>
        </div>
        <div>
          <dt>Recipient rail</dt>
          <dd>{t.recipient_rail}</dd>
        </div>
        <div>
          <dt>Recipient receives</dt>
          <dd>{t.payout_amount}</dd>
        </div>
      </dl>
    </section>
  );
}

function legTitle(leg: JourneyLeg): string {
  if (leg.label !== "Payout") return leg.label;
  return leg.is_recovery ? `Recovery payout via ${leg.rail_name}` : `Payout via ${leg.rail_name}`;
}

const STATUS_TEXT: Record<JourneyLeg["status"], string> = {
  SUCCESS: "Succeeded",
  FAILED: "Definitive failure",
  UNKNOWN: "Outcome unknown",
  IN_PROGRESS: "In progress",
  AWAITING_EVIDENCE: "Response not classified yet",
};

function FundsFlag({ account, diagnosis }: { account: string; diagnosis: Diagnosis }) {
  // "Funds are here" only when the journal proves it; otherwise the last confirmed location.
  const uncertain = diagnosis.funds_certainty === "UNCERTAIN";
  return (
    <div className={`funds-flag${uncertain ? " funds-flag--uncertain" : ""}`} role="status">
      <span className="funds-flag-label">{diagnosis.funds_label}</span>
      <span className="funds-flag-account mono">{account}</span>
      <span className="funds-flag-note">{accountName(account)}</span>
      {uncertain && (
        <span className="funds-flag-warning">The recipient may already have been credited.</span>
      )}
    </div>
  );
}

export function Journey({ view }: { view: DemoView }) {
  const { journey, diagnosis } = view;
  const uncertain = diagnosis.funds_certainty === "UNCERTAIN";
  const [mode, setMode] = useState<"legs" | "graph">("legs");
  return (
    <section className="journey" aria-labelledby="journey-title">
      <div className="journey-head">
        <h2 id="journey-title">
          {uncertain ? "Last confirmed money position" : "Where the money is"}
        </h2>
        <div className="segmented" role="group" aria-label="Journey view">
          <button
            className={`segment${mode === "legs" ? " segment--on" : ""}`}
            aria-pressed={mode === "legs"}
            onClick={() => setMode("legs")}
          >
            Journey
          </button>
          <button
            className={`segment${mode === "graph" ? " segment--on" : ""}`}
            aria-pressed={mode === "graph"}
            onClick={() => setMode("graph")}
          >
            Effect graph
          </button>
        </div>
      </div>
      {mode === "graph" ? (
        <EffectGraph view={view} />
      ) : (
      <ol className="spine">
        {journey.map((leg, i) => (
          <li key={`${leg.rail_id}-${i}`} className={`leg leg--${leg.status.toLowerCase()}`}>
            <span className={`node${leg.funds_here ? " node--funds" : ""}`} aria-hidden="true" />
            <div className="leg-body">
              <div className="leg-head">
                <span className="leg-title">{legTitle(leg)}</span>
                <span className="leg-status">{STATUS_TEXT[leg.status]}</span>
              </div>
              <p className="leg-meta">
                {leg.label === "Payout" ? "Pays out" : leg.rail_name} into{" "}
                <span className="mono">{leg.destination}</span>
              </p>
              {leg.status === "FAILED" && leg.detail && (
                <p className="leg-failure">{leg.detail}. No value moved.</p>
              )}
              {leg.funds_here && <FundsFlag account={leg.destination} diagnosis={diagnosis} />}
            </div>
          </li>
        ))}
        {!diagnosis.recipient_credited && (diagnosis.outstanding || uncertain) && (
          <li className="leg leg--waiting">
            <span className="node" aria-hidden="true" />
            <div className="leg-body">
              <div className="leg-head">
                <span className="leg-title">Recipient wallet</span>
                <span className="leg-status">
                  {uncertain
                    ? "Unconfirmed: may already be credited"
                    : `Still owed ${diagnosis.outstanding}`}
                </span>
              </div>
            </div>
          </li>
        )}
      </ol>
      )}
    </section>
  );
}

export function SafetyNote({ view }: { view: DemoView }) {
  const d = view.diagnosis;
  if (!d.recipient_credited) return null; // the funds position panel covers the open case
  return (
    <section className="safety safety--resolved" aria-label="Safety">
      <p className="safety-lead">The recovery did not charge the sender again.</p>
      <p className="muted">
        It continued from where the funds were. The ledger shows {d.sender_debit_count} sender
        debit.
      </p>
    </section>
  );
}
