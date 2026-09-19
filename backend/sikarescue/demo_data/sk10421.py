"""Seeded SYNTHETIC demo scenario SK-10421: £120 UK -> Ghana mobile money.

Everything here is simulated: rails, quotes, reliabilities, balances, policy and the
recipient's identity. Reliability figures are illustrative, NOT empirical measurements.

Story: sender debit, GBP->GHS FX and Ghana settlement all SUCCEEDED. The final MOMO_A
payout was rejected with a synthetic HTTP 503 BEFORE the provider accepted the request,
so it is DEFINITIVE_FAILED and no value moved: funds sit in GH_SETTLEMENT_ACCOUNT.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import SecretStr

from sikarescue.compute.backend import LocalRouteComputeBackend, RouteComputeBackend
from sikarescue.compute.scenarios import simulation_config
from sikarescue.models import (
    Actor,
    AttemptOutcome,
    AuditEventType,
    CountryCode,
    Currency,
    EndpointType,
    FailureDetail,
    FailureStage,
    FinancialEffect,
    FundsLocation,
    HopProfile,
    Money,
    OperationAttempt,
    OperationType,
    PaymentTransaction,
    Rail,
    RailId,
    RailQuote,
    RailStatus,
    RailType,
    RecipientDetails,
    RouteSimulationProfile,
    SimulationConfig,
    effect_key,
)
from sikarescue.models.enums import OPERATION_FLOW
from sikarescue.services.journal import FinancialJournal
from sikarescue.services.liquidity import LiquidityBook
from sikarescue.services.payout_gateway import SimulatedPayoutGateway
from sikarescue.services.policy import PolicyEngine
from sikarescue.services.rails import RailRegistry
from sikarescue.services.recovery import RecoveryService
from sikarescue.services.repository import InMemoryTransactionRepository, TransactionAggregate

# SK-10421 is the synthetic SCENARIO TEMPLATE. The CLI and tests run it as a single payment
# whose id is the template id. The resettable web demo runs each attempt as a fresh, unique
# payment INSTANCE (`SK-10421-<8 hex>`): authoritative effect, idempotency and execution keys
# all bind to the instance id, so an instance that reached a provider is never replayed.
SCENARIO_ID = "SK-10421"
TRANSACTION_ID = SCENARIO_ID
FX_RATE = Decimal("15.250000")  # synthetic GBP->GHS rate
SEND_AMOUNT = Money.of("120.00", Currency.GBP)
PAYOUT_AMOUNT = Money.of("1830.00", Currency.GHS)
BASE_TIME = datetime(2026, 9, 19, 9, 30, tzinfo=UTC)

# Synthetic recipient. Not a real person, phone number or account.
RECIPIENT_NAME = "Ama Mensah"
RECIPIENT_PHONE = "+233 20 555 0142"
RECIPIENT_REFERENCE = "GH-9821-SYNTH-0042"
RECIPIENT_TOKEN = "rcp_8f3k2m9q"

_SETTLEMENT = FundsLocation.GH_SETTLEMENT_ACCOUNT


def new_instance_id() -> str:
    """A fresh payment-instance id for the SK-10421 scenario, e.g. `SK-10421-3fa9c2d1`."""
    return f"{SCENARIO_ID}-{uuid.uuid4().hex[:8]}"


def build_instruction(transaction_id: str = TRANSACTION_ID) -> PaymentTransaction:
    return PaymentTransaction(
        transaction_id=transaction_id,
        created_at=BASE_TIME,
        origin_country=CountryCode.GB,
        destination_country=CountryCode.GH,
        send_amount=SEND_AMOUNT,
        fx_rate=FX_RATE,
        payout_amount=PAYOUT_AMOUNT,
        recipient=RecipientDetails(
            name=SecretStr(RECIPIENT_NAME),
            phone=SecretStr(RECIPIENT_PHONE),
            account_reference=SecretStr(RECIPIENT_REFERENCE),
            endpoint_type=EndpointType.MOBILE_MONEY,
        ),
        recipient_token=RECIPIENT_TOKEN,
        original_route=(
            RailId.UK_BANK_DEBIT,
            RailId.GBP_GHS_FX,
            RailId.GH_SETTLEMENT,
            RailId.MOMO_A,
        ),
    )


def build_rails() -> list[Rail]:
    momo = frozenset({EndpointType.MOBILE_MONEY})
    momo_or_bank = frozenset({EndpointType.MOBILE_MONEY, EndpointType.BANK_ACCOUNT})

    def rail(
        rail_id: RailId,
        name: str,
        rail_type: RailType,
        *,
        status: RailStatus = RailStatus.UP,
        endpoints: frozenset[EndpointType] = frozenset(),
        hops: int = 1,
        payout: bool = True,
    ) -> Rail:
        return Rail(
            rail_id=rail_id,
            display_name=name,
            rail_type=rail_type,
            status=status,
            supported_endpoints=endpoints,
            dependency_count=hops,
            payout_source=_SETTLEMENT if payout else None,
        )

    return [
        rail(RailId.UK_BANK_DEBIT, "UK bank debit", RailType.BANK_DEBIT, payout=False),
        rail(RailId.GBP_GHS_FX, "GBP->GHS FX", RailType.FX, payout=False),
        rail(RailId.GH_SETTLEMENT, "Ghana settlement", RailType.SETTLEMENT, payout=False),
        rail(RailId.MOMO_A, "MoMo A", RailType.MOBILE_MONEY_PAYOUT,
             status=RailStatus.DOWN, endpoints=momo),
        rail(RailId.MOMO_B, "MoMo B", RailType.MOBILE_MONEY_PAYOUT, endpoints=momo),
        rail(RailId.BANK_MOMO_BRIDGE, "Bank -> MoMo bridge", RailType.BANK_TO_MOMO_BRIDGE,
             endpoints=momo_or_bank, hops=2),
        rail(RailId.TOKEN_BRIDGE, "Token bridge", RailType.TOKEN_BRIDGE, endpoints=momo, hops=2),
    ]  # fmt: skip


def build_quotes() -> list[RailQuote]:
    def q(n: int, rail: RailId, fee: str, latency: int, reliability: float) -> RailQuote:
        return RailQuote(
            quote_id=f"qte_{n:012x}",
            rail_id=rail,
            incremental_fee=Money.of(fee, Currency.GBP),
            expected_latency_seconds=latency,
            quoted_reliability=reliability,
        )

    return [
        q(1, RailId.MOMO_A, "0.00", 60, 0.950),
        q(2, RailId.MOMO_B, "0.18", 74, 0.981),
        q(3, RailId.BANK_MOMO_BRIDGE, "0.42", 41, 0.964),
        # Cheapest, fastest and best-quoted -- but not permitted by corridor policy.
        q(4, RailId.TOKEN_BRIDGE, "0.09", 25, 0.990),
    ]


def build_simulation_profiles() -> list[RouteSimulationProfile]:
    """SYNTHETIC behaviour model per payout rail (one hop per external dependency).

    Tuned so the NORMAL scenario lands near each rail's quoted reliability
    (MOMO_B ~98.1%, BANK_MOMO_BRIDGE ~96.4%). Illustrative only, not measured.
    """

    def hop(name: str, failure: float, median: float, sigma: float = 0.25) -> HopProfile:
        return HopProfile(
            name=name,
            failure_probability=failure,
            median_latency_seconds=median,
            latency_sigma=sigma,
            outage_probability=0.02,
            outage_delay_seconds=45.0,
            retry_failure_probability=0.25,
        )

    return [
        RouteSimulationProfile(rail_id=RailId.MOMO_A, hops=(hop("momo_a_payout", 0.03, 60.0),)),
        RouteSimulationProfile(rail_id=RailId.MOMO_B, hops=(hop("momo_b_payout", 0.014, 70.0),)),
        RouteSimulationProfile(
            rail_id=RailId.BANK_MOMO_BRIDGE,
            hops=(
                hop("gh_bank_transfer", 0.013, 18.0, sigma=0.3),
                hop("bank_to_momo_push", 0.013, 20.0, sigma=0.3),
            ),
        ),
        RouteSimulationProfile(
            rail_id=RailId.TOKEN_BRIDGE,
            hops=(hop("token_mint", 0.005, 10.0), hop("token_offramp", 0.005, 14.0)),
        ),
    ]


def build_liquidity() -> LiquidityBook:
    return LiquidityBook(
        {
            RailId.MOMO_A: Money.of("500000.00", Currency.GHS),
            RailId.MOMO_B: Money.of("250000.00", Currency.GHS),
            RailId.BANK_MOMO_BRIDGE: Money.of("90000.00", Currency.GHS),
            RailId.TOKEN_BRIDGE: Money.of("1000000.00", Currency.GHS),
        }
    )


def build_policy() -> PolicyEngine:
    return PolicyEngine(
        corridor_rail_types={
            "GB->GH": frozenset({RailType.MOBILE_MONEY_PAYOUT, RailType.BANK_TO_MOMO_BRIDGE})
        },
        max_payout=Money.of("10000.00", Currency.GHS),
    )


_MOMO_A_FAILURES = {
    AttemptOutcome.DEFINITIVE_FAILED: FailureDetail(
        stage=FailureStage.PRE_ACCEPTANCE,
        http_status=503,
        provider_code="PROVIDER_UNAVAILABLE",
        message="Synthetic MOMO_A 503: request rejected before acceptance; no value moved.",
    ),
    AttemptOutcome.UNKNOWN: FailureDetail(
        stage=FailureStage.UNDETERMINED,
        http_status=504,
        provider_code="GATEWAY_TIMEOUT",
        message="Synthetic MOMO_A timeout after submission; outcome unknown.",
    ),
}


def seed_transaction(
    momo_a_outcome: AttemptOutcome = AttemptOutcome.DEFINITIVE_FAILED,
    *,
    transaction_id: str = TRANSACTION_ID,
) -> TransactionAggregate:
    """Build the aggregate with journal evidence for the three successful legs + MOMO_A."""
    if momo_a_outcome is AttemptOutcome.SUCCEEDED:
        raise ValueError("the demo scenario requires a failed MOMO_A payout")
    instruction = build_instruction(transaction_id)
    journal = FinancialJournal(transaction_id, principal=SEND_AMOUNT)
    aggregate = TransactionAggregate(instruction=instruction, journal=journal)

    upstream = [
        (OperationType.SENDER_DEBIT, RailId.UK_BANK_DEBIT, SEND_AMOUNT, SEND_AMOUNT, None),
        (OperationType.FX_CONVERSION, RailId.GBP_GHS_FX, SEND_AMOUNT, PAYOUT_AMOUNT, FX_RATE),
        (OperationType.GH_SETTLEMENT, RailId.GH_SETTLEMENT, PAYOUT_AMOUNT, PAYOUT_AMOUNT, None),
    ]
    for n, (op, rail, source_amount, dest_amount, rate) in enumerate(upstream, start=1):
        at = BASE_TIME + timedelta(seconds=20 * n)
        source, destination = OPERATION_FLOW[op]
        attempt = OperationAttempt(
            attempt_id=f"att_{n:012x}",
            transaction_id=transaction_id,
            operation=op,
            rail_id=rail,
            source=source,
            destination=destination,
            amount=source_amount,
            outcome=AttemptOutcome.SUCCEEDED,
            idempotency_key=effect_key(transaction_id, op),
            provider_reference=f"prv_seed{n:08d}",
            started_at=at,
            completed_at=at + timedelta(seconds=5),
        )
        journal.record_attempt(attempt)
        journal.post_effect(
            FinancialEffect(
                effect_key=effect_key(transaction_id, op),
                transaction_id=transaction_id,
                operation=op,
                rail_id=rail,
                attempt_id=attempt.attempt_id,
                source=source,
                destination=destination,
                source_amount=source_amount,
                destination_amount=dest_amount,
                fx_rate=rate,
                posted_at=at + timedelta(seconds=5),
            )
        )
        aggregate.record_audit(
            AuditEventType.LEG_SUCCEEDED,
            Actor.RAIL,
            f"{rail} succeeded: {source} -> {destination} ({dest_amount})",
            rail_id=rail.value,
        )

    at = BASE_TIME + timedelta(seconds=80)
    source, destination = OPERATION_FLOW[OperationType.RECIPIENT_CREDIT]
    failure = _MOMO_A_FAILURES[momo_a_outcome]
    journal.record_attempt(
        OperationAttempt(
            attempt_id=f"att_{4:012x}",
            transaction_id=transaction_id,
            operation=OperationType.RECIPIENT_CREDIT,
            rail_id=RailId.MOMO_A,
            source=source,
            destination=destination,
            amount=PAYOUT_AMOUNT,
            outcome=momo_a_outcome,
            idempotency_key=f"{transaction_id}:original:MOMO_A:payout",
            failure=failure,
            started_at=at,
            completed_at=at + timedelta(seconds=2),
        )
    )
    aggregate.record_audit(
        AuditEventType.LEG_FAILED,
        Actor.RAIL,
        f"MOMO_A payout {momo_a_outcome}: HTTP {failure.http_status} "
        f"({failure.stage.value.lower().replace('_', '-')})",
        rail_id=RailId.MOMO_A.value,
        outcome=momo_a_outcome.value,
        http_status=failure.http_status,
    )
    return aggregate


@dataclass
class DemoWorld:
    transaction_id: str  # the authoritative payment instance id
    repository: InMemoryTransactionRepository
    registry: RailRegistry
    policy: PolicyEngine
    liquidity: LiquidityBook
    gateway: SimulatedPayoutGateway
    service: RecoveryService


def build_demo_world(
    *,
    payout_latency_seconds: float = 0.0,
    momo_a_outcome: AttemptOutcome = AttemptOutcome.DEFINITIVE_FAILED,
    compute: RouteComputeBackend | None = None,
    simulation: SimulationConfig | None = None,
    payout_timeout_seconds: float = 30.0,
    transaction_id: str = TRANSACTION_ID,
    gateway: SimulatedPayoutGateway | None = None,
) -> DemoWorld:
    """A seeded world for ONE payment instance.

    Pass a shared `gateway` to model a provider that outlives the world: it remembers every
    idempotency key it has seen, exactly as an external payout provider would.
    """
    repository = InMemoryTransactionRepository()
    repository.add(seed_transaction(momo_a_outcome, transaction_id=transaction_id))
    registry = RailRegistry(build_rails(), build_quotes(), build_simulation_profiles())
    policy = build_policy()
    liquidity = build_liquidity()
    if gateway is None:
        gateway = SimulatedPayoutGateway(latency_seconds=payout_latency_seconds)
    service = RecoveryService(
        repository,
        registry,
        policy,
        liquidity,
        gateway,
        compute=compute or LocalRouteComputeBackend(),
        simulation_config=simulation or simulation_config(),
        payout_timeout_seconds=payout_timeout_seconds,
    )
    return DemoWorld(transaction_id, repository, registry, policy, liquidity, gateway, service)
