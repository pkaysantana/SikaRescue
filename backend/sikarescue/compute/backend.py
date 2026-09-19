"""Route compute backends. The recovery service depends only on `RouteComputeBackend`.

Contract every backend honours:
  * input:  `RouteEvaluationRequest` - self-contained candidates + `SimulationConfig`;
  * output: `RouteEvaluationBatch`   - one `RouteEvaluation` per candidate + run summary;
  * semantics of `compute.evaluation` (hard filters before simulation; rejected routes are
    never simulated, scored or ranked);
  * deterministic for a given request (seeded, counter-based simulation).
The recovery service independently re-verifies eligibility, money, scores and ranking of
every batch before it can influence a plan, so a backend can only contribute statistics.

Implementations: `LocalRouteComputeBackend` (in-process) and
`compute.modal_backend.ModalRouteComputeBackend` (Modal fan-out), which is always wrapped in
`FallbackComputeBackend` so Modal can never become a single point of failure.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod

from sikarescue.compute.evaluation import evaluate_candidates
from sikarescue.compute.verification import verify_evaluations
from sikarescue.errors import ComputeIntegrityError, ConfigurationError
from sikarescue.models import ComputeRunSummary, RouteEvaluationBatch, RouteEvaluationRequest

logger = logging.getLogger(__name__)

DEFAULT_REMOTE_TIMEOUT_SECONDS = 60.0


class RouteComputeBackend(ABC):
    name: str

    @abstractmethod
    async def evaluate(self, request: RouteEvaluationRequest) -> RouteEvaluationBatch:
        """Evaluate every candidate in the request."""


def evaluate_request(request: RouteEvaluationRequest, backend_name: str) -> RouteEvaluationBatch:
    """Synchronous in-process reference implementation."""
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


def describe_failure(exc: BaseException, timeout_seconds: float) -> str:
    """Short, secret-free description of why a remote backend failed."""
    if isinstance(exc, TimeoutError) and not str(exc):
        return f"TimeoutError: no result within {timeout_seconds:g}s"
    message = " ".join(str(exc).split())[:200] or "no details"
    return f"{type(exc).__name__}: {message}"[:280]


class FallbackComputeBackend(RouteComputeBackend):
    """Run `primary` with a timeout; on ANY failure evaluate with `fallback` and say so.

    Failures include timeouts, authentication/lookup errors, remote exceptions and malformed
    or inconsistent results (the batch is verified here before being accepted). The returned
    summary then names the backend that actually produced the statistics, plus
    `fallback_from` / `fallback_reason`, so the fallback is never silent.
    """

    def __init__(
        self,
        primary: RouteComputeBackend,
        fallback: RouteComputeBackend,
        *,
        timeout_seconds: float = DEFAULT_REMOTE_TIMEOUT_SECONDS,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.timeout_seconds = timeout_seconds
        self.name = primary.name  # the configured backend
        self.last_failure: str | None = None

    async def evaluate(self, request: RouteEvaluationRequest) -> RouteEvaluationBatch:
        try:
            batch = await asyncio.wait_for(
                self.primary.evaluate(request), timeout=self.timeout_seconds
            )
            problems = verify_evaluations(request.candidates, batch.evaluations, request.config)
            if batch.request_id != request.request_id:
                problems.append("batch answers a different request")
            if problems:
                raise ComputeIntegrityError(problems)
            self.last_failure = None
            return batch
        except Exception as exc:  # CancelledError is not an Exception: cancellation propagates
            reason = describe_failure(exc, self.timeout_seconds)
            self.last_failure = reason
            logger.warning(
                "compute backend %r failed (%s); falling back to %r",
                self.primary.name,
                reason,
                self.fallback.name,
            )
        batch = await self.fallback.evaluate(request)
        summary = ComputeRunSummary.model_validate(
            batch.summary.model_dump()
            | {"fallback_from": self.primary.name, "fallback_reason": reason}
        )
        return batch.model_copy(update={"summary": summary})


def build_compute_backend(
    name: str,
    *,
    remote_timeout_seconds: float = DEFAULT_REMOTE_TIMEOUT_SECONDS,
    shards_per_scenario: int | None = None,
) -> RouteComputeBackend:
    if name == "local":
        return LocalRouteComputeBackend()
    if name == "modal":
        from sikarescue.compute.modal_backend import (
            DEFAULT_SHARDS_PER_SCENARIO,
            ModalRouteComputeBackend,
        )

        modal_backend = ModalRouteComputeBackend(
            shards_per_scenario=shards_per_scenario or DEFAULT_SHARDS_PER_SCENARIO
        )
        return FallbackComputeBackend(
            modal_backend, LocalRouteComputeBackend(), timeout_seconds=remote_timeout_seconds
        )
    raise ConfigurationError(f"unknown compute backend {name!r}; expected 'local' or 'modal'")
