"""The compute-backend contract: what goes in, what comes out.

Plain Pydantic models so requests/results can cross a process boundary (Phase 4: Modal)
unchanged. The request is self-contained: no repository access is needed to evaluate it.
"""

from __future__ import annotations

from pydantic import model_validator

from sikarescue.models.common import DomainModel, EntityId, TransactionId
from sikarescue.models.routes import CandidateRecoveryRoute, RouteEvaluation
from sikarescue.models.simulation import ComputeRunSummary, SimulationConfig


class RouteEvaluationRequest(DomainModel):
    request_id: EntityId
    transaction_id: TransactionId
    candidates: tuple[CandidateRecoveryRoute, ...]
    config: SimulationConfig


class RouteEvaluationBatch(DomainModel):
    request_id: EntityId
    evaluations: tuple[RouteEvaluation, ...]
    summary: ComputeRunSummary

    @model_validator(mode="after")
    def _trial_count_matches(self) -> RouteEvaluationBatch:
        counted = sum(r.simulation_count for e in self.evaluations for r in e.scenario_results)
        if counted != self.summary.simulated_trials:
            raise ValueError("summary.simulated_trials does not match the evaluations")
        return self
