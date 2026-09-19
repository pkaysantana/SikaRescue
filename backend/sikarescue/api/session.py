"""One in-memory demo session for SK-10421: SINGLE PROCESS, SINGLE WORKER ONLY.

The repository, journal and asyncio locks live in this process, exactly as in the CLI. Every
operation calls the existing deterministic services; this module only sequences them.

Reliability rules for live demos:
  * one session lock serialises every mutating operation, so a double-click waits for the
    first request and then observes its result instead of repeating it;
  * analyse / approve / execute are idempotent for the plan currently on screen;
  * reset rebuilds the world from the seed (fresh journal, repository and liquidity), so it
    can never duplicate a journal effect; the compute backend is kept so Modal stays warm.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from sikarescue.agent.advisor import AdvisorConfig, AdvisoryOutcome, RecoveryAdvisor
from sikarescue.api.views import DemoView, build_view
from sikarescue.compute.backend import RouteComputeBackend, build_compute_backend
from sikarescue.compute.scenarios import workload_config
from sikarescue.config import Settings
from sikarescue.demo_data.sk10421 import TRANSACTION_ID, DemoWorld, build_demo_world
from sikarescue.errors import IllegalTransitionError
from sikarescue.models import ExecutionStatus, RecoveryState

APPROVER = "demo.operator"

WorldFactory = Callable[[], DemoWorld]
AdvisorFactory = Callable[[DemoWorld], RecoveryAdvisor]


class DemoSession:
    def __init__(
        self,
        settings: Settings,
        *,
        world_factory: WorldFactory | None = None,
        advisor_factory: AdvisorFactory | None = None,
    ) -> None:
        self.settings = settings
        self._compute: RouteComputeBackend | None = None
        self._world_factory = world_factory or self._seeded_world
        self._advisor_factory = advisor_factory or self._configured_advisor
        self._lock = asyncio.Lock()
        self._world: DemoWorld | None = None
        self._advisory: AdvisoryOutcome | None = None
        self.resets = 0

    # ------------------------------------------------------------------ construction

    def _seeded_world(self) -> DemoWorld:
        settings = self.settings
        if self._compute is None:  # reused across resets: keeps the Modal client warm
            self._compute = build_compute_backend(
                settings.compute_backend,
                remote_timeout_seconds=settings.modal_timeout_seconds,
                shards_per_scenario=settings.modal_shards_per_scenario,
            )
        return build_demo_world(
            payout_latency_seconds=settings.payout_latency_seconds,
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

    @property
    def world(self) -> DemoWorld:
        if self._world is None:
            self._world = self._world_factory()
        return self._world

    @property
    def agent_mode(self) -> str:
        return self.settings.agent_mode

    def view(self) -> DemoView:
        return build_view(
            self.world, self._advisory, agent_mode=self.agent_mode, resets=self.resets
        )

    # ------------------------------------------------------------------ operations

    async def reset(self) -> DemoView:
        async with self._lock:
            self._world = self._world_factory()
            self._advisory = None
            self.resets += 1
            return self.view()

    async def analyse(self) -> DemoView:
        async with self._lock:
            state = self.world.repository.get(TRANSACTION_ID).state
            if self._advisory is not None and state in (
                RecoveryState.AWAITING_APPROVAL,
                RecoveryState.APPROVED,
            ):
                current = self.world.repository.get(TRANSACTION_ID).current_plan_id
                if current == self._advisory.plan.plan_id:
                    return self.view()  # already analysed: same immutable plan
            if state not in (RecoveryState.FAILED, RecoveryState.DIAGNOSING):
                raise IllegalTransitionError(
                    f"cannot analyse while {state}; reset the demo to run it again"
                )
            advisor = self._advisor_factory(self.world)
            self._advisory = await advisor.advise(TRANSACTION_ID)
            return self.view()

    async def approve(self, plan_id: str, plan_hash: str) -> DemoView:
        async with self._lock:
            await self.world.service.approve_recovery(
                plan_id, plan_hash=plan_hash, approver=APPROVER
            )  # idempotent for the same plan + hash; refuses any other plan
            return self.view()

    async def execute(self, plan_id: str) -> DemoView:
        async with self._lock:
            aggregate = self.world.repository.get(TRANSACTION_ID)
            if aggregate.state is not RecoveryState.RECONCILED:
                execution = await self.world.service.execute_recovery(plan_id)
                if execution.status is ExecutionStatus.SUCCEEDED:
                    await self.world.service.reconcile_transaction(TRANSACTION_ID)
            return self.view()
