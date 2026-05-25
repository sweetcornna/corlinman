"""Tests for ``GET /admin/credentials/{provider}/{key}/reveal`` (W2.1).

The reveal endpoint is the only place the admin surface returns
cleartext credential values. Everything else stays masked. These tests
pin the four contract decisions that matter most:

* Reveal of a literal-string credential returns the cleartext value.
* Reveal of a missing credential returns 404 with a typed envelope
  (the UI uses this to show "credential not found" instead of trying
  to render an empty modal).
* Reveal honours the same admin auth gate as every other admin-B route.
* The cleartext value is NEVER logged — only ``provider`` + ``key``
  make it into the audit record.

Same fixture pattern as ``test_credentials.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from corlinman_server.gateway.routes_admin_b import credentials
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._admin_auth import authenticated_test_client, configure_admin_auth

# ---------------------------------------------------------------------------
# Fixtures (mirrors test_credentials.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_config_path(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text("", encoding="utf-8")
    return cfg


@pytest.fixture()
def admin_state(temp_config_path: Path) -> Iterator[AdminState]:
    snapshot: dict[str, Any] = {}

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
    app.include_router(credentials.router())
    return authenticated_test_client(app)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


SECRET_VALUE = "sk-test-reveal-1234secret"


def _seed_openai_api_key(state: AdminState, value: str = SECRET_VALUE) -> None:
    snapshot: dict[str, Any] = state.extras["snapshot"]
    snapshot["providers"] = {
        "openai": {
            "kind": "openai",
            "enabled": True,
            "api_key": value,
        }
    }


def test_reveal_returns_value_for_existing_credential(
    client: TestClient, admin_state: AdminState
) -> None:
    """A stored literal credential reveals its cleartext value."""
    _seed_openai_api_key(admin_state)

    resp = client.get("/admin/credentials/openai/api_key/reveal")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"value": SECRET_VALUE}


def test_reveal_returns_value_for_value_dict_shape(
    client: TestClient, admin_state: AdminState
) -> None:
    """``{"value": "..."}`` storage shape reveals the inner literal."""
    snapshot: dict[str, Any] = admin_state.extras["snapshot"]
    snapshot["providers"] = {
        "openai": {
            "kind": "openai",
            "enabled": True,
            "api_key": {"value": SECRET_VALUE},
        }
    }

    resp = client.get("/admin/credentials/openai/api_key/reveal")
    assert resp.status_code == 200
    assert resp.json() == {"value": SECRET_VALUE}


# ---------------------------------------------------------------------------
# 404 paths
# ---------------------------------------------------------------------------


def test_reveal_404_for_unknown_credential(client: TestClient) -> None:
    """No provider block → 404 with a typed envelope."""
    resp = client.get("/admin/credentials/openai/api_key/reveal")
    assert resp.status_code == 404
    assert resp.json() == {"error": "credential_not_found"}


def test_reveal_404_for_env_referenced_credential(
    client: TestClient, admin_state: AdminState
) -> None:
    """``{env="FOO"}`` shape is opaque — the admin surface never reads env."""
    snapshot: dict[str, Any] = admin_state.extras["snapshot"]
    snapshot["providers"] = {
        "openai": {
            "kind": "openai",
            "enabled": True,
            "api_key": {"env": "OPENAI_API_KEY"},
        }
    }

    resp = client.get("/admin/credentials/openai/api_key/reveal")
    assert resp.status_code == 404
    assert resp.json() == {"error": "credential_not_found"}


def test_reveal_400_for_unknown_field(client: TestClient) -> None:
    """Whitelist gate is preserved — unknown fields 400 the same as PUT."""
    resp = client.get("/admin/credentials/openai/secret_token/reveal")
    assert resp.status_code == 400
    assert resp.json() == {"error": "unknown_field"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_reveal_requires_admin_auth(admin_state: AdminState) -> None:
    """Without the admin Basic header the route must reject before any
    config lookup. We mount the router without the auth header so the
    require_admin dependency fires.
    """
    _seed_openai_api_key(admin_state)

    app = FastAPI()
    app.include_router(credentials.router())
    with TestClient(app) as bare:  # no auth header
        resp = bare.get("/admin/credentials/openai/api_key/reveal")
    # The shared admin guard returns either 401 (configured admin) or
    # 403 (admin_not_configured fallthrough). Anything in the 4xx
    # family on the unauth path proves the gate fired before the
    # handler.
    assert resp.status_code in (401, 403), resp.text
    # And the body absolutely must not contain the cleartext value.
    assert SECRET_VALUE not in resp.text


# ---------------------------------------------------------------------------
# Audit — the cleartext must never appear in any log record
# ---------------------------------------------------------------------------


def test_reveal_never_logs_value(
    client: TestClient,
    admin_state: AdminState,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Capture every log record across the reveal and assert the value
    is absent. Structlog routes through the stdlib logging module by
    default, so caplog catches it the same as any other emitter.
    """
    _seed_openai_api_key(admin_state)

    with caplog.at_level(logging.DEBUG):
        resp = client.get("/admin/credentials/openai/api_key/reveal")
    assert resp.status_code == 200

    # No record (message, args, or formatted output) may contain the
    # secret. We also check the structured ``extra`` dict in case a
    # downstream sink ingests record attributes directly.
    for record in caplog.records:
        rendered = record.getMessage()
        assert SECRET_VALUE not in rendered, (
            f"secret leaked into log message: {rendered!r}"
        )
        for value in record.__dict__.values():
            assert SECRET_VALUE not in str(value), (
                f"secret leaked into log record attribute: {value!r}"
            )

    # And the response body of course contains it — sanity check we
    # actually hit the reveal path rather than e.g. an early 404.
    assert SECRET_VALUE in resp.text
