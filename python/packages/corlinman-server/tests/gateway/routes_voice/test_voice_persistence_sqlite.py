"""Tests for the durable :class:`SqliteVoiceSessionStore` (NEW-fhfunc-4,
the SESSION-STORE half).

Three layers, REPRODUCE-FIRST per slice:

* **round-trip** — ``record_start`` → ``fetch`` shows the open row;
  ``record_end`` → ``fetch`` shows the end fields; ``list_for_session``
  returns the session's rows; reopening the store on the SAME file still
  fetches the row (durability across process restart).
* **concurrency** (R5-B3 lesson) — two concurrent start/end pairs on
  DIFFERENT ids don't corrupt each other: the store owns its own
  connection and guards every write with one ``asyncio.Lock``, so no bare
  ``commit()`` can flush another coroutine's transaction.
* **end-to-end** — the real ``/v1/voice`` ASGI route, driven via
  ``TestClient.websocket_connect`` with a real minted tenant API key + a
  :class:`MockVoiceProvider` + a real ``SqliteVoiceSessionStore`` wired,
  writes a ``voice_sessions`` row to the on-disk sqlite file once a
  session completes.

The transcript→chat bridge is intentionally NOT exercised here — it is a
separate follow-up (``transcript_sink`` stays ``None``).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_server.gateway.middleware.auth import ApiKeyAuthState
from corlinman_server.gateway.routes_voice.framing import SUBPROTOCOL
from corlinman_server.gateway.routes_voice.mod import (
    WS_TOKEN_SUBPROTOCOL_PREFIX,
    router,
)
from corlinman_server.gateway.routes_voice.persistence import (
    SqliteVoiceSessionStore,
    VoiceEndReason,
    VoiceSessionEnd,
    VoiceSessionStart,
    VoiceStoreRowMissingError,
)
from corlinman_server.tenancy import AdminDb, TenantId
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

# Root ``asyncio_mode = "auto"`` auto-detects the ``async def`` tests.


# ---------------------------------------------------------------------------
# Direct round-trip + durability
# ---------------------------------------------------------------------------


def _start(
    id: str,
    *,
    tenant_id: str = "acme",
    session_key: str = "sk-1",
    started_at: int = 1000,
) -> VoiceSessionStart:
    return VoiceSessionStart(
        id=id,
        tenant_id=tenant_id,
        session_key=session_key,
        agent_id="agent-7",
        provider_alias="openai",
        started_at=started_at,
    )


async def test_record_start_then_fetch_returns_open_row(tmp_path: Path) -> None:
    store = await SqliteVoiceSessionStore.open(tmp_path / "voice_sessions.sqlite")
    try:
        await store.record_start(_start("voice-1"))
        row = await store.fetch("voice-1")
        assert row is not None
        assert row.id == "voice-1"
        assert row.tenant_id == "acme"
        assert row.session_key == "sk-1"
        assert row.agent_id == "agent-7"
        assert row.provider_alias == "openai"
        assert row.started_at == 1000
        # End fields are NULL until record_end; end_reason defaults to the
        # crash-safe "graceful" placeholder.
        assert row.ended_at is None
        assert row.duration_secs is None
        assert row.audio_path is None
        assert row.transcript_text is None
        assert row.end_reason == VoiceEndReason.GRACEFUL.value
    finally:
        await store.close()


async def test_record_end_updates_same_row_in_place(tmp_path: Path) -> None:
    store = await SqliteVoiceSessionStore.open(tmp_path / "voice_sessions.sqlite")
    try:
        await store.record_start(_start("voice-2"))
        await store.record_end(
            VoiceSessionEnd(
                id="voice-2",
                ended_at=1090,
                duration_secs=90,
                audio_path=None,
                transcript_text="user: hi\nassistant: hello",
                end_reason=VoiceEndReason.CLIENT_DISCONNECT,
            )
        )
        row = await store.fetch("voice-2")
        assert row is not None
        # Start fields preserved through the in-place UPDATE.
        assert row.started_at == 1000
        assert row.session_key == "sk-1"
        # End fields now populated.
        assert row.ended_at == 1090
        assert row.duration_secs == 90
        assert row.transcript_text == "user: hi\nassistant: hello"
        assert row.end_reason == VoiceEndReason.CLIENT_DISCONNECT.value
    finally:
        await store.close()


async def test_record_end_missing_row_raises(tmp_path: Path) -> None:
    """Finalising a session that never started must raise — defends
    against double-finalisation, identical to the memory store."""
    store = await SqliteVoiceSessionStore.open(tmp_path / "voice_sessions.sqlite")
    try:
        with pytest.raises(VoiceStoreRowMissingError):
            await store.record_end(
                VoiceSessionEnd(
                    id="voice-never-started",
                    ended_at=1,
                    duration_secs=0,
                    audio_path=None,
                    transcript_text=None,
                    end_reason=VoiceEndReason.GRACEFUL,
                )
            )
    finally:
        await store.close()


async def test_list_for_session_returns_rows_most_recent_first(
    tmp_path: Path,
) -> None:
    store = await SqliteVoiceSessionStore.open(tmp_path / "voice_sessions.sqlite")
    try:
        await store.record_start(
            _start("voice-a", session_key="chat-9", started_at=100)
        )
        await store.record_start(
            _start("voice-b", session_key="chat-9", started_at=200)
        )
        # Different session_key — must not appear in the listing.
        await store.record_start(
            _start("voice-c", session_key="other", started_at=300)
        )
        rows = await store.list_for_session("acme", "chat-9")
        assert [r.id for r in rows] == ["voice-b", "voice-a"]
        # Different tenant filtered out.
        assert await store.list_for_session("nobody", "chat-9") == []
    finally:
        await store.close()


async def test_durable_across_reopen(tmp_path: Path) -> None:
    """A row written by one store instance is still readable after the
    store is closed and reopened on the SAME file (process-restart
    durability)."""
    db_path = tmp_path / "voice_sessions.sqlite"
    store = await SqliteVoiceSessionStore.open(db_path)
    await store.record_start(_start("voice-durable"))
    await store.record_end(
        VoiceSessionEnd(
            id="voice-durable",
            ended_at=1120,
            duration_secs=120,
            audio_path="/data/tenants/acme/voice/voice-durable.pcm",
            transcript_text=None,
            end_reason=VoiceEndReason.MAX_SESSION,
        )
    )
    await store.close()

    reopened = await SqliteVoiceSessionStore.open(db_path)
    try:
        row = await reopened.fetch("voice-durable")
        assert row is not None
        assert row.duration_secs == 120
        assert row.audio_path == "/data/tenants/acme/voice/voice-durable.pcm"
        assert row.end_reason == VoiceEndReason.MAX_SESSION.value
    finally:
        await reopened.close()


# ---------------------------------------------------------------------------
# Concurrency (R5-B3): no shared-connection transaction interleave
# ---------------------------------------------------------------------------


async def test_concurrent_start_end_on_different_ids_no_corruption(
    tmp_path: Path,
) -> None:
    """Two concurrent record_start/record_end pairs on DIFFERENT ids must
    both land cleanly. The store owns one connection guarded by a single
    write lock, so no bare commit() from one coroutine flushes the other's
    transaction (the R5-B3 shared-connection interleave bug)."""
    store = await SqliteVoiceSessionStore.open(tmp_path / "voice_sessions.sqlite")
    try:

        async def run(idx: int) -> None:
            sid = f"voice-{idx}"
            await store.record_start(
                _start(sid, session_key=f"sk-{idx}", started_at=1000 + idx)
            )
            await store.record_end(
                VoiceSessionEnd(
                    id=sid,
                    ended_at=2000 + idx,
                    duration_secs=idx,
                    audio_path=None,
                    transcript_text=None,
                    end_reason=VoiceEndReason.GRACEFUL,
                )
            )

        await asyncio.gather(*(run(i) for i in range(50)))

        # Every row landed with its OWN end fields — no cross-row bleed.
        for i in range(50):
            row = await store.fetch(f"voice-{i}")
            assert row is not None, f"row voice-{i} missing"
            assert row.duration_secs == i
            assert row.ended_at == 2000 + i
            assert row.started_at == 1000 + i
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# End-to-end: real /v1/voice ASGI route writes a voice_sessions row
# ---------------------------------------------------------------------------


def test_end_to_end_route_writes_voice_sessions_row(tmp_path: Path) -> None:
    """Drive the REAL ``/v1/voice`` ASGI route via
    ``TestClient.websocket_connect`` with a real minted tenant API key +
    a MockVoiceProvider + a real ``SqliteVoiceSessionStore`` wired through
    the production ``build_voice_state_from_app`` resolution path,
    complete a session, then assert a ``voice_sessions`` row was written
    to the sqlite file on disk.

    This is a SYNCHRONOUS test on purpose: ``TestClient`` runs the ASGI
    app on its OWN event loop in a worker thread, and ``aiosqlite``
    connections (AdminDb + the voice store) are bound to the loop that
    created them. Opening those on the outer test loop and using them
    from the app loop deadlocks — so everything async (AdminDb open + key
    mint, store open) happens INSIDE the app's loop: AdminDb via the
    FastAPI lifespan, the voice store via the per-connect
    ``build_voice_state_from_app`` path the gateway mount really uses.
    """
    data_dir = tmp_path / "voice-data"
    db_file = data_dir / "voice_sessions.sqlite"

    # A tiny AppState shim build_voice_state_from_app understands: it reads
    # ``config['voice']`` (dict) + ``data_dir`` and resolves the provider.
    # No OpenAI key anywhere → resolve_voice_provider falls back to the
    # MockVoiceProvider, which is exactly what we want to drive. A
    # SimpleNamespace (instance, not class attrs) keeps the shim mutable
    # without tripping the class-level-mutable-default lint.
    corlinman = SimpleNamespace(
        config={
            "voice": {
                "enabled": True,
                "provider_alias": "mock",
                "budget_minutes_per_tenant_per_day": 60,
                "default_tenant": "default",
            },
            "providers": {},
        },
        data_dir=str(data_dir),
    )

    minted: dict[str, str] = {}

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> Any:
        # Open AdminDb + mint the key on the APP's event loop so
        # verify_api_key (also on the app loop) shares the same aiosqlite
        # connection thread.
        db = await AdminDb.open(tmp_path / "tenants.sqlite")
        tenant = TenantId.new("acme")
        await db.create_tenant(tenant, "Acme Inc", created_at=0)
        key = await db.mint_api_key(tenant, "operator", "voice", None)
        minted["token"] = key.token
        app.state.corlinman = corlinman
        app.state.api_key_auth = ApiKeyAuthState(admin_db=db)
        try:
            yield
        finally:
            await db.close()

    app = FastAPI(lifespan=_lifespan)
    # Bare router (no explicit state) → per-connect resolution via
    # build_voice_state_from_app, the real gateway mount path. This is
    # where SqliteVoiceSessionStore is opened (on the app loop).
    app.include_router(router())

    consumed: list[Any] = []
    with TestClient(app) as client:
        token = minted["token"]
        with client.websocket_connect(
            "/v1/voice",
            subprotocols=[SUBPROTOCOL, f"{WS_TOKEN_SUBPROTOCOL_PREFIX}{token}"],
        ) as ws:
            ws.send_text(json.dumps({"type": "start", "session_key": "sk-e2e"}))
            ws.send_text(json.dumps({"type": "end"}))
            # Read frames until the server closes (graceful end). Starlette's
            # TestClient surfaces the server close as a ``websocket.close``
            # message dict (NOT a raised WebSocketDisconnect), so break on
            # that type; also catch the disconnect for robustness. Bound the
            # loop so a protocol regression can't hang the suite.
            for _ in range(50):
                try:
                    msg = ws.receive()
                except WebSocketDisconnect:
                    break
                consumed.append(msg)
                if msg.get("type") == "websocket.close":
                    break

    # The `started` handshake frame went out — the session really ran.
    assert any(
        m.get("type") == "websocket.send"
        and "started" in (m.get("text") or "")
        for m in consumed
    ), f"no started frame observed; frames={consumed}"

    # The on-disk sqlite file must now hold exactly one row for the
    # session, with both the start AND end fields persisted.
    assert db_file.exists(), "voice_sessions.sqlite was never created on disk"
    conn = sqlite3.connect(str(db_file))
    try:
        cur = conn.execute(
            "SELECT id, tenant_id, session_key, provider_alias, "
            "started_at, ended_at, duration_secs, end_reason "
            "FROM voice_sessions WHERE session_key = ?",
            ("sk-e2e",),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    assert len(rows) == 1, f"expected exactly one voice_sessions row, got {rows}"
    (
        _row_id,
        tenant_id,
        session_key,
        provider_alias,
        started_at,
        ended_at,
        duration_secs,
        end_reason,
    ) = rows[0]
    # Tenant bound to the authenticated key, not a spoofable header.
    assert tenant_id == "acme"
    assert session_key == "sk-e2e"
    assert provider_alias == "mock"
    assert started_at is not None
    # record_end fired on the graceful close → end fields populated.
    assert ended_at is not None
    assert duration_secs is not None
    assert end_reason == VoiceEndReason.GRACEFUL.value
