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


@lru_cache
def get_settings() -> Settings:
    return Settings()
