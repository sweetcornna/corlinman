"""Regression: the OpenAI-wire client must NOT send the SDK's default
``OpenAI/Python`` User-Agent.

Prod incident (1.28.1): an OpenAI-compatible relay behind Cloudflare
blocked the official SDK's User-Agent with a generic
``upstream_auth_permanent: Your request was blocked.`` 403 even though the
API key was valid. The adapter now sends a neutral ``corlinman-gateway``
UA (operator custom headers still win).
"""

from __future__ import annotations

from corlinman_providers.openai_compatible import OpenAICompatibleProvider
from corlinman_providers.openai_provider import (
    _NEUTRAL_USER_AGENT,
    OpenAIProvider,
)
from openai._models import FinalRequestOptions


def _request_ua(client) -> str:
    req = client._build_request(
        FinalRequestOptions(
            method="post",
            url="/chat/completions",
            json_data={"model": "x", "messages": []},
        )
    )
    return req.headers.get("user-agent", "")


def test_openai_client_sends_neutral_user_agent() -> None:
    p = OpenAIProvider(api_key="sk-test", base_url="https://api.example.test/v1")
    ua = _request_ua(p._make_client())
    assert ua == _NEUTRAL_USER_AGENT
    assert "openai" not in ua.lower()


def test_openai_compatible_client_sends_neutral_user_agent() -> None:
    p = OpenAICompatibleProvider(
        base_url="https://api.cornna.example/", api_key="sk-test"
    )
    ua = _request_ua(p._make_client())
    assert ua == _NEUTRAL_USER_AGENT


def test_operator_custom_user_agent_wins() -> None:
    p = OpenAIProvider(
        api_key="sk-test",
        base_url="https://api.example.test/v1",
        default_headers={"User-Agent": "my-custom-ua/9"},
    )
    ua = _request_ua(p._make_client())
    assert ua == "my-custom-ua/9"
