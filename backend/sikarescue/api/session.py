"""One in-memory demo session for scenario SK-10421: SINGLE PROCESS, SINGLE WORKER ONLY.

The repository, journal and asyncio locks live in this process, exactly as in the CLI. Every
operation calls the existing deterministic services; this module only sequences them.

Reliability rules for live demos:
  * one session lock serialises every mutating operation, so a double-click waits for the
    first request and then observes its result instead of repeating it;
  * analyse / approve / execute are idempotent for the plan currently on screen;
  * each run of the scenario is a unique payment INSTANCE (`SK-10421-<8 hex>`). The simulated
    payout provider outlives resets, just as a real provider would remember every key it saw.
    A reset after any payout was dispatched RETIRES the instance and starts a new one, so an
    instance whose keys reached the provider is never erased and replayed. A reset before any
    dispatch rebuilds the same instance (nothing external ever saw it);
  * the compute backend is kept across resets so Modal stays warm.

Each instance starts from a synthetic MOMO_A provider incident (DEFINITIVE or UNKNOWN). The
first step classifies it: Pydantic AI extracts FailureEvidence from the raw payload and the
deterministic verifier decides the outcome. Switching scenario is a reset into a new instance,
never a mutation of the current one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from sikarescue.agent.advisor import AdvisorConfig, AdvisoryOutcome, RecoveryAdvisor
from sikarescue.agent.evidence import EvidenceExtraction, EvidenceExtractor
from sikarescue.api.control_views import OutageView, build_outage_view
from sikarescue.api.views import DemoView, build_view
from sikarescue.compute.backend import RouteComputeBackend, build_compute_backend
from sikarescue.compute.scenarios import workload_config
from sikarescue.config import Settings
from sikarescue.demo_data.incidents import IncidentScenario
from sikarescue.demo_data.outage import build_outage_analyzer
from sikarescue.demo_data.sk10421 import DemoWorld, build_demo_world, new_instance_id
from sikarescue.errors import IdempotencyConflictError, IllegalTransitionError
from sikarescue.models import ExecutionStatus, RecoveryState
from sikarescue.models.outage import OutageAnalysis
from sikarescue.services.payout_gateway import SimulatedPayoutGateway

APPROVER = "demo.operator"  # an unauthenticated demo operator: there is no login or RBAC

# (payment instance id, the session's long-lived payout provider) -> a seeded world
WorldFactory = Callable[[str, SimulatedPayoutGateway], DemoWorld]
AdvisorFactory = Callable[[DemoWorld], RecoveryAdvisor]
ExtractorFactory = Callable[[DemoWorld], EvidenceExtractor]

_ANALYSABLE = frozenset(
    {RecoveryState.FAILED, RecoveryState.DIAGNOSING, RecoveryState.RECOVERY_FAILED}
)
_AWAITING = frozenset({RecoveryState.AWAITING_APPROVAL, RecoveryState.APPROVED})


class DemoSession:
    def __init__(
        self,
        settings: Settings,
        *,
        world_factory: WorldFactory | None = None,
        advisor_factory: AdvisorFactory | None = None,
        extractor_factory: ExtractorFactory | None = None,
        scenario: IncidentScenario = IncidentScenario.DEFINITIVE,
    ) -> None:
        self.settings = settings
        self._compute: RouteComputeBackend | None = None
        self._world_factory = world_factory or self._seeded_world
        self._advisor_factory = advisor_factory or self._configured_advisor
        self._extractor_factory = extractor_factory or self._configured_extractor
        self.scenario = scenario
        self._extraction: EvidenceExtraction | None = None
        self._outage: OutageAnalysis | None = None
        self._outage_lock = asyncio.Lock()  # independent of the payment: never blocks it
        self._lock = asyncio.Lock()
        # The external payout provider: it outlives every reset and remembers every key.
        self.gateway = SimulatedPayoutGateway(latency_seconds=settings.payout_latency_seconds)
        self._world: DemoWorld | None = None
        self._advisory: AdvisoryOutcome | None = None
        self.retired_instance_ids: list[str] = []
        self.resets = 0

    # ------------------------------------------------------------------ construction

    def _seeded_world(self, transaction_id: str, gateway: SimulatedPayoutGateway) -> DemoWorld:
        settings = self.settings
        if self._compute is None:  # reused across resets: keeps the Modal client warm
            self._compute = build_compute_backend(
                settings.compute_backend,
                remote_timeout_seconds=settings.modal_timeout_seconds,
                shards_per_scenario=settings.modal_shards_per_scenario,
            )
        return build_demo_world(
            transaction_id=transaction_id,
            gateway=gateway,
            incident=self.scenario,
            payout_timeout_seconds=settings.payout_timeout_seconds,
            compute=self._compute,
            simulation=workload_config(
                settings.effective_workload,
                seed=settings.simulation_seed,
                trials_per_scenario=settings.simulation_trials_per_scenario,
            ),
        )

    def _configured_advisor(self, world: DemoWorld) -> RecoveryAdvisor:
        return RecoveryAdvisor(
            world.service, AdvisorConfig.from_settings(self.settings), settings=self.settings
        )

    def _configured_extractor(self, world: DemoWorld) -> EvidenceExtractor:
        return EvidenceExtractor(AdvisorConfig.from_settings(self.settings), settings=self.settings)

    def _dispatched(self, transaction_id: str) -> bool:
        """Has ANY payout request for this instance ever reached the provider?"""
        return any(r.transaction_id == transaction_id for r in self.gateway.requests)

    def _build_world(self, transaction_id: str) -> DemoWorld:
        # Fail closed: an instance the provider has seen can never be seeded again.
        if transaction_id in self.retired_instance_ids or self._dispatched(transaction_id):
            raise IdempotencyConflictError(
                f"payment instance {transaction_id} already reached the payout provider; "
                "it is retired and can never be reused"
            )
        world = self._world_factory(transaction_id, self.gateway)
        if world.transaction_id != transaction_id or world.gateway is not self.gateway:
            raise IdempotencyConflictError("demo world is not bound to its payment instance")
        return world

    @property
    def world(self) -> DemoWorld:
        if self._world is None:
            self._world = self._build_world(new_instance_id())
        return self._world

    @property
    def transaction_id(self) -> str:
        return self.world.transaction_id

    @property
    def agent_mode(self) -> str:
        return self.settings.agent_mode

    def view(self) -> DemoView:
        return build_view(
            self.world,
            self._advisory,
            agent_mode=self.agent_mode,
            resets=self.resets,
            retired_instance_ids=self.retired_instance_ids,
            scenario=self.scenario,
            extraction=self._extraction,
        )

    # ------------------------------------------------------------------ operations

    async def reset(self, scenario: IncidentScenario | None = None) -> DemoView:
        async with self._lock:
            current = self.world.transaction_id
            aggregate = self.world.repository.get(current)
            target = scenario or self.scenario
            # An instance that reached a provider, or whose outcome is UNKNOWN, is retired:
            # its identity is never erased and replayed.
            if (
                self._dispatched(current)
                or aggregate.executions
                or aggregate.state is RecoveryState.MANUAL_REVIEW
            ):
                self.retired_instance_ids.append(current)
                instance = new_instance_id()
            elif target is not self.scenario:
                instance = new_instance_id()  # a different payment, never a mutated one
            else:
                instance = current  # nothing reached a provider: rebuilding it is safe
            self.scenario = target
            self._world = self._build_world(instance)
            self._advisory = None
            self._extraction = None
            self.resets += 1
            return self.view()

    async def classify(self) -> DemoView:
        """Pydantic AI extracts evidence; the deterministic verifier classifies. Idempotent."""
        async with self._lock:
            aggregate = self.world.repository.get(self.transaction_id)
            if aggregate.incident_classification is not None:
                return self.view()  # write-once: already classified
            incident = aggregate.incident
            if incident is None:
                raise IllegalTransitionError("this payment has no provider incident to classify")
            extractor = self._extractor_factory(self.world)
            extraction = await extractor.extract(incident, aggregate.instruction)
            self._extraction = extraction
            await self.world.service.classify_provider_incident(
                self.transaction_id, extraction.evidence
            )
            return self.view()

    async def analyse(self) -> DemoView:
        async with self._lock:
            aggregate = self.world.repository.get(self.transaction_id)
            state = aggregate.state
            if state in _AWAITING:
                if self._advisory is not None and (
                    aggregate.current_plan_id == self._advisory.plan.plan_id
                ):
                    return self.view()  # already analysed: same immutable plan
                # A stale plan was replaced: explain the replacement, which needs a NEW
                # approval. Planning returns the current plan while it is still fresh.
            elif state not in _ANALYSABLE:
                raise IllegalTransitionError(
                    f"cannot analyse while {state}; reset the demo to run it again"
                )
            advisor = self._advisor_factory(self.world)
            self._advisory = await advisor.advise(self.transaction_id)
            return self.view()

    async def approve(self, plan_id: str, plan_hash: str) -> DemoView:
        async with self._lock:
            await self.world.service.approve_recovery(
                plan_id, plan_hash=plan_hash, approver=APPROVER
            )  # idempotent for the same plan + hash; refuses any other plan
            return self.view()

    async def execute(self, plan_id: str) -> DemoView:
        async with self._lock:
            aggregate = self.world.repository.get(self.transaction_id)
            if aggregate.state is not RecoveryState.RECONCILED:
                execution = await self.world.service.execute_recovery(plan_id)
                if execution.status is ExecutionStatus.SUCCEEDED:
                    await self.world.service.reconcile_transaction(self.transaction_id)
            return self.view()

    # ------------------------------------------------------------------ systemic outage

    def outage_view(self) -> OutageView | None:
        if self._outage is None:
            return None
        names = {r.rail_id: r.display_name.replace("->", "→") for r in self.world.registry.all()}
        return build_outage_view(self._outage, names)

    async def run_outage(self) -> OutageView:
        async with self._outage_lock:
            settings = self.settings
            analyzer = build_outage_analyzer(
                settings.compute_backend, timeout_seconds=settings.outage_timeout_seconds
            )
            self._outage = await analyzer.run()
            view = self.outage_view()
            assert view is not None
            return view
