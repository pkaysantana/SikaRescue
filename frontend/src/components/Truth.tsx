import { accountName } from "../format";
import type { DemoView, JourneyLeg } from "../types";

export function PaymentSummary({ view }: { view: DemoView }) {
  const t = view.transaction;
  return (
    <section className="summary" aria-label="Payment">
      <div className="summary-id">
        <span className="muted">Payment</span>
        <strong className="mono">{t.transaction_id}</strong>
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
};

function FundsFlag({ account }: { account: string }) {
  return (
    <div className="funds-flag" role="status">
      <span className="funds-flag-label">Funds are here</span>
      <span className="funds-flag-account mono">{account}</span>
      <span className="funds-flag-note">{accountName(account)}</span>
    </div>
  );
}

export function Journey({ view }: { view: DemoView }) {
  const { journey, diagnosis } = view;
  return (
    <section className="journey" aria-labelledby="journey-title">
      <h2 id="journey-title">Where the money is</h2>
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
              {leg.funds_here && <FundsFlag account={leg.destination} />}
            </div>
          </li>
        ))}
        {!diagnosis.recipient_credited && diagnosis.outstanding && (
          <li className="leg leg--waiting">
            <span className="node" aria-hidden="true" />
            <div className="leg-body">
              <div className="leg-head">
                <span className="leg-title">Recipient wallet</span>
                <span className="leg-status">Still owed {diagnosis.outstanding}</span>
              </div>
            </div>
          </li>
        )}
      </ol>
    </section>
  );
}

export function SafetyNote({ view }: { view: DemoView }) {
  const d = view.diagnosis;
  if (d.recipient_credited) {
    return (
      <section className="safety safety--resolved" aria-label="Safety">
        <p className="safety-lead">The sender was never charged twice.</p>
        <p className="muted">
          Recovery continued from where the funds were. {d.sender_debit_count} sender debit on the
          ledger.
        </p>
      </section>
    );
  }
  return (
    <section className="safety" aria-label="Safety">
      <p className="safety-lead">Restarting this payment would risk charging the sender twice.</p>
      <dl className="safety-facts">
        <div>
          <dt>Sender debit already recorded</dt>
          <dd>{d.sender_debited ? `Yes, ${d.sender_debit_count} debit` : "No"}</dd>
        </div>
        <div>
          <dt>Safe to restart from origin</dt>
          <dd>{d.safe_to_restart_from_origin ? "Yes" : "No"}</dd>
        </div>
        <div>
          <dt>Safe recovery action</dt>
          <dd>
            Resume from the current funds location,{" "}
            <span className="mono">{d.funds_location}</span>
          </dd>
        </div>
      </dl>
    </section>
  );
}
