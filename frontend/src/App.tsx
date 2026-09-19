import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, api } from "./api";
import { FrontierPanel, FundsPositionPanel, RetryPanel } from "./components/ControlPlane";
import { EvidencePanel, ScenarioSwitch } from "./components/Evidence";
import { OutagePanel } from "./components/Outage";
import { Journey, PaymentSummary, SafetyNote } from "./components/Truth";
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
import type { Action, DemoView, OutageView, RecoveryState } from "./types";

type Focus = "evidence" | "frontier" | "routes" | "execute" | "proof" | "top";
type Tab = "payment" | "outage";

const BADGES: Record<RecoveryState, { text: string; tone: string }> = {
  FAILED: { text: "FAILED", tone: "bad" },
  DIAGNOSING: { text: "DIAGNOSING", tone: "busy" },
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
  const busy: Partial<Record<Action, string>> = {
    classify: "CLASSIFYING",
    analyse: "ANALYSING",
    execute: "EXECUTING",
  };
  const text = pending ? busy[pending] : undefined;
  const badge = text ? { text, tone: "busy" } : BADGES[state];
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
  const [outage, setOutage] = useState<OutageView | null>(null);
  // `#outage` opens the fleet view directly (bookmarkable, and handy for recordings).
  const [tab, setTabState] = useState<Tab>(() =>
    window.location.hash === "#outage" ? "outage" : "payment",
  );
  const setTab = (next: Tab) => {
    setTabState(next);
    window.history.replaceState(null, "", next === "outage" ? "#outage" : "#");
  };
  const [pending, setPending] = useState<Action | null>("load");
  const [error, setError] = useState<{ message: string; code: string } | null>(null);
  const [focus, setFocus] = useState<Focus | null>(null);
  const inFlight = useRef(false);
  const evidenceRef = useRef<HTMLElement>(null);
  const frontierRef = useRef<HTMLElement>(null);
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
    Promise.all([api.status(), api.outage()])
      .then(([v, o]) => {
        if (!active) return;
        setView(v);
        setOutage(o);
      })
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
      const refs = {
        evidence: evidenceRef,
        frontier: frontierRef,
        routes: routesRef,
        execute: executeRef,
        proof: proofRef,
      };
      refs[focus].current?.scrollIntoView({ behavior, block: "start" });
    }
  }, [focus, view]);

  const plan = view?.analysis?.plan;
  const reset = () => run("reset", () => api.reset(), "top");
  const switchScenario = (scenario: string) => run("reset", () => api.reset(scenario), "top");
  const classify = () => run("classify", api.classify, "frontier");
  const analyse = () => run("analyse", api.analyse, "routes");
  const approve = () => plan && run("approve", () => api.approve(plan.plan_id, plan.plan_hash), "execute");
  const execute = () => plan && run("execute", () => api.execute(plan.plan_id), "proof");

  async function runOutage() {
    if (inFlight.current) return;
    inFlight.current = true;
    setPending("outage");
    setError(null);
    try {
      setOutage(await api.runOutage());
    } catch (e) {
      showError(e);
    } finally {
      inFlight.current = false;
      setPending(null);
    }
  }

  const showAnalyse = view && (view.frontier.payout_actions_permitted || view.analysis !== null);

  return (
    <>
      <header className="topbar">
        <div className="brand">
          <span className="wordmark">SikaRescue</span>
          <span className="tagline">Recovery control plane for cross-border payouts</span>
        </div>
        <nav className="tabs" aria-label="View">
          <button
            className={`tab${tab === "payment" ? " tab--on" : ""}`}
            aria-pressed={tab === "payment"}
            onClick={() => setTab("payment")}
          >
            Payment recovery
          </button>
          <button
            className={`tab${tab === "outage" ? " tab--on" : ""}`}
            aria-pressed={tab === "outage"}
            onClick={() => setTab("outage")}
          >
            Rail outage
          </button>
        </nav>
        <div className="topbar-actions">
          {view && tab === "payment" && <StatusBadge state={view.state} pending={pending} />}
          {tab === "payment" && (
            <button className="ghost" onClick={reset} disabled={pending !== null || !view}>
              {pending === "reset" ? <Spinner label="Resetting" /> : "Reset demo"}
            </button>
          )}
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

      {tab === "outage" ? (
        <OutagePanel
          outage={outage}
          running={pending === "outage"}
          disabled={pending !== null}
          onRun={runOutage}
        />
      ) : !view ? (
        <main className="loading">
          {pending === "load" ? <Spinner label="Loading SK-10421" /> : "No data from the API yet."}
        </main>
      ) : (
        <main className="layout">
          <aside className="truth">
            <PaymentSummary view={view} />
            <Journey view={view} />
            <FundsPositionPanel view={view} />
            <SafetyNote view={view} />
            <p className="synthetic">Synthetic demo data: no real money, rails, providers or people.</p>
          </aside>

          <div className="workflow">
            <ScenarioSwitch view={view} pending={pending} onSwitch={switchScenario} />
            <Stepper next={view.next_action} />
            {view.notice && (
              <p className="flow-notice" role="status">
                {view.notice}
              </p>
            )}
            <EvidencePanel
              view={view}
              pending={pending}
              onClassify={classify}
              sectionRef={evidenceRef}
            />
            <FrontierPanel view={view} sectionRef={frontierRef} />
            <RetryPanel view={view} />
            {showAnalyse && <AnalyseStep view={view} pending={pending} onAnalyse={analyse} />}
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
