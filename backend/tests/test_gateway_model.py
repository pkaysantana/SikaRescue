"""The Gateway model path that made the live Gemini integration work, tested on the wire.

Gemini 3 behind an OpenAI-compatible (Chat Completions) Gateway route returns a thought
signature in `tool_calls[].extra_content` and rejects the next request with HTTP 400 unless it
is echoed back verbatim. These tests drive a real Pydantic AI agent loop against an in-process
mock Gateway (no network) and inspect the exact JSON each request carried.
"""

from __future__ import annotations

import json

import httpx2
from pydantic_ai import Agent, models

from sikarescue.agent.model import SignaturePreservingChatModel, build_model, describe_model
from sikarescue.config import Settings

SIGNATURE = {"google": {"thought_signature": "c2lnbmF0dXJlLWJ5dGVz"}}
KEY = "pylf_v2_eu_" + "k" * 24


def _completion(message: dict, finish: str) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "models/gemini-3.8-flash",
        "choices": [{"index": 0, "finish_reason": finish, "message": message}],
    }


def _mock_gateway(sent: list[dict], *, signature: dict | None):
    def respond(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        sent.append({"url": str(request.url), "body": body})
        if len(sent) == 1:  # first turn: the model calls a tool (Gemini attaches a signature)
            call = {
                "id": "call_1",
                "type": "function",
                "function": {"name": "inspect_incident", "arguments": "{}"},
            }
            if signature:
                call["extra_content"] = signature
            message = {"role": "assistant", "content": None, "tool_calls": [call]}
            return httpx2.Response(200, json=_completion(message, "tool_calls"))
        return httpx2.Response(
            200, json=_completion({"role": "assistant", "content": "READY"}, "stop")
        )

    return httpx2.AsyncClient(transport=httpx2.MockTransport(respond))


def _gateway_model(client: httpx2.AsyncClient):
    settings = Settings(
        _env_file=None,
        PYDANTIC_AI_GATEWAY_API_KEY=KEY,
        PYDANTIC_AI_GATEWAY_BASE_URL="https://gateway-eu.pydantic.dev/proxy/sr",
        gateway_route="sr",
    )
    choice = describe_model(
        "gateway/openai-chat:models/gemini-3.8-flash",
        settings.gateway_route,
        base_url=settings.gateway_base_url,
    )
    return build_model(choice, settings, http_client=client)


async def _run_one_tool_turn(signature: dict | None) -> list[dict]:
    sent: list[dict] = []
    model = _gateway_model(_mock_gateway(sent, signature=signature))
    agent = Agent(model)

    @agent.tool_plain
    def inspect_incident() -> str:
        """Inspect the incident."""
        return "funds at GH_SETTLEMENT_ACCOUNT"

    with models.override_allow_model_requests(True):  # mock transport: nothing leaves the process
        result = await agent.run("Investigate SK-10421.")
    assert result.output == "READY"
    return sent


def test_chat_completions_gateway_route_uses_the_signature_preserving_model():
    model = _gateway_model(httpx2.AsyncClient())
    assert isinstance(model, SignaturePreservingChatModel)
    assert model.model_name == "models/gemini-3.8-flash"
    assert model.base_url == "https://gateway-eu.pydantic.dev/proxy/sr/"


async def test_gemini_thought_signature_is_echoed_back_on_the_next_request():
    sent = await _run_one_tool_turn(SIGNATURE)
    assert [s["url"] for s in sent] == [
        "https://gateway-eu.pydantic.dev/proxy/sr/chat/completions"
    ] * 2
    assert sent[0]["body"]["model"] == "models/gemini-3.8-flash"
    assistant = next(m for m in sent[1]["body"]["messages"] if m["role"] == "assistant")
    assert assistant["tool_calls"][0]["extra_content"] == SIGNATURE  # verbatim, as Gemini requires


async def test_providers_without_extra_content_are_unaffected():
    sent = await _run_one_tool_turn(signature=None)
    assistant = next(m for m in sent[1]["body"]["messages"] if m["role"] == "assistant")
    assert "extra_content" not in assistant["tool_calls"][0]
