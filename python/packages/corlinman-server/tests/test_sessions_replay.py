"""Tests for ``POST /admin/sessions/{session_key}/replay``.

Pins the existing replay handler in
``routes_admin_a/sessions.py``. The endpoint is **already** the legacy
transcript-dump shape that matches the live deployment's
``/openapi.json`` and the ``replay-dialog.tsx`` consumer:

* request body: ``{ "mode": "transcript" | "rerun" | null }``
* 200 ``transcript`` mode: ``{ session_key, mode, transcript: [
  { role, content, ts } ], summary: { message_count, tenant_id,
  rerun_diff? } }``
* 503 ``rerun_disabled`` for ``rerun`` mode — live deployment also
  returns this envelope until the chat-service rerun plumbing lands in
  ``routes_admin_b``.
* 404 ``not_found`` for unknown session keys.

This file covers the handler explicitly, since the existing admin-A
test suite only exercises GET + DELETE on ``/admin/sessions``.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from corlinman_replay.session_store import (
    SessionMessage,
    SessionRole,
    SqliteSessionStore,
)
from corlinman_server.gateway.routes_admin_a import (
    AdminState,
    build_router,
    set_admin_state,
)
from corlinman_server.gateway.routes_admin_a._session_store import (
    AdminSessionStore,
)
from corlinman_server.gateway.routes_admin_a.auth import hash_password
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _basic_auth_header(username: str = "admin", password: str = "rootroot") -> str:
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


def _seed_flat_sessions(
    data_dir: Path, sessions: dict[str, list[tuple[str, str]]]
) -> None:
    """Pre-populate the flat-legacy ``<data_dir>/sessions.sqlite`` (the
    path the route picks when ``tenants_enabled=False`` and the tenant
    is the legacy default — the default ``AdminState`` shape) with one
    row per ``(role, content)`` tuple, in order."""

    async def _run() -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        store = await SqliteSessionStore.open(data_dir / "sessions.sqlite")
        try:
            for session_key, msgs in sessions.items():
                for role, content in msgs:
                    await store.append(
                        session_key,
                        SessionMessage(
                            role=SessionRole.from_str(role),
                            content=content,
                            ts=datetime.now(UTC),
                        ),
                    )
        finally:
            await store.close()

    asyncio.run(_run())


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    state = AdminState(
        data_dir=tmp_path,
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
    )
    set_admin_state(state)
    app = FastAPI()
    app.include_router(build_router())
    with TestClient(app, headers={"Authorization": _basic_auth_header()}) as c:
        yield c
    set_admin_state(None)


# ---------------------------------------------------------------------------
# transcript mode
# ---------------------------------------------------------------------------


def test_replay_transcript_mode_returns_message_list(
    client: TestClient, tmp_path: Path
) -> None:
    """A seeded session round-trips through the route as the legacy
    transcript-dump shape that the live deployment + ``replay-dialog.tsx``
    consume."""
    _seed_flat_sessions(
        tmp_path,
        {
            "sess-talk": [
                ("user", "hello"),
                ("assistant", "hi there"),
                ("user", "how are you"),
            ],
        },
    )

    resp = client.post(
        "/admin/sessions/sess-talk/replay", json={"mode": "transcript"}
    )
    assert resp.status_code == 200, resp.text

    body = resp.json()
    assert body["session_key"] == "sess-talk"
    assert body["mode"] == "transcript"
    assert len(body["transcript"]) == 3
    assert [m["role"] for m in body["transcript"]] == [
        "user",
        "assistant",
        "user",
    ]
    assert [m["content"] for m in body["transcript"]] == [
        "hello",
        "hi there",
        "how are you",
    ]
    # Every transcript row carries an RFC-3339 timestamp string.
    for m in body["transcript"]:
        assert isinstance(m["ts"], str) and len(m["ts"]) > 0

    assert body["summary"]["message_count"] == 3
    assert body["summary"]["tenant_id"] == "default"
    # transcript mode never emits the rerun_diff sentinel.
    assert "rerun_diff" not in body["summary"]


def test_replay_default_mode_is_transcript(
    client: TestClient, tmp_path: Path
) -> None:
    """Omitting the body (and omitting ``mode``) falls back to
    ``transcript`` — matches the route's ``_parse_mode(None)`` default
    and the frontend client default in ``ui/lib/api/sessions.ts``."""
    _seed_flat_sessions(tmp_path, {"sess-default": [("user", "hi")]})

    resp = client.post("/admin/sessions/sess-default/replay")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "transcript"
    assert body["summary"]["message_count"] == 1


# ---------------------------------------------------------------------------
# rerun mode — 503 rerun_disabled (mirrors live; chat-service wiring
# lives in routes_admin_b and isn't reachable from this handler)
# ---------------------------------------------------------------------------


def test_replay_rerun_mode_returns_rerun_disabled(
    client: TestClient, tmp_path: Path
) -> None:
    """``mode=rerun`` returns the same 503 envelope the live deployment
    emits today. The frontend's ``replaySession`` maps this to
    ``{ kind: "rerun_disabled" }`` which ``ReplayDialog`` renders as the
    "rerun not implemented" placeholder.

    NOTE: the plan's wishlist asked for ``rerun`` to create a NEW
    session_key. We deliberately do not implement that here — it would
    diverge from the live shape probed at ``corlinman.cornna.xyz``
    (``ReplayBody`` only exposes ``mode``; no ``since_turn_id``; no
    ``new_session_key`` response field). Mirror over reinvent."""
    _seed_flat_sessions(tmp_path, {"sess-rerun": [("user", "hi")]})

    resp = client.post(
        "/admin/sessions/sess-rerun/replay", json={"mode": "rerun"}
    )
    assert resp.status_code == 503, resp.text
    assert resp.json()["detail"]["error"] == "rerun_disabled"


# ---------------------------------------------------------------------------
# 404 not_found
# ---------------------------------------------------------------------------


def test_replay_unknown_session_returns_404(
    client: TestClient, tmp_path: Path
) -> None:
    """Unknown session_key → 404 ``not_found`` (the ``SessionNotFoundError``
    + ``StoreOpenError`` paths collapse to the same envelope). The
    frontend's ``replaySession`` maps this to ``{ kind: "not_found" }``."""
    # Seed an unrelated session so the store file exists; the route
    # still reaches SessionNotFoundError for the missing key.
    _seed_flat_sessions(tmp_path, {"sess-other": [("user", "hi")]})

    resp = client.post(
        "/admin/sessions/never-existed/replay", json={"mode": "transcript"}
    )
    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert detail["error"] == "not_found"
    assert detail["session_key"] == "never-existed"


def test_replay_404_when_store_file_missing(
    client: TestClient,
) -> None:
    """No ``sessions.sqlite`` at all → 404 ``not_found`` (the
    ``StoreOpenError`` branch maps to the same envelope so the UI
    renders the same "session not found" card regardless of why the
    underlying read failed)."""
    resp = client.post(
        "/admin/sessions/anything/replay", json={"mode": "transcript"}
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["detail"]["error"] == "not_found"


# ---------------------------------------------------------------------------
# auth + sessions_disabled gates
# ---------------------------------------------------------------------------


def test_replay_auth_required(tmp_path: Path) -> None:
    """No ``Authorization`` header → 401, never reaches the handler."""
    state = AdminState(
        data_dir=tmp_path,
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
    )
    set_admin_state(state)
    app = FastAPI()
    app.include_router(build_router())
    try:
        # No default Authorization header on this client.
        with TestClient(app) as c:
            resp = c.post(
                "/admin/sessions/whatever/replay",
                json={"mode": "transcript"},
            )
            assert resp.status_code == 401, resp.text
    finally:
        set_admin_state(None)


def test_replay_503_when_sessions_disabled(tmp_path: Path) -> None:
    """``sessions_disabled=True`` short-circuits before the store read."""
    state = AdminState(
        data_dir=tmp_path,
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
        sessions_disabled=True,
    )
    set_admin_state(state)
    app = FastAPI()
    app.include_router(build_router())
    try:
        with TestClient(
            app, headers={"Authorization": _basic_auth_header()}
        ) as c:
            resp = c.post(
                "/admin/sessions/anything/replay",
                json={"mode": "transcript"},
            )
            assert resp.status_code == 503, resp.text
            assert resp.json()["detail"]["error"] == "sessions_disabled"
    finally:
        set_admin_state(None)
