"""H19 — web chat (``/v1/chat/completions``) persona injection.

The in-app ``/chat`` UI drives ``POST /v1/chat/completions``. Historically
that path NEVER injected a persona — ``persona_id`` was always empty — so
格兰 was out of character on the web while the 5 chat channels
(Telegram/Discord/Slack/Feishu/QQ) prepended the persona system prompt to
every inbound turn.

These tests pin the new ``[web].humanlike`` wiring in
:mod:`corlinman_server.gateway.routes.chat`:

* gate ON + bound ``persona_id`` → a leading ``role="system"`` message
  carrying the persona's ``system_prompt`` is prepended and
  ``InternalChatRequest.persona_id`` is set;
* gate OFF → no injection (request untouched, ``persona_id`` empty);
* an explicit ``X-Persona-Id`` header / body field overrides the config
  default and force-enables injection;
* a missing persona store / missing row degrades silently.

Strategy: a scripted ``ChatService`` records the exact
:class:`InternalChatRequest` the route handed it, so we can assert on the
post-injection messages + ``persona_id`` directly. We stand up the full
``router`` against an ``app.state.corlinman`` carrying the live config and
install a fake persona store on the admin_a singleton (the same handle the
production path reads).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from corlinman_server.gateway.routes.chat import ChatState, router
from corlinman_server.gateway_api import (
    DoneEvent,
    InternalChatRequest,
    Role,
    TokenDeltaEvent,
)

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# ─── Scripted service that records the internal request ──────────────────────


class _RecordingChatService:
    """Minimal ``ChatService`` stand-in that records the request it ran."""

    def __init__(self) -> None:
        self.seen: InternalChatRequest | None = None

    def run(
        self, req: InternalChatRequest, cancel: asyncio.Event
    ) -> AsyncIterator[Any]:
        self.seen = req
        return self._aiter()

    async def _aiter(self) -> AsyncIterator[Any]:
        yield TokenDeltaEvent(text="hi")
        yield DoneEvent(finish_reason="stop", usage=None)


# ─── Fakes for the persona store + admin_a state ─────────────────────────────


@dataclass
class _FakePersona:
    system_prompt: str
    model_bindings: dict[str, Any] | None = None


class _FakePersonaStore:
    """Duck-typed persona store exposing the single ``get`` coroutine the
    injection helper calls. ``asset_store`` stays ``None`` so no emoji block."""

    def __init__(self, personas: dict[str, _FakePersona]) -> None:
        self._personas = personas

    async def get(self, persona_id: str) -> _FakePersona | None:
        return self._personas.get(persona_id)


class _FakeAdminState:
    def __init__(self, persona_store: Any) -> None:
        self.persona_store = persona_store
        self.persona_asset_store = None


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_app(
    service: _RecordingChatService,
    config: dict[str, Any] | None,
) -> FastAPI:
    """Build the chat router with ``app.state.corlinman`` carrying config.

    The router gets the service via an explicit ``ChatState`` (so the test
    is router-only for the service), but the handler still reaches
    ``app.state.corlinman.config`` for the ``[web].humanlike`` block.
    """
    app = FastAPI()
    app.include_router(router(ChatState(service=service)))

    class _AppState:
        def __init__(self, cfg: dict[str, Any] | None) -> None:
            self.config = cfg
            self.chat = service

    app.state.corlinman = _AppState(config)
    return app


@pytest.fixture()
def install_persona_store():
    """Install a fake persona store on the admin_a singleton + restore."""
    from corlinman_server.gateway.routes_admin_a import state as admin_state_mod

    saved = admin_state_mod._STATE

    def _install(store: Any) -> None:
        admin_state_mod.set_admin_state(_FakeAdminState(store))

    yield _install

    admin_state_mod._STATE = saved


def _body(persona_id: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "gpt-test",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
    }
    if persona_id is not None:
        body["persona_id"] = persona_id
    return body


# ─── Tests ────────────────────────────────────────────────────────────────────


def test_web_persona_injected_when_enabled(install_persona_store) -> None:
    """[web].humanlike enabled + bound persona → system message prepended."""
    svc = _RecordingChatService()
    install_persona_store(
        _FakePersonaStore({"grantley": _FakePersona("You are 格兰.")})
    )
    config = {"web": {"humanlike": {"enabled": True, "persona_id": "grantley"}}}
    app = _make_app(svc, config)

    with TestClient(app) as client:
        resp = client.post("/v1/chat/completions", json=_body())
    assert resp.status_code == 200

    req = svc.seen
    assert req is not None
    assert req.persona_id == "grantley"
    # Leading system message carries the persona body + trailing rule.
    assert req.messages[0].role == Role.SYSTEM
    assert "You are 格兰." in req.messages[0].content
    assert req.messages[0].content.endswith("---\n")
    # Original user turn preserved after the injected system message.
    assert req.messages[1].role == Role.USER
    assert req.messages[1].content == "hello"


def test_web_persona_text_model_binding_overrides_request_model(
    install_persona_store,
) -> None:
    """Persona text model binding wins over the web request's default model."""
    svc = _RecordingChatService()
    install_persona_store(
        _FakePersonaStore(
            {
                "grantley": _FakePersona(
                    "You are 格兰.",
                    model_bindings={
                        "text": {
                            "provider": "relay",
                            "model": "gpt-5.5",
                        }
                    },
                )
            }
        )
    )
    config = {"web": {"humanlike": {"enabled": True, "persona_id": "grantley"}}}
    app = _make_app(svc, config)

    with TestClient(app) as client:
        resp = client.post("/v1/chat/completions", json=_body())
    assert resp.status_code == 200

    req = svc.seen
    assert req is not None
    assert req.model == "gpt-5.5"
    assert req.provider_hint == "relay"
    assert resp.json()["model"] == "gpt-5.5"


def test_web_persona_skipped_when_disabled(install_persona_store) -> None:
    """Gate off → no injection; request reaches the service untouched."""
    svc = _RecordingChatService()
    install_persona_store(
        _FakePersonaStore({"grantley": _FakePersona("You are 格兰.")})
    )
    config = {"web": {"humanlike": {"enabled": False, "persona_id": "grantley"}}}
    app = _make_app(svc, config)

    with TestClient(app) as client:
        resp = client.post("/v1/chat/completions", json=_body())
    assert resp.status_code == 200

    req = svc.seen
    assert req is not None
    assert not req.persona_id
    assert len(req.messages) == 1
    assert req.messages[0].role == Role.USER


def test_explicit_persona_id_overrides_config(install_persona_store) -> None:
    """A body ``persona_id`` overrides the config default + force-enables."""
    svc = _RecordingChatService()
    install_persona_store(
        _FakePersonaStore(
            {
                "grantley": _FakePersona("You are 格兰."),
                "alt": _FakePersona("You are ALT."),
            }
        )
    )
    # Config gate is OFF and points at grantley — the body override wins.
    config = {"web": {"humanlike": {"enabled": False, "persona_id": "grantley"}}}
    app = _make_app(svc, config)

    with TestClient(app) as client:
        resp = client.post("/v1/chat/completions", json=_body(persona_id="alt"))
    assert resp.status_code == 200

    req = svc.seen
    assert req is not None
    assert req.persona_id == "alt"
    assert req.messages[0].role == Role.SYSTEM
    assert "You are ALT." in req.messages[0].content


def test_persona_header_overrides_config(install_persona_store) -> None:
    """An ``X-Persona-Id`` header overrides config + force-enables."""
    svc = _RecordingChatService()
    install_persona_store(
        _FakePersonaStore({"alt": _FakePersona("You are ALT.")})
    )
    config = {"web": {"humanlike": {"enabled": False}}}
    app = _make_app(svc, config)

    with TestClient(app) as client:
        resp = client.post(
            "/v1/chat/completions",
            json=_body(),
            headers={"X-Persona-Id": "alt"},
        )
    assert resp.status_code == 200

    req = svc.seen
    assert req is not None
    assert req.persona_id == "alt"
    assert "You are ALT." in req.messages[0].content


def test_missing_persona_row_degrades_silently(install_persona_store) -> None:
    """Bound persona_id with no matching row → no injection, no error."""
    svc = _RecordingChatService()
    install_persona_store(_FakePersonaStore({}))  # empty store
    config = {"web": {"humanlike": {"enabled": True, "persona_id": "ghost"}}}
    app = _make_app(svc, config)

    with TestClient(app) as client:
        resp = client.post("/v1/chat/completions", json=_body())
    assert resp.status_code == 200

    req = svc.seen
    assert req is not None
    assert not req.persona_id
    assert len(req.messages) == 1


def test_no_config_no_injection(install_persona_store) -> None:
    """No ``[web]`` config block at all → request untouched."""
    svc = _RecordingChatService()
    install_persona_store(
        _FakePersonaStore({"grantley": _FakePersona("You are 格兰.")})
    )
    app = _make_app(svc, config={})

    with TestClient(app) as client:
        resp = client.post("/v1/chat/completions", json=_body())
    assert resp.status_code == 200

    req = svc.seen
    assert req is not None
    assert not req.persona_id
    assert len(req.messages) == 1
