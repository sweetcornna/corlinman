"""Adaptive endpoint completion for openai_compatible base URLs.

A relay's ``base_url`` is pasted in many shapes; ``complete_openai_base_url``
normalises it to the API root the OpenAI SDK appends ``/chat/completions``
onto, so a relay that the admin "fetch models" probe accepted also serves
chat. Mirrors the probe normalization (``_provider_models_url``).
"""

from __future__ import annotations

import pytest
from corlinman_providers import OpenAICompatibleProvider
from corlinman_providers.market_providers import MistralProvider
from corlinman_providers.openai_provider import complete_openai_base_url


@pytest.mark.parametrize(
    "raw, expected",
    [
        # bare host → /v1 appended
        ("https://relay.example", "https://relay.example/v1"),
        ("https://relay.example/", "https://relay.example/v1"),
        ("  https://relay.example  ", "https://relay.example/v1"),
        # already versioned → unchanged (idempotent)
        ("https://relay.example/v1", "https://relay.example/v1"),
        ("https://relay.example/v1/", "https://relay.example/v1"),
        ("https://relay.example/openai/v1", "https://relay.example/openai/v1"),
        ("https://relay.example/api/v4", "https://relay.example/api/v4"),
        # non-versioned sub-path → /v1 appended (matches the probe)
        ("https://relay.example/api", "https://relay.example/api/v1"),
        # full endpoint pasted → trimmed back to the root
        (
            "https://relay.example/v1/chat/completions",
            "https://relay.example/v1",
        ),
        ("https://relay.example/chat/completions", "https://relay.example/v1"),
        ("https://relay.example/v1/responses", "https://relay.example/v1"),
        ("https://relay.example/v1/models", "https://relay.example/v1"),
        # verbatim escape: trailing '#' pins the exact root, no /v1 added
        ("https://relay.example/custom#", "https://relay.example/custom"),
        ("https://relay.example#", "https://relay.example"),
        # empty stays empty
        ("", ""),
    ],
)
def test_complete_openai_base_url(raw: str, expected: str) -> None:
    assert complete_openai_base_url(raw) == expected


def test_complete_is_idempotent() -> None:
    once = complete_openai_base_url("https://relay.example")
    assert complete_openai_base_url(once) == once == "https://relay.example/v1"


def test_compat_provider_completes_bare_host() -> None:
    """A bare-host relay stores the completed /v1 root, so chat_stream's
    AsyncOpenAI client targets ``/v1/chat/completions`` not ``/chat/completions``."""
    p = OpenAICompatibleProvider(base_url="https://relay.example")
    assert p._base_url == "https://relay.example/v1"


def test_compat_provider_trims_full_endpoint() -> None:
    p = OpenAICompatibleProvider(
        base_url="https://relay.example/v1/chat/completions"
    )
    assert p._base_url == "https://relay.example/v1"


def test_compat_provider_idempotent_on_versioned() -> None:
    p = OpenAICompatibleProvider(base_url="https://relay.example/v1")
    assert p._base_url == "https://relay.example/v1"


def test_market_kind_default_base_url_unchanged() -> None:
    """Market kinds ship ``/v1`` defaults — completion must be a no-op so a
    vendor relay isn't rewritten."""
    p = MistralProvider()
    assert p._base_url == "https://api.mistral.ai/v1"
