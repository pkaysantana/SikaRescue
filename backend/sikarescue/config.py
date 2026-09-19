"""Runtime configuration from environment variables (prefix `SIKARESCUE_`) or `.env`.

Third-party credentials keep their vendors' own unprefixed names (PYDANTIC_AI_GATEWAY_API_KEY,
LOGFIRE_TOKEN, ...) and are held as `SecretStr`, so they are never printed or logged.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
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
    # Systemic outage analysis (one Modal job per scenario); on breach: local fallback.
    outage_timeout_seconds: float = Field(default=45.0, gt=0, le=600)

    # Pydantic AI advice layer (Phase 5). `deterministic` never calls a model.
    agent_mode: Literal["deterministic", "pydantic"] = "deterministic"
    # Pydantic AI model string. `gateway/<provider>:<model>` routes through Pydantic AI Gateway.
    # Default (verified live): Gemini 3.8 Flash on the custom Gateway route `sr`, which speaks
    # OpenAI Chat Completions. Pydantic AI 2.46 cannot parse a custom route slug in the model
    # string (`gateway/<slug>:...`), so the API flavour goes there and the slug goes in the route.
    agent_model: str = "gateway/openai-chat:models/gemini-3.8-flash"
    # Gateway route: a provider or gateway-endpoint slug (Logfire -> Gateway).
    gateway_route: str | None = Field(default="sr", pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")
    # Per model request (HTTP) and whole agent run. On breach: deterministic fallback.
    agent_request_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    agent_run_timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    agent_max_model_requests: int = Field(default=8, ge=2, le=30)

    # Observability. Exported to Logfire only when a token/credentials are present.
    environment: str = Field(default="dev", max_length=32)
    # Model-facing content is allowlisted (no PII), so capturing it in traces is safe.
    logfire_capture_model_content: bool = True

    # Vendor credentials (unprefixed names, as documented by the vendors).
    gateway_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("PYDANTIC_AI_GATEWAY_API_KEY", "PAIG_API_KEY")
    )
    gateway_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PYDANTIC_AI_GATEWAY_BASE_URL", "PAIG_BASE_URL"),
    )
    logfire_token: SecretStr | None = Field(default=None, validation_alias="LOGFIRE_TOKEN")

    @property
    def effective_workload(self) -> str:
        if self.simulation_workload:
            return self.simulation_workload
        return "stress" if self.compute_backend == "modal" else "quick"


@lru_cache
def get_settings() -> Settings:
    return Settings()
