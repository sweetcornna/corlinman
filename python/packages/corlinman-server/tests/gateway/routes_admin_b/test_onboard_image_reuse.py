"""Tests for ``POST /admin/onboard/finalize-image-provider`` choice=reuse.

The "reuse my current chat provider for image generation" branch probes
the live provider slot via
:func:`corlinman_providers.capabilities.probe_image_capability` (an
``async def`` returning the ``{supported, evidence, models}`` wire shape).

Regression coverage for the bug where the handler called the async probe
*without* ``await`` — the resulting coroutine is not a dict and has no
``supported`` attribute, so every reuse call collapsed to
``supported=False`` and returned 409, leaking an un-awaited coroutine.
"""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import corlinman_providers.capabilities as cap
import pytest
from corlinman_server.gateway.routes_admin_b import onboard
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._admin_auth import authenticated_test_client, configure_admin_auth

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_config_path(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text("", encoding="utf-8")
    return cfg


@pytest.fixture()
def admin_state(temp_config_path: Path) -> Iterator[AdminState]:
    """AdminState seeded with one configured OpenAI chat provider."""
    snapshot: dict[str, Any] = {
        "providers": {
            "openai": {
                "kind": "openai",
                "enabled": True,
                "api_key": {"value": "sk-xxx"},
            }
        }
    }

    def _loader() -> dict[str, Any]:
        return dict(snapshot)

    state = AdminState(
        config_loader=_loader,
        config_path=temp_config_path,
    )
    configure_admin_auth(state)
    state.extras["snapshot"] = snapshot
    set_admin_state(state)
    try:
        yield state
    finally:
        set_admin_state(None)


@pytest.fixture()
def client(admin_state: AdminState) -> TestClient:
    app = FastAPI()
    app.include_router(onboard.router())
    return authenticated_test_client(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_reuse_returns_200_when_probe_reports_supported(
    client: TestClient,
    admin_state: AdminState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A supported provider's reuse choice succeeds (no spurious 409)."""

    async def _fake_probe(provider: Any) -> dict[str, Any]:
        return {
            "supported": True,
            "evidence": "images_endpoint_ok_(200)",
            "models": ["gpt-image-1"],
        }

    monkeypatch.setattr(cap, "probe_image_capability", _fake_probe)

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        resp = client.post(
            "/admin/onboard/finalize-image-provider",
            json={"choice": "reuse", "provider_name": "openai"},
        )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["choice"] == "reuse"
    assert payload["image_provider"] == "openai"
    assert payload["evidence"] == "images_endpoint_ok_(200)"


def test_reuse_returns_409_when_probe_reports_unsupported(
    client: TestClient,
    admin_state: AdminState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unsupported provider still 409s with the awaited evidence."""

    async def _fake_probe(provider: Any) -> dict[str, Any]:
        return {
            "supported": False,
            "evidence": "images_endpoint_not_present_(404)",
            "models": [],
        }

    monkeypatch.setattr(cap, "probe_image_capability", _fake_probe)

    resp = client.post(
        "/admin/onboard/finalize-image-provider",
        json={"choice": "reuse", "provider_name": "openai"},
    )

    assert resp.status_code == 409, resp.text
    payload = resp.json()
    assert payload["error"] == "image_not_supported"
    assert payload["supported"] is False
    assert payload["evidence"] == "images_endpoint_not_present_(404)"


def test_reuse_marks_provider_image_capable_on_success(
    client: TestClient,
    admin_state: AdminState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a supported reuse, the slot is persisted with image_capable=true."""
    import tomllib

    async def _fake_probe(provider: Any) -> dict[str, Any]:
        return {"supported": True, "evidence": "ok", "models": []}

    monkeypatch.setattr(cap, "probe_image_capability", _fake_probe)

    resp = client.post(
        "/admin/onboard/finalize-image-provider",
        json={"choice": "reuse", "provider_name": "openai"},
    )
    assert resp.status_code == 200, resp.text

    assert admin_state.config_path is not None
    on_disk = tomllib.loads(
        admin_state.config_path.read_text(encoding="utf-8")
    )
    assert on_disk["providers"]["openai"]["image_capable"] is True
