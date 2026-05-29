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
from typing import Any

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


# ---------------------------------------------------------------------------
# R2-001: legacy aliases of the canonical /v1/* routes were sneaking past the
# gate. The route registry mounts the same handlers under both ``/v1/...``
# AND a legacy bare prefix (e.g. ``/memory/upsert``, ``/canvas/render``,
# ``/plugin-callback/{id}``). R1-001 only added ``/v1/`` to
# ``DEFAULT_PROTECTED_PREFIXES`` so an unauthenticated attacker could still
# wipe memory docs, render canvas content, subscribe to canvas SSE streams
# (exfiltrating live operator output), and poison parked agent loops via
# fake plugin callbacks — all by addressing the alias path. Extend the gate.
# ---------------------------------------------------------------------------


def _assert_unauthorized(resp: Any, route: str) -> None:
    """Shared assertion shape for the R2-001 alias-gate tests."""
    assert resp.status_code == 401, (
        f"expected 401 from unauthenticated {route}; "
        f"got {resp.status_code}: {resp.text[:200]!r}"
    )
    body = resp.json()
    assert body.get("error") == "unauthorized", body
    assert body.get("reason") in {
        "missing_authorization",
        "admin_db_not_configured",
    }, body


def test_legacy_memory_upsert_alias_requires_auth(tmp_path: Path) -> None:
    """``POST /memory/upsert`` is a legacy alias of ``/v1/memory/upsert``
    that lets an unauthenticated caller write into the per-tenant memory
    store. The api-key gate must cover it."""
    with _client(tmp_path) as client:
        resp = client.post(
            "/memory/upsert",
            json={"content": "attacker-injected", "namespace": "default"},
        )
    _assert_unauthorized(resp, "/memory/upsert")


def test_legacy_canvas_render_alias_requires_auth(tmp_path: Path) -> None:
    """``POST /canvas/render`` runs the Canvas Renderer (HTML/template
    composition + I/O). Unauthenticated access is a sandbox + DoS amplifier
    plus a vector for forcing renderer-side fetches."""
    with _client(tmp_path) as client:
        resp = client.post("/canvas/render", json={"kind": "noop"})
    _assert_unauthorized(resp, "/canvas/render")


def test_legacy_canvas_session_events_alias_requires_auth(
    tmp_path: Path,
) -> None:
    """``GET /canvas/session/{id}/events`` is the SSE stream of rendered
    LLM output for an active operator session. Unauthenticated subscription
    is a live-stream exfiltration channel for any guessed session id."""
    with _client(tmp_path) as client:
        resp = client.get("/canvas/session/cs_anything/events")
    _assert_unauthorized(resp, "/canvas/session/<id>/events")


def test_legacy_plugin_callback_alias_requires_auth(tmp_path: Path) -> None:
    """``POST /plugin-callback/{task_id}`` is the alias of
    ``/v1/plugins/callback/{task_id}``. Even though the ``task_id`` itself
    is the per-task one-shot credential (and SEC-106 tracks tightening
    that), gating the prefix is defence-in-depth: an attacker who can
    enumerate / guess task ids loses the ability to inject fake tool
    results into parked agent loops just by hitting the alias."""
    with _client(tmp_path) as client:
        resp = client.post("/plugin-callback/anything", json={})
    _assert_unauthorized(resp, "/plugin-callback/<task_id>")
