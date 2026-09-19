"""Enumerations shared across the SikaRescue domain."""

from enum import StrEnum


class Currency(StrEnum):
    GBP = "GBP"
    GHS = "GHS"


class CountryCode(StrEnum):
    GB = "GB"
    GH = "GH"


class EndpointType(StrEnum):
    MOBILE_MONEY = "MOBILE_MONEY"
    BANK_ACCOUNT = "BANK_ACCOUNT"


class RailId(StrEnum):
    # Original route legs
    UK_BANK_DEBIT = "UK_BANK_DEBIT"
    GBP_GHS_FX = "GBP_GHS_FX"
    GH_SETTLEMENT = "GH_SETTLEMENT"
    # Payout rails (original + synthetic alternatives)
    MOMO_A = "MOMO_A"
    MOMO_B = "MOMO_B"
    BANK_MOMO_BRIDGE = "BANK_MOMO_BRIDGE"
    TOKEN_BRIDGE = "TOKEN_BRIDGE"


class RailType(StrEnum):
    BANK_DEBIT = "BANK_DEBIT"
    FX = "FX"
    SETTLEMENT = "SETTLEMENT"
    MOBILE_MONEY_PAYOUT = "MOBILE_MONEY_PAYOUT"
    BANK_TO_MOMO_BRIDGE = "BANK_TO_MOMO_BRIDGE"
    TOKEN_BRIDGE = "TOKEN_BRIDGE"


PAYOUT_RAIL_TYPES = frozenset(
    {RailType.MOBILE_MONEY_PAYOUT, RailType.BANK_TO_MOMO_BRIDGE, RailType.TOKEN_BRIDGE}
)


class RailStatus(StrEnum):
    UP = "UP"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"


class FundsLocation(StrEnum):
    """Where the value of the transaction currently sits (synthetic accounts)."""

    SENDER_ACCOUNT = "SENDER_ACCOUNT"
    UK_COLLECTION_ACCOUNT = "UK_COLLECTION_ACCOUNT"
    GHS_FX_POOL = "GHS_FX_POOL"
    GH_SETTLEMENT_ACCOUNT = "GH_SETTLEMENT_ACCOUNT"
    RECIPIENT_ENDPOINT = "RECIPIENT_ENDPOINT"


class FundsCertainty(StrEnum):
    """How sure we are that the funds are still where the journal last proved them to be."""

    PROVEN = "PROVEN"  # no payout in flight and no UNKNOWN outcome: the journal is the truth
    UNCERTAIN = "UNCERTAIN"  # a payout is in flight or UNKNOWN: value may already have moved


class OperationType(StrEnum):
    """Logical value-moving operations. Each succeeds at most once per transaction."""

    SENDER_DEBIT = "SENDER_DEBIT"
    FX_CONVERSION = "FX_CONVERSION"
    GH_SETTLEMENT = "GH_SETTLEMENT"
    RECIPIENT_CREDIT = "RECIPIENT_CREDIT"


# Transaction-wide effect-key suffixes: `{txn}:{suffix}`.
EFFECT_KEY_SUFFIX: dict[OperationType, str] = {
    OperationType.SENDER_DEBIT: "sender_debit",
    OperationType.FX_CONVERSION: "fx",
    OperationType.GH_SETTLEMENT: "gh_settlement",
    OperationType.RECIPIENT_CREDIT: "recipient_credit",
}

# The corridor's value chain: operation -> (source, destination).
OPERATION_FLOW: dict[OperationType, tuple[FundsLocation, FundsLocation]] = {
    OperationType.SENDER_DEBIT: (FundsLocation.SENDER_ACCOUNT, FundsLocation.UK_COLLECTION_ACCOUNT),
    OperationType.FX_CONVERSION: (FundsLocation.UK_COLLECTION_ACCOUNT, FundsLocation.GHS_FX_POOL),
    OperationType.GH_SETTLEMENT: (FundsLocation.GHS_FX_POOL, FundsLocation.GH_SETTLEMENT_ACCOUNT),
    OperationType.RECIPIENT_CREDIT: (
        FundsLocation.GH_SETTLEMENT_ACCOUNT,
        FundsLocation.RECIPIENT_ENDPOINT,
    ),
}


class AttemptOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    # Provider definitively rejected the request; no value moved.
    DEFINITIVE_FAILED = "DEFINITIVE_FAILED"
    # We cannot tell whether value moved (e.g. timeout after acceptance).
    UNKNOWN = "UNKNOWN"


class FailureStage(StrEnum):
    PRE_ACCEPTANCE = "PRE_ACCEPTANCE"
    POST_ACCEPTANCE = "POST_ACCEPTANCE"
    UNDETERMINED = "UNDETERMINED"


class SettlementLegStatus(StrEnum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    IN_PROGRESS = "IN_PROGRESS"


class RecoveryState(StrEnum):
    FAILED = "FAILED"
    DIAGNOSING = "DIAGNOSING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    RECOVERY_EXECUTING = "RECOVERY_EXECUTING"
    RECOVERED = "RECOVERED"
    RECONCILED = "RECONCILED"
    RECOVERY_FAILED = "RECOVERY_FAILED"
    MANUAL_REVIEW = "MANUAL_REVIEW"


class RejectionReason(StrEnum):
    POLICY_DENIED = "POLICY_DENIED"
    RAIL_UNAVAILABLE = "RAIL_UNAVAILABLE"
    RECIPIENT_INCOMPATIBLE = "RECIPIENT_INCOMPATIBLE"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    FAILED_EARLIER_FOR_TRANSACTION = "FAILED_EARLIER_FOR_TRANSACTION"


class AdviceReasonCode(StrEnum):
    """Why a recommendation is safe. Derived by code from the plan; the advisor may only cite."""

    SENDER_ALREADY_DEBITED = "SENDER_ALREADY_DEBITED"
    FUNDS_HELD_MID_ROUTE = "FUNDS_HELD_MID_ROUTE"
    ORIGINAL_PAYOUT_DEFINITIVELY_FAILED = "ORIGINAL_PAYOUT_DEFINITIVELY_FAILED"
    ONLY_OUTSTANDING_LEG_EXECUTED = "ONLY_OUTSTANDING_LEG_EXECUTED"
    POLICY_PERMITTED = "POLICY_PERMITTED"
    RECIPIENT_COMPATIBLE = "RECIPIENT_COMPATIBLE"
    LIQUIDITY_SUFFICIENT = "LIQUIDITY_SUFFICIENT"
    RAIL_AVAILABLE = "RAIL_AVAILABLE"
    LOWEST_INCREMENTAL_COST = "LOWEST_INCREMENTAL_COST"
    HIGHEST_SIMULATED_SCORE = "HIGHEST_SIMULATED_SCORE"
    STRESS_TESTED = "STRESS_TESTED"
    FEE_ABSORBED_BY_OPERATOR = "FEE_ABSORBED_BY_OPERATOR"
    HUMAN_APPROVAL_REQUIRED = "HUMAN_APPROVAL_REQUIRED"


class ScenarioId(StrEnum):
    """Synthetic operating regimes for route reliability simulation."""

    NORMAL = "NORMAL"
    CONGESTION = "CONGESTION"
    RAIL_DEGRADATION = "RAIL_DEGRADATION"
    RAIL_OUTAGE = "RAIL_OUTAGE"
    LATENCY_SPIKE = "LATENCY_SPIKE"
    REGIONAL_DISRUPTION = "REGIONAL_DISRUPTION"
    CORRELATED_FAILURE = "CORRELATED_FAILURE"


class HardConstraintStatus(StrEnum):
    PASSED = "PASSED"
    REJECTED = "REJECTED"


class PlanStatus(StrEnum):
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    STALE = "STALE"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


class ExecutionStatus(StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


class FeeBearer(StrEnum):
    # Recovery fees are absorbed by the operator; the sender is never charged again.
    OPERATOR = "OPERATOR"


class Actor(StrEnum):
    SYSTEM = "SYSTEM"
    RAIL = "RAIL"
    RECOVERY_ENGINE = "RECOVERY_ENGINE"
    HUMAN = "HUMAN"
    AGENT = "AGENT"


class AuditEventType(StrEnum):
    LEG_SUCCEEDED = "LEG_SUCCEEDED"
    LEG_FAILED = "LEG_FAILED"
    PROVIDER_CALLBACK = "PROVIDER_CALLBACK"
    STATE_TRANSITION = "STATE_TRANSITION"
    DIAGNOSIS_COMPLETED = "DIAGNOSIS_COMPLETED"
    ROUTES_DISCOVERED = "ROUTES_DISCOVERED"
    ROUTES_EVALUATED = "ROUTES_EVALUATED"
    PLANNING_RESULT_DISCARDED = "PLANNING_RESULT_DISCARDED"
    COMPUTE_FALLBACK = "COMPUTE_FALLBACK"
    RECOVERY_PLAN_CREATED = "RECOVERY_PLAN_CREATED"
    RECOVERY_ADVICE_GENERATED = "RECOVERY_ADVICE_GENERATED"
    PLAN_MARKED_STALE = "PLAN_MARKED_STALE"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_GRANTED = "APPROVAL_GRANTED"
    APPROVAL_DENIED = "APPROVAL_DENIED"
    EXECUTION_STARTED = "EXECUTION_STARTED"
    EXECUTION_REPLAYED = "EXECUTION_REPLAYED"
    EXECUTION_SUCCEEDED = "EXECUTION_SUCCEEDED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    EXECUTION_OUTCOME_UNKNOWN = "EXECUTION_OUTCOME_UNKNOWN"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    RECONCILIATION_FAILED = "RECONCILIATION_FAILED"
    RECONCILED = "RECONCILED"
