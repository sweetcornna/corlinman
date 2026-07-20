"""Contract test for the repo-root ``_hermetic_env`` autouse fixture (E5).

A shell that exports ``ANTHROPIC_BASE_URL`` or proxy variables for daily
work must not silently reroute the respx-mocked provider tests. The root
``conftest.py`` scrubs those variables for every non-live test; these
tests pin the contract from inside the suite so a future conftest
refactor that drops the fixture fails loudly instead of resurfacing as
"works on CI, 15 mystery failures on my laptop".
"""

from __future__ import annotations

import os

import pytest


def test_routing_env_is_scrubbed_for_hermetic_tests() -> None:
    for var in (
        "ANTHROPIC_BASE_URL",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        assert var not in os.environ, f"{var} leaked into a hermetic test"


def test_tests_can_still_set_routing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A test's own monkeypatch.setenv runs after the autouse scrub."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:1/api")
    assert os.environ["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:1/api"
