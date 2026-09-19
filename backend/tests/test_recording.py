"""The recording configuration is checked up front: without Pydantic AI the definitive
scenario would (correctly) stop in MANUAL_REVIEW, so the recording must refuse to start."""

from __future__ import annotations

from pydantic import SecretStr

from sikarescue.agent.live import recording_problems
from sikarescue.config import Settings

KEY = SecretStr("pylf_v2_eu_" + "k" * 24)  # synthetic test key


def _settings(**overrides) -> Settings:
    values = {
        "agent_mode": "pydantic",
        "compute_backend": "modal",
        "gateway_route": "sr",
        "agent_model": "gateway/openai-chat:models/gemini-3.8-flash",
        "PYDANTIC_AI_GATEWAY_API_KEY": KEY,
        "PYDANTIC_AI_GATEWAY_BASE_URL": "https://gateway-eu.pydantic.dev/proxy",
    }
    return Settings(_env_file=None, **(values | overrides))


def test_recording_configuration_is_accepted():
    assert recording_problems(_settings()) == []


def test_deterministic_agent_mode_is_refused_before_recording():
    problems = recording_problems(_settings(agent_mode="deterministic"))
    assert any("agent mode is 'deterministic'" in p for p in problems)


def test_wrong_route_model_compute_or_missing_key_are_refused():
    assert recording_problems(_settings(gateway_route="other"))
    assert recording_problems(_settings(agent_model="gateway/openai-chat:models/other-model"))
    assert recording_problems(_settings(compute_backend="local"))
    missing = recording_problems(_settings(PYDANTIC_AI_GATEWAY_API_KEY=None))
    assert missing and not any("pylf_" in p for p in missing)  # names only, never values
