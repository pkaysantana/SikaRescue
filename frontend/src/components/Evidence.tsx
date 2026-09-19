import type { Ref } from "react";

import type { Action, Check, DemoView, Extraction, Incident, Verdict } from "../types";
import { Spinner } from "./Workflow";

// ------------------------------------------------------------------ scenario switch

interface ScenarioProps {
  view: DemoView;
  pending: Action | null;
  onSwitch: (scenario: string) => void;
}

export function ScenarioSwitch({ view, pending, onSwitch }: ScenarioProps) {
  const { current, options } = view.scenario;
  if (current === null) return null;
  return (
    <div className="scenario-switch" role="group" aria-label="Provider incident scenario">
      <span className="scenario-switch-label">MOMO_A incident</span>
      {options.map((option) => (
        <button
          key={option.id}
          className={`segment${option.id === current ? " segment--on" : ""}`}
          aria-pressed={option.id === current}
          disabled={pending !== null}
          onClick={() => option.id !== current && onSwitch(option.id)}
        >
          {option.label}
        </button>
      ))}
      <span className="muted small">Switching starts a new payment instance.</span>
    </div>
  );
}

// ------------------------------------------------------------------ evidence panel

const TRANSPORT_TEXT: Record<string, string> = {
  RESPONSE_RECEIVED: "Response received",
  TIMEOUT: "Timed out, no response",
  CONNECTION_LOST: "Connection lost",
  NOT_STATED: "Not stated",
};

const STAGE_TEXT: Record<string, string> = {
  PRE_ACCEPTANCE: "Before acceptance",
  POST_ACCEPTANCE: "After acceptance",
  UNKNOWN: "Unknown",
};

function transportLine(incident: Incident): string {
  if (incident.response_received) {
    return `Request sent. HTTP ${incident.http_status} after ${incident.elapsed_ms} ms.`;
  }
  return `Request sent. No response within the ${incident.timeout_ms.toLocaleString("en-GB")} ms timeout.`;
}

function EvidenceFields({ extraction }: { extraction: Extraction }) {
  const e = extraction.evidence;
  const rejection =
    e.explicit_rejection === null ? "Not stated" : e.explicit_rejection ? "Yes" : "No";
  return (
    <>
      <dl className="evidence-fields">
        <div>
          <dt>Provider code</dt>
          <dd className="mono">{e.provider_code ?? "none"}</dd>
        </div>
        <div>
          <dt>Transport</dt>
          <dd>{TRANSPORT_TEXT[e.transport_outcome] ?? e.transport_outcome}</dd>
        </div>
        <div>
          <dt>Acceptance stage</dt>
          <dd>{STAGE_TEXT[e.acceptance_stage] ?? e.acceptance_stage}</dd>
        </div>
        <div>
          <dt>Explicit rejection</dt>
          <dd>{rejection}</dd>
        </div>
        {e.provider_reference && (
          <div>
            <dt>Provider reference</dt>
            <dd className="mono">{e.provider_reference}</dd>
          </div>
        )}
      </dl>
      {e.fragments.length > 0 && (
        <ul className="citations" aria-label="Verbatim citations">
          {e.fragments.map((fragment) => (
            <li key={fragment}>
              <q>{fragment}</q>
            </li>
          ))}
        </ul>
      )}
      {extraction.fallback_reason && (
        <p className="warning-note">Fallback reason: {extraction.fallback_reason}</p>
      )}
    </>
  );
}

function CheckList({ checks, label }: { checks: Check[]; label: string }) {
  return (
    <ul className="checklist" aria-label={label}>
      {checks.map((c) => (
        <li key={c.name} className={c.passed ? "check check--pass" : "check check--fail"}>
          <span className="check-mark" aria-hidden="true">
            {c.passed ? "✓" : "✗"}
          </span>
          <span>
            <strong>{c.label}</strong>
            <span className="check-detail">{c.detail}</span>
          </span>
        </li>
      ))}
    </ul>
  );
}

const VERDICT_TEXT: Record<Verdict["classification"], { title: string; tone: string }> = {
  DEFINITIVE_FAILED: { title: "Definitive failure: no value moved", tone: "good" },
  UNKNOWN: { title: "Outcome unknown: the recipient may already have been credited", tone: "bad" },
  SUCCEEDED: { title: "Provider reports success: confirm and book manually", tone: "bad" },
};

function VerdictBlock({ verdict }: { verdict: Verdict }) {
  const text = VERDICT_TEXT[verdict.classification];
  return (
    <>
      <p className={`verdict verdict--${text.tone}`}>
        <span className="mono">{verdict.classification}</span> {text.title}
      </p>
      <CheckList checks={verdict.checks} label="Integrity checks" />
      <p className="checklist-head">To prove a definitive failure, all of these must hold:</p>
      <CheckList checks={verdict.requirements} label="Definitive-failure requirements" />
      {verdict.recorded_outcome !== verdict.classification && (
        <p className="warning-note">
          Recorded in the journal as {verdict.recorded_outcome}: extracted evidence never books a
          credit.
        </p>
      )}
    </>
  );
}

interface EvidenceProps {
  view: DemoView;
  pending: Action | null;
  onClassify: () => void;
  sectionRef: Ref<HTMLElement>;
}

export function EvidencePanel({ view, pending, onClassify, sectionRef }: EvidenceProps) {
  const incident = view.incident;
  if (!incident) return null;
  const extraction = incident.extraction;
  const running = pending === "classify";
  return (
    <section className="step-section evidence" ref={sectionRef} aria-labelledby="evidence-title">
      <h2 id="evidence-title">Classify the provider's response</h2>
      <p className="lead">
        {incident.rail_id} answered the payout, and what it said decides whether money may move.
        Pydantic AI extracts typed evidence with verbatim citations. A deterministic verifier
        decides, and anything it can't prove is UNKNOWN. The AI can't authorise a payout.
      </p>
      <ol className="evidence-flow">
        <li className="evidence-stage">
          <h3>Raw provider response</h3>
          <p className="muted small">
            {transportLine(incident)} Synthetic payload, digest{" "}
            <span className="mono">{incident.digest_short}</span>.
          </p>
          <pre className="payload" tabIndex={0}>
            {incident.raw_payload}
          </pre>
        </li>
        <li className="evidence-stage">
          <h3>FailureEvidence</h3>
          {extraction ? (
            <>
              <p className="muted small">
                {extraction.label}
                {extraction.provider_model && (
                  <>
                    {" "}
                    using <span className="mono">{extraction.provider_model}</span>
                  </>
                )}
                {extraction.gateway_route && <> via Gateway route {extraction.gateway_route}</>}
              </p>
              <EvidenceFields extraction={extraction} />
            </>
          ) : (
            <p className="muted">Not extracted yet.</p>
          )}
        </li>
        <li className="evidence-stage">
          <h3>Deterministic verdict</h3>
          {incident.verdict ? (
            <VerdictBlock verdict={incident.verdict} />
          ) : (
            <p className="muted">Not classified yet. Nothing can be planned until it is.</p>
          )}
        </li>
      </ol>
      {view.next_action === "classify" && (
        <button className="primary" onClick={onClassify} disabled={pending !== null}>
          {running ? <Spinner label="Extracting and verifying" /> : "Classify provider response"}
        </button>
      )}
      {running && (
        <p className="progress-note" role="status">
          {view.agent_mode === "pydantic"
            ? "Pydantic AI is reading the payload; the verifier then checks every citation."
            : "AI extraction is off: only our own transport log is read, so this fails closed."}
        </p>
      )}
    </section>
  );
}
