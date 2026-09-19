"""Runtime configuration from environment variables (prefix `SIKARESCUE_`) or `.env`."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SIKARESCUE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    compute_backend: Literal["local", "modal"] = "local"
    payout_latency_seconds: float = Field(default=0.5, ge=0, le=30)
    # No provider answer within this window after dispatch => outcome UNKNOWN (manual review).
    payout_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    # Synthetic reliability simulation (seeded => reproducible).
    simulation_seed: int = Field(default=10421, ge=0)
    # quick = 3 scenarios x 2,000 trials; stress = 6 scenarios x 50,000 trials.
    # Unset: stress when compute_backend=modal, quick otherwise.
    simulation_workload: Literal["quick", "stress"] | None = None
    simulation_trials_per_scenario: int | None = Field(default=None, ge=100, le=1_000_000)
    # Modal compute (Phase 4). Any failure or timeout falls back to local compute, visibly.
    # ~15 s: comfortably above a measured ~10.5 s cold start, short enough for a live demo.
    modal_timeout_seconds: float = Field(default=15.0, gt=0, le=600)
    modal_shards_per_scenario: int = Field(default=2, ge=1, le=16)

    @property
    def effective_workload(self) -> str:
        if self.simulation_workload:
            return self.simulation_workload
        return "stress" if self.compute_backend == "modal" else "quick"


@lru_cache
def get_settings() -> Settings:
    return Settings()
