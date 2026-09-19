import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, api } from "./api";
import { PaymentSummary, Journey, SafetyNote } from "./components/Truth";
import {
  AdviceCard,
  AnalyseStep,
  ComputePanel,
  ExecutionPanel,
  PlanApproval,
  ProofPanel,
  RouteList,
  Spinner,
  Stepper,
} from "./components/Workflow";
import type { Action, DemoView, RecoveryState } from "./types";

type Focus = "routes" | "execute" | "proof" | "top";

const BADGES: Record<RecoveryState, { text: string; tone: string }> = {
  FAILED: { text: "FAILED", tone: "bad" },
  DIAGNOSING: { text: "ANALYSING", tone: "busy" },
  AWAITING_APPROVAL: { text: "AWAITING APPROVAL", tone: "wait" },
  APPROVED: { text: "APPROVED", tone: "wait" },
  RECOVERY_EXECUTING: { text: "EXECUTING", tone: "busy" },
  RECOVERED: { text: "RECOVERED", tone: "good" },
  RECONCILED: { text: "RECONCILED", tone: "good" },
  RECOVERY_FAILED: { text: "RECOVERY FAILED", tone: "bad" },
  MANUAL_REVIEW: { text: "MANUAL REVIEW", tone: "bad" },
};

function StatusBadge({ state, pending }: { state: RecoveryState; pending: Action | null }) {
  // While a request runs, say what is happening; otherwise show the backend's state.
  const badge =
    pending === "analyse"
      ? { text: "ANALYSING", tone: "busy" }
      : pending === "execute"
        ? { text: "EXECUTING", tone: "busy" }
        : BADGES[state];
  return (
    <span className={`badge badge--${badge.tone}`} role="status" aria-live="polite">
      {badge.text}
    </span>
  );
}

const prefersReducedMotion = () =>
  window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

export default function App() {
  const [view, setView] = useState<DemoView | null>(null);
  const [pending, setPending] = useState<Action | null>("load");
  const [error, setError] = useState<{ message: string; code: string } | null>(null);
  const [focus, setFocus] = useState<Focus | null>(null);
  const inFlight = useRef(false);
  const routesRef = useRef<HTMLElement>(null);
  const executeRef = useRef<HTMLElement>(null);
  const proofRef = useRef<HTMLElement>(null);

  const run = useCallback(
    async (action: Action, call: () => Promise<DemoView>, then?: Focus) => {
      if (inFlight.current) return; // one request at a time: double-clicks are ignored
      inFlight.current = true;
      setPending(action);
      setError(null);
      try {
        setView(await call());
        if (then) setFocus(then);
      } catch (e) {
        showError(e);
      } finally {
        inFlight.current = false;
        setPending(null);
      }
    },
    [],
  );

  function showError(e: unknown) {
    if (e instanceof ApiError) {
      setError({ message: e.message, code: e.code });
      if (e.view) setView(e.view); // always show the backend's current truth
    } else {
      setError({ message: String(e), code: "Error" });
    }
  }

  useEffect(() => {
    // Initial load: state is only set from the async callbacks, never synchronously here.
    let active = true;
    api
      .status()
      .then((v) => active && setView(v))
      .catch((e) => active && showError(e))
      .finally(() => active && setPending(null));
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    if (!focus) return;
    const behavior: ScrollBehavior = prefersReducedMotion() ? "auto" : "smooth";
    if (focus === "top") window.scrollTo({ top: 0, behavior });
    else {
      const target = { routes: routesRef, execute: executeRef, proof: proofRef }[focus].current;
      target?.scrollIntoView({ behavior, block: "start" });
    }
  }, [focus, view]);

  const plan = view?.analysis?.plan;
  const reset = () => run("reset", api.reset, "top");
  const analyse = () => run("analyse", api.analyse, "routes");
  const approve = () => plan && run("approve", () => api.approve(plan.plan_id, plan.plan_hash), "execute");
  const execute = () => plan && run("execute", () => api.execute(plan.plan_id), "proof");

  return (
    <>
      <header className="topbar">
        <div className="brand">
          <span className="wordmark">SikaRescue</span>
          <span className="tagline">
            Agentic failed-payment recovery without double charging the sender.
          </span>
        </div>
        <div className="topbar-actions">
          {view && <StatusBadge state={view.state} pending={pending} />}
          <button className="ghost" onClick={reset} disabled={pending !== null || !view}>
            {pending === "reset" ? <Spinner label="Resetting" /> : "Reset demo"}
          </button>
        </div>
      </header>

      {error && (
        <div className="error-banner" role="alert">
          <p>
            <strong>{error.code}</strong> {error.message}
          </p>
          <button className="ghost" onClick={() => run("load", api.status)} disabled={pending !== null}>
            Refresh status
          </button>
        </div>
      )}

      {!view ? (
        <main className="loading">
          {pending === "load" ? <Spinner label="Loading SK-10421" /> : "No data from the API yet."}
        </main>
      ) : (
        <main className="layout">
          <aside className="truth">
            <PaymentSummary view={view} />
            <Journey view={view} />
            <SafetyNote view={view} />
            <p className="synthetic">Synthetic demo data: no real money, rails, providers or people.</p>
          </aside>

          <div className="workflow">
            <Stepper next={view.next_action} />
            {view.notice && (
              <p className="flow-notice" role="status">
                {view.notice}
              </p>
            )}
            <AnalyseStep view={view} pending={pending} onAnalyse={analyse} />
            <RouteList view={view} sectionRef={routesRef} />
            <ComputePanel view={view} />
            {view.analysis && <AdviceCard advice={view.analysis.advice} />}
            <PlanApproval view={view} pending={pending} onApprove={approve} sectionRef={null} />
            <ExecutionPanel view={view} pending={pending} onExecute={execute} sectionRef={executeRef} />
            <ProofPanel view={view} pending={pending} onReset={reset} sectionRef={proofRef} />
          </div>
        </main>
      )}
    </>
  );
}
