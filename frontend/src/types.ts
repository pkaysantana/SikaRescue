// Mirrors backend/sikarescue/api/views.py. Every value is decided by the backend; the UI only
// renders it (money and percentages arrive pre-formatted).

export type RecoveryState =
  | "FAILED"
  | "DIAGNOSING"
  | "AWAITING_APPROVAL"
  | "APPROVED"
  | "RECOVERY_EXECUTING"
  | "RECOVERED"
  | "RECONCILED"
  | "RECOVERY_FAILED"
  | "MANUAL_REVIEW";

export type NextAction = "analyse" | "approve" | "execute" | "done" | "manual_review";

export interface TransactionSummary {
  scenario_id: string; // the synthetic template, e.g. SK-10421
  payment_instance_id: string; // the authoritative id every key binds to
  send_amount: string;
  payout_amount: string;
  fx_rate: string;
  origin: string;
  destination: string;
  recipient_rail: string;
  recipient_token: string;
}

export interface JourneyLeg {
  label: string;
  rail_id: string;
  rail_name: string;
  status: "SUCCESS" | "FAILED" | "UNKNOWN" | "IN_PROGRESS";
  detail: string | null;
  destination: string;
  is_recovery: boolean;
  funds_here: boolean; // the LAST CONFIRMED funds location
}

export interface Diagnosis {
  state: RecoveryState;
  funds_location: string; // last location proven by the journal
  funds_certainty: "PROVEN" | "UNCERTAIN";
  funds_label: string; // "Funds are here" | "Last confirmed here"
  available_for_automatic_action: boolean;
  uncertainty_reason: string | null;
  funds_at_recipient: boolean;
  sender_debited: boolean;
  sender_debit_count: number;
  recipient_credited: boolean;
  recipient_credit_count: number;
  duplicate_sender_debits: number;
  safe_to_restart_from_origin: boolean;
  failed_leg: string | null;
  outstanding: string | null;
}

export interface StressResult {
  scenario: string;
  success: string;
  within_sla: string;
}

export interface RouteOption {
  rail_id: string;
  rail_name: string;
  eligible: boolean;
  selected: boolean;
  rank: number | null;
  rejection_reasons: string[];
  rejection_details: string[];
  fee: string;
  quoted_arrival_seconds: number;
  quoted_reliability: string;
  simulated_success: string | null;
  within_sla: string | null;
  p95_arrival_seconds: number | null;
  score: string | null;
  stress: StressResult[];
}

export interface ComputeSummary {
  backend: string;
  configured_backend: string;
  fallback_from: string | null;
  fallback_reason: string | null;
  simulated_outcomes: number;
  routes_simulated: number;
  scenarios: string[];
  trials_per_scenario: number;
  parallel_jobs: number;
  elapsed_seconds: number;
  remote_compute_seconds: number | null;
  function_ref: string | null;
  rejected_routes_simulated: number;
}

export interface PlanSummary {
  plan_id: string;
  plan_hash: string;
  plan_hash_short: string;
  bound_revision: number;
  status: string;
  source: string;
  rail_id: string;
  rail_name: string;
  amount: string;
  incremental_fee: string;
  fee_bearer: string;
  quoted_arrival_seconds: number;
  selection_reasons: string[];
  approval_summary: string;
}

export interface AdviceCard {
  orchestrator: "pydantic_ai" | "deterministic" | "deterministic_fallback";
  ai_used: boolean;
  label: string;
  fallback_reason: string | null;
  model: string | null;
  provider_model: string | null;
  gateway_route: string | null;
  via_gateway: boolean;
  trace_id: string | null;
  tool_calls: string[];
  incident_summary: string;
  why_origin_retry_is_unsafe: string;
  recommended_route: string;
  reason_codes: string[];
  stress_summary: string;
  operator_message: string;
}

export interface Analysis {
  routes: RouteOption[];
  compute: ComputeSummary;
  plan: PlanSummary;
  advice: AdviceCard;
}

export interface ApprovalSummary {
  approver: string;
  plan_id: string;
  plan_hash_short: string;
  decided_at: string;
}

export interface ExecutionSummary {
  execution_id: string;
  status: string;
  rail_id: string;
  rail_name: string;
  detail: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface ReconciliationSummary {
  reconciled: boolean;
  checks: { name: string; passed: boolean; detail: string }[];
  sender_debit_count: number;
  recipient_credit_count: number;
  duplicate_sender_debits: number;
  funds_location: string;
}

export interface StateTransition {
  at: string;
  from_state: string;
  to_state: string;
  actor: string;
}

export interface DemoView {
  state: RecoveryState;
  next_action: NextAction;
  notice: string | null;
  configured_compute_backend: string;
  agent_mode: string;
  transaction: TransactionSummary;
  journey: JourneyLeg[];
  diagnosis: Diagnosis;
  analysis: Analysis | null;
  approval: ApprovalSummary | null;
  execution: ExecutionSummary | null;
  reconciliation: ReconciliationSummary | null;
  transitions: StateTransition[];
  payout_calls: number; // provider calls for this payment instance
  telemetry: { enabled: boolean; exporting: boolean; detail: string };
  resets: number;
  retired_instance_ids: string[];
}

export type Action = "load" | "reset" | "analyse" | "approve" | "execute";
