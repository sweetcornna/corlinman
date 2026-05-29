"""Route-level tests for ``gateway/routes/memory.py`` (TEST-007).

Two angles:

* **auth required** — the ``/v1/memory/*`` surface lets a caller read /
  write the per-tenant memory store, so it must 401 without a tenant API
  key. Driven through the production :func:`build_app` boot path (the
  R2-001 alias-gate test only covers ``/memory/upsert``; this adds the
  canonical ``/v1/memory/*`` paths).
* **basic memory ops** — upsert → read-back, read-missing → 404, health
  → 200, malformed body → 400. Backed by a *real*
  ``corlinman_memory_host.LocalSqliteHost`` (no mock of the host), driven
  through the real route endpoint coroutines.

Why endpoint coroutines instead of ``TestClient`` for the ops:
``LocalSqliteHost`` wraps an ``aiosqlite`` connection bound to the event
loop it was opened on. ``TestClient`` runs requests on its own private
anyio portal loop, so a host opened with ``asyncio.run`` (a now-closed
loop) deadlocks on the first query. Driving the handlers on one loop
keeps the host + handlers on the same loop, which is what production
does. The handlers themselves are the real, unmodified route functions.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("corlinman_memory_host")

from corlinman_memory_host import LocalSqliteHost  # noqa: E402
from corlinman_server.gateway.lifecycle.entrypoint import build_app  # noqa: E402
from corlinman_server.gateway.routes.memory import MemoryState, router  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.requests import Request  # noqa: E402


def _json_request(method: str, body: Any | None = None, raw: bytes | None = None) -> Request:
    payload = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": payload, "more_body": False}

    scope = {
        "type": "http",
        "method": method,
        "headers": [(b"content-type", b"application/json")],
        "path": "/x",
        "query_string": b"",
    }
    return Request(scope, receive)


def _endpoints(state: MemoryState) -> dict[tuple[tuple[str, ...], str], Any]:
    api = router(state)
    return {
        (tuple(sorted(r.methods)), r.path): r.endpoint  # type: ignore[attr-defined]
        for r in api.routes
    }


async def _open_host() -> LocalSqliteHost:
    return await LocalSqliteHost.open(
        "local", Path(tempfile.mkdtemp()) / "mem.sqlite"
    )


# ---------------------------------------------------------------------------
# auth required (through the production build_app boot path)
# ---------------------------------------------------------------------------


def test_canonical_memory_upsert_requires_auth(tmp_path: Path) -> None:
    """``POST /v1/memory/upsert`` writes into the per-tenant store; it
    must 401 without a tenant API key."""
    app = build_app(config_path=None, data_dir=tmp_path)
    with TestClient(app) as client:
        resp = client.post(
            "/v1/memory/upsert",
            json={"content": "attacker", "namespace": "default"},
        )
    assert resp.status_code == 401, resp.text
    assert resp.json().get("error") == "unauthorized"


def test_canonical_memory_query_requires_auth(tmp_path: Path) -> None:
    app = build_app(config_path=None, data_dir=tmp_path)
    with TestClient(app) as client:
        resp = client.post("/v1/memory/query", json={"query": "x", "top_k": 1})
    assert resp.status_code == 401, resp.text
    assert resp.json().get("error") == "unauthorized"


# ---------------------------------------------------------------------------
# basic memory ops against a real LocalSqliteHost
# ---------------------------------------------------------------------------


async def test_upsert_then_get_round_trip() -> None:
    host = await _open_host()
    try:
        ep = _endpoints(MemoryState(host=host))
        upsert = ep[(("POST",), "/v1/memory/upsert")]
        get_doc = ep[(("GET",), "/v1/memory/docs/{id}")]

        up_resp = await upsert(
            _json_request(
                "POST", {"content": "hello world", "namespace": "default"}
            )
        )
        assert up_resp.status_code == 200, bytes(up_resp.body)
        doc_id = json.loads(bytes(up_resp.body))["id"]

        get_resp = await get_doc(doc_id)
        assert get_resp.status_code == 200, bytes(get_resp.body)
        assert json.loads(bytes(get_resp.body))["content"] == "hello world"
    finally:
        await host.close()


async def test_get_missing_doc_returns_404() -> None:
    host = await _open_host()
    try:
        ep = _endpoints(MemoryState(host=host))
        get_doc = ep[(("GET",), "/v1/memory/docs/{id}")]
        resp = await get_doc("does-not-exist")
        assert resp.status_code == 404, bytes(resp.body)
        body = json.loads(bytes(resp.body))
        assert body["error"] == "not_found"
        assert body["resource"] == "memory_doc"
        assert body["id"] == "does-not-exist"
    finally:
        await host.close()


async def test_health_returns_ok() -> None:
    host = await _open_host()
    try:
        ep = _endpoints(MemoryState(host=host))
        health = ep[(("GET",), "/v1/memory/health")]
        resp = await health()
        assert resp.status_code == 200, bytes(resp.body)
        assert json.loads(bytes(resp.body)) == {"status": "ok"}
    finally:
        await host.close()


async def test_upsert_malformed_body_returns_400() -> None:
    host = await _open_host()
    try:
        ep = _endpoints(MemoryState(host=host))
        upsert = ep[(("POST",), "/v1/memory/upsert")]
        resp = await upsert(_json_request("POST", raw=b"not valid json"))
        assert resp.status_code == 400, bytes(resp.body)
        assert json.loads(bytes(resp.body))["error"] == "invalid_request"
    finally:
        await host.close()
