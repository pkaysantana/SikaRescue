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

export type NextAction =
  | "classify"
  | "analyse"
  | "approve"
  | "execute"
  | "done"
  | "manual_review";

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
  status: "SUCCESS" | "FAILED" | "UNKNOWN" | "IN_PROGRESS" | "AWAITING_EVIDENCE";
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

// ------------------------------------------------------------------ provider evidence

export interface EvidenceFields {
  extracted_by: string;
  provider: string;
  provider_code: string | null;
  provider_message: string | null;
  transport_outcome: string;
  acceptance_stage: string;
  explicit_rejection: boolean | null;
  provider_reference: string | null;
  fragments: string[];
  completeness: string;
}

export interface Extraction {
  orchestrator: "pydantic_ai" | "deterministic" | "deterministic_fallback";
  ai_used: boolean;
  label: string;
  model: string | null;
  provider_model: string | null;
  gateway_route: string | null;
  fallback_reason: string | null;
  trace_id: string | null;
  rejected_drafts: number;
  elapsed_seconds: number;
  evidence: EvidenceFields;
}

export interface Check {
  name: string;
  label: string;
  passed: boolean;
  detail: string;
}

export interface Verdict {
  classification: "SUCCEEDED" | "DEFINITIVE_FAILED" | "UNKNOWN";
  recorded_outcome: string;
  checks: Check[];
  requirements: Check[];
  reasons: string[];
  catalog_meaning: string | null;
  evidence_digest_short: string;
}

export interface Incident {
  incident_id: string;
  rail_id: string;
  attempt_id: string;
  request_fully_sent: boolean;
  response_received: boolean;
  http_status: number | null;
  elapsed_ms: number;
  timeout_ms: number;
  raw_payload: string;
  digest_short: string;
  extraction: Extraction | null;
  verdict: Verdict | null;
}

// ------------------------------------------------------------------ derived control plane

export interface FundsPosition {
  amount: string;
  last_confirmed_location: string;
  position_status: "AVAILABLE" | "IN_FLIGHT" | "UNCERTAIN" | "FINAL";
  certainty: "PROVEN" | "UNCERTAIN";
  available_for_automatic_action: boolean;
  label: string;
  reason: string | null;
  derived_from: string[];
}

export interface EffectNode {
  node_id: string;
  kind: string;
  label: string;
  status: string;
  source: string;
  destination: string;
  source_amount: string | null;
  destination_amount: string | null;
  rail_name: string | null;
  attempts: string[];
  outstanding: string | null;
  depends_on: string[];
}

export interface FrontierAction {
  action_id: string;
  kind: string;
  label: string;
  rail_name: string | null;
  moves_value: boolean;
  eligible: boolean | null;
  rejection_details: string[];
  simulated: boolean;
  simulated_reliability: string | null;
  score: string | null;
  rank: number | null;
  selected: boolean;
  implemented: boolean;
  note: string | null;
}

export interface Frontier {
  basis: "PLAN" | "CANDIDATES" | "PENDING_EVIDENCE" | "UNCERTAIN_FUNDS" | "SETTLED";
  payout_actions_permitted: boolean;
  candidates: number;
  rejected_before_simulation: number;
  eligible: number;
  simulated: number;
  selected: number;
  actions: FrontierAction[];
  reason: string;
  plan_id: string | null;
}

export interface Preview {
  plan_id: string;
  valid: boolean;
  invalidation_reasons: string[];
  rows: { label: string; current: string; proposed: string; changed: boolean }[];
  proposed_effect_key: string | null;
  proposed_rail: string | null;
}

export interface CounterfactualStep {
  label: string;
  rail_name: string | null;
  amount: string;
  already_completed: boolean;
  risk: string | null;
}

export interface Counterfactual {
  naive_steps: CounterfactualStep[];
  naive_repeated_effects: number;
  naive_extra_sender_debit: string | null;
  duplicate_recipient_credit_risk: boolean;
  sikarescue_steps: CounterfactualStep[];
  sikarescue_action: string;
}

export interface DemoView {
  state: RecoveryState;
  next_action: NextAction;
  notice: string | null;
  scenario: { current: string | null; options: { id: string; label: string }[] };
  incident: Incident | null;
  funds_position: FundsPosition;
  effect_graph: EffectNode[];
  frontier: Frontier;
  preview: Preview | null;
  counterfactual: Counterfactual;
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

export type Action =
  | "load"
  | "reset"
  | "classify"
  | "analyse"
  | "approve"
  | "execute"
  | "outage";

// ------------------------------------------------------------------ systemic outage

export interface OutageAllocation {
  rail_id: string;
  rail_name: string;
  obligations: number;
  amount: string;
  liquidity: string;
  liquidity_utilisation: number;
  liquidity_utilisation_text: string;
  capacity: number;
  capacity_utilisation: number;
  capacity_utilisation_text: string;
  incremental_fee: string;
}

export interface OutageRail {
  rail_id: string;
  rail_name: string;
  status: string;
  eligible: boolean;
  rejection_reasons: string[];
  policy_detail: string;
  liquidity: string;
  capacity: number;
  fee: string;
}

export interface OutageScenario {
  scenario_id: string;
  label: string;
  description: string;
  recoverable_obligations: number;
  recoverable_share: string;
  recoverable_amount: string;
  unserved_obligations: number;
  unserved_amount: string;
  unserved: { reason: string; label: string; obligations: number; amount: string }[];
  allocations: OutageAllocation[];
  rails: OutageRail[];
  aggregate_incremental_fee: string;
  compute_seconds: number;
  checks: string[];
}

export interface OutageView {
  failed_rail: string;
  corridor: string;
  portfolio: {
    size: number;
    seed: number;
    total_amount: string;
    mobile_money: number;
    bank_account: number;
    over_policy_limit: number;
    oldest_minutes: number;
    digest_short: string;
  };
  scenarios: OutageScenario[];
  backend: string;
  configured_backend: string;
  fallback_from: string | null;
  fallback_reason: string | null;
  parallel_jobs: number;
  wall_seconds: number;
  compute_wall_seconds: number;
  kernel_seconds: number;
  verification_seconds: number;
  function_ref: string | null;
  generated_at: string;
}
