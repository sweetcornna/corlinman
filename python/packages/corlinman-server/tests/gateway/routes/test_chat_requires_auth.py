"""Security regression: ``/v1/*`` must require a tenant API key (R1-001).

The :class:`ApiKeyAuthMiddleware` ships ready-to-install but for a long
time the gateway boot path never actually called
:func:`install_api_key_middleware`. Combined with the agent servicer's
default auto-inject of ``RUN_SHELL_TOOL`` and the approval gate being a
TODO stub, an unauthenticated POST to ``/v1/chat/completions`` that
nudged the model into ``run_shell`` was an unauthenticated RCE on the
default ``0.0.0.0:8080`` bind. The same omission also leaked the
per-turn ``/approve`` route.

These tests stand on top of the real :func:`build_app` (the production
boot path) so a regression that silently un-installs the middleware
again — or installs it with the wrong path-prefix — fails this file
loudly. The legacy router-only fixture in ``test_chat_tracing.py`` does
not exercise middleware at all and would have happily passed alongside
the original bug.
"""

from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from corlinman_server.gateway.lifecycle.entrypoint import build_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _client(tmp_path: Path) -> TestClient:
    """Build the full gateway app + return a ``TestClient`` over it.

    ``with TestClient(app)`` drives the FastAPI ``lifespan`` so the
    AdminDb open + middleware rebind happens before the first request,
    matching production. Tests must use ``with`` so the lifespan
    teardown closes the AdminDb sqlite handle.
    """
    app = build_app(config_path=None, data_dir=tmp_path)
    return TestClient(app)


def test_chat_completions_without_auth_header_returns_401(
    tmp_path: Path,
) -> None:
    """An unauthenticated POST to /v1/chat/completions must 401, not 200."""
    with _client(tmp_path) as client:
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": "any-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert resp.status_code == 401, (
        f"expected 401 from /v1/chat/completions with no auth header; "
        f"got {resp.status_code}: {resp.text[:200]!r}"
    )
    body = resp.json()
    assert body.get("error") == "unauthorized", body
    # Either ``missing_authorization`` (middleware installed + admin_db
    # wired) or ``admin_db_not_configured`` (middleware installed but
    # admin_db open failed during lifespan) — both are fail-closed and
    # both fix R1-001. ``invalid_token`` would be unexpected here
    # because we sent no token; anything else indicates the middleware
    # is misinstalled.
    assert body.get("reason") in {
        "missing_authorization",
        "admin_db_not_configured",
    }, body
    assert resp.headers.get("www-authenticate", "").lower().startswith("bearer")


def test_chat_completions_with_bogus_bearer_returns_401(
    tmp_path: Path,
) -> None:
    """A POST with an unknown bearer token must 401."""
    with _client(tmp_path) as client:
        resp = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer not-a-real-key"},
            json={
                "model": "any-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert resp.status_code == 401, (
        f"expected 401 from /v1/chat/completions with bogus bearer; "
        f"got {resp.status_code}: {resp.text[:200]!r}"
    )
    body = resp.json()
    assert body.get("error") == "unauthorized", body


def test_chat_approve_without_auth_header_returns_401(
    tmp_path: Path,
) -> None:
    """The per-turn /approve route shares the same gate; no header → 401."""
    with _client(tmp_path) as client:
        resp = client.post(
            "/v1/chat/completions/turn-abc/approve",
            json={
                "call_id": "call_abc123",
                "approved": True,
            },
        )

    assert resp.status_code == 401, (
        f"expected 401 from /v1/chat/completions/<turn>/approve with "
        f"no auth header; got {resp.status_code}: {resp.text[:200]!r}"
    )
    body = resp.json()
    assert body.get("error") == "unauthorized", body


def test_health_endpoint_remains_unauthenticated(tmp_path: Path) -> None:
    """The api-key gate must NOT cover liveness/readiness probes —
    Kubernetes / docker-compose health checks send no Bearer token and
    breaking them would brick every deploy. Regression guard for an
    overly-greedy ``protected_prefixes`` change."""
    with _client(tmp_path) as client:
        resp = client.get("/health")
    assert resp.status_code == 200, resp.text
