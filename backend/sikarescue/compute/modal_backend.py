"""ModalRouteComputeBackend: fan survivor simulations out across Modal containers.

Hard filters run HERE, in-process, before anything is sent; only routes that passed every
hard constraint are simulated remotely. Scores and ranks are assembled locally from the
merged statistics, and the recovery service checks the whole batch again afterwards
(structure, eligibility, money, recomputed scores and ranks; not the statistics themselves).
`modal` is imported lazily so the local path never depends on it.
"""

from __future__ import annotations

import time
from typing import Any

from sikarescue.compute.backend import RouteComputeBackend
from sikarescue.compute.evaluation import assemble_evaluations, split_candidates
from sikarescue.compute.shards import (
    MODAL_APP_NAME,
    MODAL_FUNCTION_NAME,
    merge_shards,
    plan_shards,
)
from sikarescue.models import ComputeRunSummary, RouteEvaluationBatch, RouteEvaluationRequest

DEFAULT_SHARDS_PER_SCENARIO = 2


class ModalRouteComputeBackend(RouteComputeBackend):
    name = "modal"

    def __init__(
        self,
        *,
        shards_per_scenario: int = DEFAULT_SHARDS_PER_SCENARIO,
        app_name: str = MODAL_APP_NAME,
        function_name: str = MODAL_FUNCTION_NAME,
        remote_function: Any = None,  # injectable for tests; anything with `.map.aio`
    ) -> None:
        if shards_per_scenario < 1:
            raise ValueError("shards_per_scenario must be >= 1")
        self.shards_per_scenario = shards_per_scenario
        self.function_ref = f"{app_name}/{function_name}"
        self._app_name = app_name
        self._function_name = function_name
        self._remote = remote_function

    def _function(self) -> Any:
        if self._remote is None:
            import modal  # lazy: only needed when Modal is actually used

            self._remote = modal.Function.from_name(self._app_name, self._function_name)
        return self._remote

    async def evaluate(self, request: RouteEvaluationRequest) -> RouteEvaluationBatch:
        started = time.perf_counter()
        survivors, rejected = split_candidates(request.candidates, self.name)
        specs = plan_shards(survivors, request.config, self.shards_per_scenario)
        raw_results: list[object] = []
        if specs:
            payloads = [spec.model_dump(mode="json") for spec in specs]
            async for raw in self._function().map.aio(payloads, order_outputs=True):
                raw_results.append(raw)
        results_by_route, remote_seconds = merge_shards(specs, raw_results, request.config)
        evaluations = assemble_evaluations(survivors, rejected, results_by_route, self.name)
        return RouteEvaluationBatch(
            request_id=request.request_id,
            evaluations=evaluations,
            summary=ComputeRunSummary(
                backend=self.name,
                seed=request.config.seed,
                trials_per_scenario=request.config.trials_per_scenario,
                scenarios=tuple(s.scenario_id for s in request.config.scenarios),
                routes_simulated=len(survivors),
                simulated_trials=sum(spec.stop - spec.start for spec in specs),
                elapsed_seconds=round(time.perf_counter() - started, 4),
                shards_per_scenario=self.shards_per_scenario,
                parallel_jobs=len(specs),
                remote_compute_seconds=remote_seconds,
                function_ref=self.function_ref,
            ),
        )
