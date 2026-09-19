"""Resolve the agent's Pydantic AI model: through Pydantic AI Gateway (default) or directly.

`gateway/<provider>:<model>` strings are built with `gateway_provider(...)`, so an optional
route (a provider slug or gateway-endpoint slug from Logfire -> Gateway) and a custom HTTP
client can be supplied. Credentials come from settings / the environment and are never
logged or echoed in error messages.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from typing import Any

from openai.types import chat
from openai.types.chat import ChatCompletionMessageFunctionToolCallParam
from pydantic_ai import ModelResponse, ToolCallPart
from pydantic_ai.exceptions import UserError
from pydantic_ai.models import Model, infer_model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers import Provider
from pydantic_ai.providers.gateway import gateway_provider, normalize_gateway_provider

from sikarescue.config import Settings

# Direct (non-Gateway) providers and the environment variables their SDKs read.
_DIRECT_PROVIDER_KEYS: dict[str, tuple[str, ...]] = {
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "groq": ("GROQ_API_KEY",),
}


# Gateway API flavours that speak OpenAI Chat Completions (e.g. a custom BYOK Gemini route).
_CHAT_COMPLETIONS_FLAVOURS = frozenset({"openai-chat"})
_EXTRA_CONTENT = "openai_compatible_extra_content"


class SignaturePreservingChatModel(OpenAIChatModel):
    """Chat Completions model that round-trips a tool call's `extra_content` unchanged.

    Gemini 3 behind an OpenAI-compatible endpoint returns a thought signature in
    `tool_calls[].extra_content.google` and rejects the next request (HTTP 400, "Function call
    is missing a thought_signature") unless it is sent back verbatim. Pydantic AI 2.46 drops the
    field on this path; this keeps it on the `ToolCallPart` and echoes it. Providers that never
    send `extra_content` are unaffected.
    """

    def _process_response(self, response: chat.ChatCompletion | str) -> ModelResponse:
        result = super()._process_response(response)
        if isinstance(response, str) or not response.choices:
            return result
        extras = {
            call.id: extra
            for call in response.choices[0].message.tool_calls or ()
            if (extra := (call.model_extra or {}).get("extra_content"))
        }
        if not extras:
            return result
        parts = [
            replace(
                p,
                provider_details={
                    **(p.provider_details or {}),
                    _EXTRA_CONTENT: extras[p.tool_call_id],
                },
            )
            if isinstance(p, ToolCallPart) and p.tool_call_id in extras
            else p
            for p in result.parts
        ]
        return replace(result, parts=parts)

    @staticmethod
    def _map_tool_call(t: ToolCallPart) -> ChatCompletionMessageFunctionToolCallParam:
        param = OpenAIChatModel._map_tool_call(t)
        if extra := (t.provider_details or {}).get(_EXTRA_CONTENT):
            param["extra_content"] = extra  # type: ignore[typeddict-unknown-key]
        return param


class ModelUnavailableError(Exception):
    """No usable model: missing credentials or an unknown model/provider string."""


@dataclass(frozen=True)
class ModelChoice:
    name: str  # e.g. "gateway/openai-chat:models/gemini-3.8-flash"
    upstream: str  # e.g. "openai-chat" (the API flavour behind the Gateway)
    via_gateway: bool
    route: str | None = None

    @property
    def label(self) -> str:
        if not self.via_gateway:
            return f"{self.name} (direct provider, NOT via Pydantic AI Gateway)"
        return f"{self.name} via Pydantic AI Gateway" + (
            f" (route {self.route})" if self.route else ""
        )


def gateway_endpoint(base_url: str | None, route: str | None) -> tuple[str | None, str | None]:
    """(proxy root, route) from either documented base-URL form.

    The Connect tab gives a full endpoint URL (`https://gateway-eu.pydantic.dev/proxy/<route>`);
    `gateway_provider` expects the proxy root and appends the route itself. Accept both, never
    build `.../<route>/<route>`, and refuse a base URL that names a different route.
    """
    if not base_url:
        return None, route
    root, sep, tail = base_url.rstrip("/").partition("/proxy/")
    embedded = tail.strip("/")
    if not sep or not embedded:
        return base_url, route
    if "/" in embedded:
        raise ModelUnavailableError("PYDANTIC_AI_GATEWAY_BASE_URL must end at /proxy/<route>")
    if route and route != embedded:
        raise ModelUnavailableError(
            f"PYDANTIC_AI_GATEWAY_BASE_URL names route {embedded!r} but "
            f"SIKARESCUE_GATEWAY_ROUTE is {route!r}"
        )
    return f"{root}/proxy", embedded


def describe_model(
    name: str, route: str | None = None, *, base_url: str | None = None
) -> ModelChoice:
    provider, sep, model_id = name.partition(":")
    if not sep or not provider or not model_id:
        raise ModelUnavailableError(f"model {name!r} must look like '<provider>:<model>'")
    via_gateway = provider.startswith("gateway/")
    if via_gateway:
        _, route = gateway_endpoint(base_url, route)
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
        base_url, route = gateway_endpoint(settings.gateway_base_url, choice.route)

        def gateway(provider_name: str) -> Provider[Any]:
            return gateway_provider(
                provider_name,
                route=route,
                api_key=api_key,
                base_url=base_url,
                http_client=http_client,
            )

        provider_name, _, model_id = choice.name.partition(":")
        if normalize_gateway_provider(provider_name) in _CHAT_COMPLETIONS_FLAVOURS:
            return SignaturePreservingChatModel(model_id, provider=gateway(provider_name))
        return infer_model(choice.name, provider_factory=gateway)
    except UserError as exc:
        # Pydantic AI's messages name env vars/models, never key values.
        raise ModelUnavailableError(str(exc)[:200]) from exc
