"""Tests for the public, unauthenticated persona-art routes (F2).

Covers:

* ``GET /public/personas/{id}/assets/{aid}`` serves the blob with the same
  media-type / ETag / cache headers the admin route emits.
* Cross-persona id confusion (asset of persona B under persona A's path) 404s.
* Unknown persona / asset id 404s; a metadata row whose blob was deleted 404s.
* ``GET /public/personas/{id}/avatar`` redirects to the first emoji, falls back
  to the reference 立绘 when no emoji exists, and 404s when the persona has no
  art at all.
* No store wired (degraded boot) → 404, never 500.
* The status token carries an optional ``persona_id`` round-trip
  (``verify_status_token_full``) while staying backward compatible.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from corlinman_server.gateway.routes import public_personas
from corlinman_server.gateway.routes import status as status_routes
from corlinman_server.gateway.status_token import (
    make_status_token,
    resolve_signing_key,
    verify_status_token,
    verify_status_token_full,
)
from corlinman_server.persona import PersonaAssetStore
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Minimal valid PNG header so the upload passes the MIME allowlist.
_PNG = bytes.fromhex(
    "89504E470D0A1A0A0000000D49484452000000010000000108020000009077"
    "53DE"
)

_KEY = b"k" * 32


@pytest.fixture
async def store(tmp_path):
    s = await PersonaAssetStore.open(
        tmp_path / "persona_assets.sqlite",
        tmp_path / "personas",
    )
    try:
        yield s
    finally:
        await s.close()


def _client_for(store_or_none) -> TestClient:
    """Mount the public router with ``store_or_none`` on the admin_a slot —
    exactly the slot the gateway lifespan populates."""
    app = FastAPI()
    app.include_router(public_personas.router())
    # The route probes ``app.state.corlinman_admin_a_state.persona_asset_store``
    # (the real wiring), so stash the store behind that namespace.
    app.state.corlinman_admin_a_state = SimpleNamespace(
        persona_asset_store=store_or_none
    )
    return TestClient(app)


# ---------------------------------------------------------------------------
# Blob serve
# ---------------------------------------------------------------------------


async def test_serve_asset_returns_blob_with_etag(store) -> None:
    rec = await store.put(
        "lycaon", "emoji", "happy",
        bytes_=_PNG, mime="image/png", file_name="happy.png",
    )
    client = _client_for(store)

    resp = client.get(f"/public/personas/lycaon/assets/{rec.id}")
    assert resp.status_code == 200
    assert resp.content == _PNG
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["etag"] == f'"{rec.sha256}"'
    assert "immutable" in resp.headers["cache-control"]


async def test_serve_reference_kind_too(store) -> None:
    # Both art kinds are public — reference 立绘 are served same as emoji.
    rec = await store.put(
        "vivian", "reference", "front",
        bytes_=_PNG, mime="image/png", file_name="front.png",
    )
    client = _client_for(store)
    resp = client.get(f"/public/personas/vivian/assets/{rec.id}")
    assert resp.status_code == 200
    assert resp.content == _PNG


async def test_cross_persona_id_confusion_404s(store) -> None:
    rec = await store.put(
        "vivian", "emoji", "happy",
        bytes_=_PNG, mime="image/png", file_name="happy.png",
    )
    client = _client_for(store)
    # vivian's asset id requested under lycaon's path → 404 (path-confusion guard).
    resp = client.get(f"/public/personas/lycaon/assets/{rec.id}")
    assert resp.status_code == 404


async def test_unknown_asset_404s(store) -> None:
    client = _client_for(store)
    resp = client.get("/public/personas/lycaon/assets/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "asset_not_found"


async def test_blob_missing_behind_metadata_404s(store, tmp_path) -> None:
    rec = await store.put(
        "lycaon", "emoji", "happy",
        bytes_=_PNG, mime="image/png", file_name="happy.png",
    )
    # Simulate a manual rm of the blob while the metadata row survives.
    store.path_for(rec).unlink()
    client = _client_for(store)
    resp = client.get(f"/public/personas/lycaon/assets/{rec.id}")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "asset_blob_missing"


async def test_no_store_wired_404s_not_500() -> None:
    client = _client_for(None)
    resp = client.get("/public/personas/lycaon/assets/whatever")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Avatar redirect
# ---------------------------------------------------------------------------


async def test_avatar_prefers_emoji(store) -> None:
    emoji = await store.put(
        "lycaon", "emoji", "happy",
        bytes_=_PNG, mime="image/png", file_name="happy.png",
    )
    await store.put(
        "lycaon", "reference", "front",
        bytes_=_PNG + b"\x01", mime="image/png", file_name="front.png",
    )
    client = _client_for(store)
    resp = client.get(
        "/public/personas/lycaon/avatar", follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == (
        f"/public/personas/lycaon/assets/{emoji.id}"
    )


async def test_avatar_falls_back_to_reference(store) -> None:
    ref = await store.put(
        "vivian", "reference", "front",
        bytes_=_PNG, mime="image/png", file_name="front.png",
    )
    client = _client_for(store)
    resp = client.get(
        "/public/personas/vivian/avatar", follow_redirects=False
    )
    assert resp.status_code == 302
    assert resp.headers["location"] == (
        f"/public/personas/vivian/assets/{ref.id}"
    )


async def test_avatar_followed_serves_the_blob(store) -> None:
    await store.put(
        "lycaon", "emoji", "happy",
        bytes_=_PNG, mime="image/png", file_name="happy.png",
    )
    client = _client_for(store)
    # follow_redirects (default) → the redirect resolves to the blob.
    resp = client.get("/public/personas/lycaon/avatar")
    assert resp.status_code == 200
    assert resp.content == _PNG


async def test_avatar_no_art_404s(store) -> None:
    client = _client_for(store)
    resp = client.get("/public/personas/ghost/avatar", follow_redirects=False)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Status token carries persona_id (F2) — backward compatible
# ---------------------------------------------------------------------------


def test_token_persona_round_trip() -> None:
    token = make_status_token("sess-1", _KEY, now=1000, persona_id="lycaon")
    assert verify_status_token_full(token, _KEY, now=1000) == (
        "sess-1",
        "lycaon",
    )
    # Session-only variant still returns just the session key.
    assert verify_status_token(token, _KEY, now=1000) == "sess-1"


def test_token_without_persona_is_byte_identical_to_pre_f2() -> None:
    # Omitting persona_id must keep the exact pre-F2 3-field body so existing
    # mint callsites (agent_servicer / config_loading) are unaffected.
    with_none = make_status_token("s", _KEY, now=1000, epoch=2)
    without_kw = make_status_token("s", _KEY, now=1000, epoch=2, persona_id=None)
    assert with_none == without_kw
    # And it verifies with a None persona.
    assert verify_status_token_full(with_none, _KEY, now=1000, current_epoch=2) == (
        "s",
        None,
    )


def test_token_persona_is_signed_not_swappable() -> None:
    # Swapping the persona field invalidates the signature.
    token = make_status_token("s", _KEY, now=1000, persona_id="lycaon")
    _body, _, sig = token.partition(".")
    # Forge a body with a different persona but the original signature.
    forged_body = make_status_token(
        "s", _KEY, now=1000, persona_id="vivian"
    ).partition(".")[0]
    forged = f"{forged_body}.{sig}"
    assert verify_status_token_full(forged, _KEY, now=1000) is None


def test_status_data_surfaces_persona_id(tmp_path, monkeypatch) -> None:
    """The public /status/{token}/data route echoes the token's bound persona_id."""
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    key = resolve_signing_key(tmp_path)
    token = make_status_token("sess-1", key, persona_id="grantley")
    app = FastAPI()
    app.include_router(status_routes.router())
    # No journal wired -> empty-snapshot path, which still carries persona_id.
    client = TestClient(app)
    resp = client.get(f"/status/{token}/data")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_key"] == "sess-1"
    assert body["persona_id"] == "grantley"


def test_status_data_persona_id_null_when_unbound(tmp_path, monkeypatch) -> None:
    """A token minted without a persona surfaces persona_id=None (back-compat)."""
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    key = resolve_signing_key(tmp_path)
    token = make_status_token("sess-2", key)
    app = FastAPI()
    app.include_router(status_routes.router())
    client = TestClient(app)
    resp = client.get(f"/status/{token}/data")
    assert resp.status_code == 200
    assert resp.json()["persona_id"] is None
