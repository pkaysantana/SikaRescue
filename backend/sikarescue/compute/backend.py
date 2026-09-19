"""Route compute backends. The recovery service depends only on `RouteComputeBackend`.

Contract (what a future ModalRouteComputeBackend must honour):
  * input:  `RouteEvaluationRequest` - self-contained candidates + `SimulationConfig`;
  * output: `RouteEvaluationBatch`   - one `RouteEvaluation` per candidate + run summary;
  * semantics of `compute.evaluation.evaluate_candidates` (hard filters before simulation;
    rejected routes are never simulated, scored or ranked);
  * deterministic for a given request (seeded, counter-based simulation).
The recovery service independently re-verifies eligibility, money, scores and ranking of
every batch before it can influence a plan, so a backend can only contribute statistics.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import ClassVar

from sikarescue.compute.evaluation import evaluate_candidates
from sikarescue.errors import ConfigurationError
from sikarescue.models import ComputeRunSummary, RouteEvaluationBatch, RouteEvaluationRequest


class RouteComputeBackend(ABC):
    name: ClassVar[str]

    @abstractmethod
    async def evaluate(self, request: RouteEvaluationRequest) -> RouteEvaluationBatch:
        """Evaluate every candidate in the request."""


def evaluate_request(request: RouteEvaluationRequest, backend_name: str) -> RouteEvaluationBatch:
    """Synchronous reference implementation shared by backends."""
    started = time.perf_counter()
    evaluations = evaluate_candidates(request.candidates, request.config, backend_name)
    elapsed = time.perf_counter() - started
    simulated = [e for e in evaluations if e.scenario_results]
    return RouteEvaluationBatch(
        request_id=request.request_id,
        evaluations=evaluations,
        summary=ComputeRunSummary(
            backend=backend_name,
            seed=request.config.seed,
            trials_per_scenario=request.config.trials_per_scenario,
            scenarios=tuple(s.scenario_id for s in request.config.scenarios),
            routes_simulated=len(simulated),
            simulated_trials=sum(r.simulation_count for e in simulated for r in e.scenario_results),
            elapsed_seconds=round(elapsed, 4),
        ),
    )


class LocalRouteComputeBackend(RouteComputeBackend):
    """In-process CPU evaluation. Runs in a worker thread so the event loop stays responsive."""

    name = "local"

    async def evaluate(self, request: RouteEvaluationRequest) -> RouteEvaluationBatch:
        return await asyncio.to_thread(evaluate_request, request, self.name)


def build_compute_backend(name: str) -> RouteComputeBackend:
    if name == "local":
        return LocalRouteComputeBackend()
    if name == "modal":
        raise ConfigurationError(
            "SIKARESCUE_COMPUTE_BACKEND=modal is not available yet (Modal backend arrives "
            "in Phase 4); use SIKARESCUE_COMPUTE_BACKEND=local"
        )
    raise ConfigurationError(f"unknown compute backend {name!r}; expected 'local' or 'modal'")
