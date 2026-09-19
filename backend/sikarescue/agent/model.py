"""Resolve the agent's Pydantic AI model: through Pydantic AI Gateway (default) or directly.

`gateway/<provider>:<model>` strings are built with `gateway_provider(...)`, so an optional
route (a provider slug or gateway-endpoint slug from Logfire -> Gateway) and a custom HTTP
client can be supplied. Credentials come from settings / the environment and are never
logged or echoed in error messages.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from pydantic_ai.exceptions import UserError
from pydantic_ai.models import Model, infer_model
from pydantic_ai.providers import Provider
from pydantic_ai.providers.gateway import gateway_provider

from sikarescue.config import Settings

# Direct (non-Gateway) providers and the environment variables their SDKs read.
_DIRECT_PROVIDER_KEYS: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "groq": ("GROQ_API_KEY",),
}


class ModelUnavailableError(Exception):
    """No usable model: missing credentials or an unknown model/provider string."""


@dataclass(frozen=True)
class ModelChoice:
    name: str  # e.g. "gateway/anthropic:claude-haiku-4-5"
    upstream: str  # e.g. "anthropic"
    via_gateway: bool
    route: str | None = None

    @property
    def label(self) -> str:
        if not self.via_gateway:
            return f"{self.name} (direct provider, NOT via Pydantic AI Gateway)"
        return f"{self.name} via Pydantic AI Gateway" + (
            f" (route {self.route})" if self.route else ""
        )


def describe_model(name: str, route: str | None = None) -> ModelChoice:
    provider, sep, model_id = name.partition(":")
    if not sep or not provider or not model_id:
        raise ModelUnavailableError(f"model {name!r} must look like '<provider>:<model>'")
    via_gateway = provider.startswith("gateway/")
    return ModelChoice(
        name=name,
        upstream=provider.removeprefix("gateway/"),
        via_gateway=via_gateway,
        route=route if via_gateway else None,
    )


def missing_configuration(choice: ModelChoice, settings: Settings) -> list[str]:
    """Names (never values) of the credentials this model still needs."""
    if choice.via_gateway:
        if settings.gateway_api_key is None:
            return ["PYDANTIC_AI_GATEWAY_API_KEY (create one in Logfire -> Gateway -> API Keys)"]
        return []
    keys = _DIRECT_PROVIDER_KEYS.get(choice.upstream, ())
    if keys and not any(os.getenv(k) for k in keys):
        return [" or ".join(keys)]
    return []


def build_model(
    choice: ModelChoice, settings: Settings, *, http_client: Any | None = None
) -> Model:
    missing = missing_configuration(choice, settings)
    if missing:
        raise ModelUnavailableError("missing " + ", ".join(missing))
    try:
        if not choice.via_gateway:
            return infer_model(choice.name)
        assert settings.gateway_api_key is not None
        api_key = settings.gateway_api_key.get_secret_value()

        def gateway(provider_name: str) -> Provider[Any]:
            return gateway_provider(
                provider_name,
                route=choice.route,
                api_key=api_key,
                base_url=settings.gateway_base_url,
                http_client=http_client,
            )

        return infer_model(choice.name, provider_factory=gateway)
    except UserError as exc:
        # Pydantic AI's messages name env vars/models, never key values.
        raise ModelUnavailableError(str(exc)[:200]) from exc
