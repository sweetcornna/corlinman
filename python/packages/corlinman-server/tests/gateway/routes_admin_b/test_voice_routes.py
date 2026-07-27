"""Tests for ``/admin/voice/*`` — backend catalog, defaults, 试听 preview.

Pins the contract the voice settings page depends on:

* the catalog lists shipped backends *and* any ``[voice.backends.*]`` the
  operator defined, with a truthful ``credential_set`` flag;
* config edits reach the registry without a restart (and removals apply);
* secrets round-trip as ``***REDACTED***`` — the UI never holds plaintext
  and never blanks a stored key by echoing the sentinel back;
* preview returns a playable ``/v1/files`` url, and a provider failure
  surfaces the upstream code (notably GPT-Live's attestation 503) instead
  of a blind 500.

Same fixture pattern as ``test_credentials.py`` — mount just this router,
install an ``AdminState`` over a temp TOML, and refresh the snapshot
between writes the way the production watcher would.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from corlinman_agent.voice import SynthesisError, reset_custom_backends
from corlinman_server.gateway.routes_admin_b import voice as voice_routes
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    set_admin_state,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ._admin_auth import authenticated_test_client, configure_admin_auth

_AUDIO = b"ID3\x04\x00FAKEMP3" * 16


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    reset_custom_backends()
    yield
    reset_custom_backends()


@pytest.fixture()
def admin_state(tmp_path: Path) -> Iterator[AdminState]:
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text("", encoding="utf-8")
    snapshot: dict[str, Any] = {}

    state = AdminState(config_loader=lambda: dict(snapshot), config_path=cfg_path)
    configure_admin_auth(state)
    state.extras["snapshot"] = snapshot
    state.extras["data_dir"] = tmp_path
    set_admin_state(state)
    try:
        yield state
    finally:
        set_admin_state(None)


@pytest.fixture()
def client(admin_state: AdminState) -> TestClient:
    app = FastAPI()
    app.include_router(voice_routes.router())
    return authenticated_test_client(app)


def _reload(state: AdminState) -> None:
    snapshot: dict[str, Any] = state.extras["snapshot"]
    snapshot.clear()
    assert state.config_path is not None
    raw = state.config_path.read_text(encoding="utf-8")
    if raw.strip():
        snapshot.update(tomllib.loads(raw))


def _set_snapshot(state: AdminState, cfg: dict[str, Any]) -> None:
    snapshot: dict[str, Any] = state.extras["snapshot"]
    snapshot.clear()
    snapshot.update(cfg)


def _stub_synthesis(monkeypatch: pytest.MonkeyPatch, audio: bytes = _AUDIO) -> dict:
    """Route the templated HTTP driver at a MockTransport."""
    seen: dict[str, Any] = {}
    real = voice_routes.synthesize

    async def _wrapped(request):  # type: ignore[no-untyped-def]
        seen["request"] = request

        def handler(req: httpx.Request) -> httpx.Response:
            seen["url"] = str(req.url)
            return httpx.Response(200, content=audio)

        from dataclasses import replace

        return await real(replace(request, transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(voice_routes, "synthesize", _wrapped)
    return seen


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def test_catalog_lists_shipped_backends(client: TestClient) -> None:
    resp = client.get("/admin/voice/backends")
    assert resp.status_code == 200
    payload = resp.json()
    ids = [b["id"] for b in payload["backends"]]
    assert ids[:6] == ["gpt_live", "openai", "fish", "elevenlabs", "gemini", "minimax"]
    assert "mp3" in payload["formats"]


def test_catalog_exposes_voice_metadata_for_the_picker(client: TestClient) -> None:
    backends = {b["id"]: b for b in client.get("/admin/voice/backends").json()["backends"]}
    live = backends["gpt_live"]
    assert live["kind"] == "webrtc_live"
    assert {v["id"] for v in live["voices"]} == {
        "arbor", "breeze", "cove", "ember", "juniper",
        "maple", "sol", "spruce", "vale",
    }
    assert live["supports_instructions"] is True

    openai = backends["openai"]
    assert openai["default_voice"] == "alloy"
    assert {v["id"] for v in openai["voices"] if v["recommended"]} == {"marin", "cedar"}

    fish = backends["fish"]
    assert fish["free_form_voices"] is True
    assert fish["voices"] == []


def test_custom_backend_from_config_appears_in_catalog(
    client: TestClient, admin_state: AdminState
) -> None:
    _set_snapshot(
        admin_state,
        {
            "voice": {
                "backends": {
                    "acme": {
                        "label": "Acme 语音",
                        "base_url": "https://acme.test",
                        "models": ["v2"],
                        "voices": [{"id": "lin", "label": "小林"}],
                        "http": {"path": "/say", "body": {"q": "{text}"}},
                    }
                }
            }
        },
    )
    backends = {b["id"]: b for b in client.get("/admin/voice/backends").json()["backends"]}
    assert "acme" in backends
    assert backends["acme"]["label"] == "Acme 语音"
    assert backends["acme"]["custom"] is True
    assert [v["id"] for v in backends["acme"]["voices"]] == ["lin"]


def test_removing_a_custom_backend_takes_effect_without_restart(
    client: TestClient, admin_state: AdminState
) -> None:
    _set_snapshot(
        admin_state,
        {"voice": {"backends": {"acme": {"http": {"path": "/x"}}}}},
    )
    assert "acme" in [b["id"] for b in client.get("/admin/voice/backends").json()["backends"]]

    _set_snapshot(admin_state, {"voice": {}})
    ids = [b["id"] for b in client.get("/admin/voice/backends").json()["backends"]]
    assert "acme" not in ids
    # ...and the shipped backends are still intact.
    assert "openai" in ids


def test_credential_set_reflects_config_and_env(
    client: TestClient, admin_state: AdminState, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("FISH_AUDIO_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _set_snapshot(
        admin_state, {"voice": {"backends": {"fish": {"api_key": "fk-1"}}}}
    )
    backends = {b["id"]: b for b in client.get("/admin/voice/backends").json()["backends"]}
    assert backends["fish"]["credential_set"] is True
    assert backends["elevenlabs"]["credential_set"] is False

    monkeypatch.setenv("ELEVENLABS_API_KEY", "xi-key")
    backends = {b["id"]: b for b in client.get("/admin/voice/backends").json()["backends"]}
    assert backends["elevenlabs"]["credential_set"] is True


def test_openai_backend_can_ride_the_configured_chat_provider(
    client: TestClient, admin_state: AdminState, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _set_snapshot(
        admin_state,
        {"providers": {"main": {"kind": "openai", "api_key": "sk-chat"}}},
    )
    backends = {b["id"]: b for b in client.get("/admin/voice/backends").json()["backends"]}
    assert backends["openai"]["credential_set"] is True
    assert backends["gpt_live"]["credential_set"] is True
    # A third-party backend must NOT count the OpenAI key as its own.
    assert backends["elevenlabs"]["credential_set"] is False


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_put_settings_persists_and_round_trips(
    client: TestClient, admin_state: AdminState
) -> None:
    resp = client.put(
        "/admin/voice/settings",
        json={"backend": "gpt-live", "voice": "cove", "format": "mp3", "enabled": True},
    )
    assert resp.status_code == 200
    _reload(admin_state)

    on_disk = tomllib.loads(admin_state.config_path.read_text(encoding="utf-8"))
    # The alias spelling is folded on write, so config stays canonical.
    assert on_disk["voice"]["backend"] == "gpt_live"
    assert on_disk["voice"]["voice"] == "cove"

    got = client.get("/admin/voice/settings").json()
    assert got["backend"] == "gpt_live"
    assert got["voice"] == "cove"


def test_put_rejects_unknown_format(client: TestClient) -> None:
    resp = client.put("/admin/voice/settings", json={"format": "8track"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "unknown_format"


def test_secret_is_redacted_on_read(client: TestClient, admin_state: AdminState) -> None:
    _set_snapshot(
        admin_state,
        {"voice": {"backends": {"fish": {"api_key": "fk-secret", "voice": "ref-1"}}}},
    )
    got = client.get("/admin/voice/settings").json()
    assert got["backends"]["fish"]["api_key"] == "***REDACTED***"
    assert got["backends"]["fish"]["voice"] == "ref-1"


def test_echoing_the_redaction_sentinel_keeps_the_stored_secret(
    client: TestClient, admin_state: AdminState
) -> None:
    client.put(
        "/admin/voice/settings",
        json={"backends": {"fish": {"api_key": "fk-original", "voice": "ref-1"}}},
    )
    _reload(admin_state)

    # The UI re-submits the form it was given, sentinel and all.
    client.put(
        "/admin/voice/settings",
        json={"backends": {"fish": {"api_key": "***REDACTED***", "voice": "ref-2"}}},
    )
    _reload(admin_state)

    on_disk = tomllib.loads(admin_state.config_path.read_text(encoding="utf-8"))
    assert on_disk["voice"]["backends"]["fish"]["api_key"] == "fk-original"
    assert on_disk["voice"]["backends"]["fish"]["voice"] == "ref-2"


def test_blank_secret_also_keeps_the_stored_one(
    client: TestClient, admin_state: AdminState
) -> None:
    client.put("/admin/voice/settings", json={"backends": {"fish": {"api_key": "fk-1"}}})
    _reload(admin_state)
    client.put("/admin/voice/settings", json={"backends": {"fish": {"api_key": ""}}})
    _reload(admin_state)
    on_disk = tomllib.loads(admin_state.config_path.read_text(encoding="utf-8"))
    assert on_disk["voice"]["backends"]["fish"]["api_key"] == "fk-1"


def test_settings_write_preserves_unrelated_sections(
    client: TestClient, admin_state: AdminState
) -> None:
    _set_snapshot(admin_state, {"admin": {"bind": "127.0.0.1"}, "models": {"default": "x"}})
    client.put("/admin/voice/settings", json={"voice": "nova"})
    on_disk = tomllib.loads(admin_state.config_path.read_text(encoding="utf-8"))
    assert on_disk["admin"]["bind"] == "127.0.0.1"
    assert on_disk["models"]["default"] == "x"


def test_put_without_config_path_returns_503(admin_state: AdminState) -> None:
    admin_state.config_path = None
    app = FastAPI()
    app.include_router(voice_routes.router())
    resp = authenticated_test_client(app).put("/admin/voice/settings", json={"voice": "x"})
    assert resp.status_code == 503
    assert resp.json()["error"] == "config_path_unset"


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


def test_preview_returns_a_playable_url(
    client: TestClient, admin_state: AdminState, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(admin_state.extras["data_dir"]))
    _set_snapshot(
        admin_state,
        {"voice": {"backend": "openai", "backends": {"openai": {"api_key": "sk-1"}}}},
    )
    seen = _stub_synthesis(monkeypatch)

    resp = client.post("/admin/voice/preview", json={"voice": "nova", "text": "试听一下"})
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["url"].startswith("/v1/files/")
    assert payload["mime"] == "audio/mpeg"
    assert payload["backend"] == "openai"
    assert payload["voice"] == "nova"
    assert payload["size_bytes"] == len(_AUDIO)
    assert "/audio/speech" in seen["url"]


def test_preview_uses_default_text_when_blank(
    client: TestClient, admin_state: AdminState, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(admin_state.extras["data_dir"]))
    _set_snapshot(admin_state, {"voice": {"backends": {"openai": {"api_key": "k"}}}})
    seen = _stub_synthesis(monkeypatch)
    assert client.post("/admin/voice/preview", json={}).status_code == 200
    assert seen["request"].text == voice_routes._DEFAULT_PREVIEW_TEXT


def test_preview_truncates_long_text(
    client: TestClient, admin_state: AdminState, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(admin_state.extras["data_dir"]))
    _set_snapshot(admin_state, {"voice": {"backends": {"openai": {"api_key": "k"}}}})
    seen = _stub_synthesis(monkeypatch)
    client.post("/admin/voice/preview", json={"text": "长" * 5000})
    assert len(seen["request"].text) == voice_routes._PREVIEW_MAX_CHARS


def test_preview_unknown_backend_is_404(client: TestClient) -> None:
    resp = client.post("/admin/voice/preview", json={"backend": "nope"})
    assert resp.status_code == 404
    assert resp.json()["error"] == "unknown_backend"


def test_preview_surfaces_upstream_failure_not_a_blind_500(
    client: TestClient, admin_state: AdminState, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(admin_state.extras["data_dir"]))
    _set_snapshot(admin_state, {"voice": {"backends": {"openai": {"api_key": "k"}}}})

    real = voice_routes.synthesize

    async def _wrapped(request):  # type: ignore[no-untyped-def]
        from dataclasses import replace

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(429, text="rate limited")

        return await real(replace(request, transport=httpx.MockTransport(handler)))

    monkeypatch.setattr(voice_routes, "synthesize", _wrapped)

    resp = client.post("/admin/voice/preview", json={})
    assert resp.status_code == 502
    body = resp.json()
    assert body["error"] == "tts_http_status"
    assert body["upstream_status"] == 429


def test_preview_reports_gpt_live_attestation_block(
    client: TestClient, admin_state: AdminState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact condition the production gateway returns today.

    The Live pre-flight must win over any local problem (e.g. a missing
    aiortc wheel): "your gateway cannot attest" is the actionable fact,
    and installing a dependency would not change it.
    """
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(admin_state.extras["data_dir"]))
    _set_snapshot(
        admin_state,
        {"voice": {"backend": "gpt_live", "backends": {"gpt_live": {"api_key": "k"}}}},
    )

    async def _blocked(definition, provider, params):  # type: ignore[no-untyped-def]
        return SynthesisError(
            "live_attestation_unavailable",
            "网关拒绝 Live 会话：Live attestation is unavailable: live "
            "attestation is only supported when Sub2API runs on macOS",
            status_code=503,
        )

    monkeypatch.setattr(voice_routes, "_probe_live", _blocked)

    def _boom(*_a, **_k):  # pragma: no cover - must not be reached
        raise AssertionError("synthesis attempted despite a blocked gateway")

    monkeypatch.setattr(voice_routes, "synthesize", _boom)

    resp = client.post("/admin/voice/preview", json={})
    assert resp.status_code == 502
    body = resp.json()
    assert body["error"] == "live_attestation_unavailable"
    assert body["upstream_status"] == 503
    assert "attestation" in body["message"]


def test_preview_proceeds_when_live_gateway_is_healthy(
    client: TestClient, admin_state: AdminState, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(admin_state.extras["data_dir"]))
    _set_snapshot(
        admin_state,
        {"voice": {"backend": "gpt_live", "backends": {"gpt_live": {"api_key": "k"}}}},
    )

    async def _ok(definition, provider, params):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(voice_routes, "_probe_live", _ok)
    calls: list[Any] = []

    async def _synth(request):  # type: ignore[no-untyped-def]
        calls.append(request)
        raise SynthesisError("gpt_live_dependency_missing", "aiortc 未安装")

    monkeypatch.setattr(voice_routes, "synthesize", _synth)

    resp = client.post("/admin/voice/preview", json={})
    assert len(calls) == 1
    assert resp.json()["error"] == "gpt_live_dependency_missing"
