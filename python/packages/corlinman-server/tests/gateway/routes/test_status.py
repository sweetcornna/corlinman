"""Tests for the public agent status-card routes.

Covers: valid token → 200 snapshot (#28); tampered/expired token → 403;
journal read from app.state; redaction strips sensitive tool payloads by
default (#30) and can be disabled; absent journal degrades to an empty
snapshot rather than 500.
"""

from __future__ import annotations

from typing import Any

import pytest
from corlinman_server.gateway.routes import status as status_route
from corlinman_server.gateway.status_token import (
    make_status_token,
    resolve_signing_key,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _FakeJournal:
    """Minimal journal stub matching the methods the route calls."""

    def __init__(
        self,
        turns: list[dict[str, Any]],
        events: dict[Any, list[dict[str, Any]]],
    ):
        self._turns = turns
        self._events = events

    async def list_session_turns(self, session_key: str) -> list[dict[str, Any]]:
        return list(self._turns)

    async def get_session_turn_ids(
        self, session_key: str, *, limit: int = 50
    ) -> list[str]:
        return [str(t["turn_id"]) for t in self._turns]

    async def load_events(self, turn_id: Any) -> list[dict[str, Any]]:
        return list(
            self._events.get(str(turn_id), self._events.get(turn_id, []))
        )


def _client(journal: Any | None, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    # Pin the signing key so make/verify agree without touching disk
    # (resolve_signing_key checks CORLINMAN_STATUS_SIGNING_KEY first).
    monkeypatch.setenv("CORLINMAN_STATUS_SIGNING_KEY", "test-key")
    app = FastAPI()
    app.include_router(status_route.router())
    app.state.corlinman_journal = journal
    return TestClient(app)


def _token(session_key: str, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("CORLINMAN_STATUS_SIGNING_KEY", "test-key")
    return make_status_token(session_key, resolve_signing_key(None))


def test_valid_token_returns_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    journal = _FakeJournal(
        turns=[{"turn_id": "t1", "status": "complete", "elapsed_ms": 1200}],
        events={
            "t1": [
                {
                    "turn_id": "t1", "sequence": 0, "event_type": "TurnStarted",
                    "timestamp_ms": 1000, "payload": {},
                },
                {
                    "turn_id": "t1", "sequence": 1, "event_type": "TokenDelta",
                    "timestamp_ms": 1100, "payload": {"text": "hi"},
                },
            ]
        },
    )
    client = _client(journal, monkeypatch)
    tok = _token("tenant::sess-1", monkeypatch)

    resp = client.get(f"/status/{tok}/data")
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_key"] == "tenant::sess-1"
    assert body["status"] == "complete"
    assert len(body["turns"]) == 1
    assert len(body["events"]) == 2
    assert body["started_at_ms"] == 1000


def test_tampered_token_403(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(_FakeJournal([], {}), monkeypatch)
    resp = client.get("/status/not-a-real-token-xxxxxxxx/data")
    assert resp.status_code == 403
    assert resp.json()["error"] == "invalid_or_expired_token"


def test_expired_token_403(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORLINMAN_STATUS_SIGNING_KEY", "test-key")
    # Mint already-expired (now far in the past so exp < real now).
    tok = make_status_token(
        "tenant::sess", resolve_signing_key(None), ttl_seconds=1, now=1
    )
    client = _client(_FakeJournal([], {}), monkeypatch)
    resp = client.get(f"/status/{tok}/data")
    assert resp.status_code == 403


def test_redaction_strips_tool_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default redaction keeps the tool name + status but strips args/results."""
    journal = _FakeJournal(
        turns=[{"turn_id": "t1", "status": "complete"}],
        events={
            "t1": [
                {
                    "turn_id": "t1", "sequence": 0,
                    "event_type": "ToolStateCompleted", "timestamp_ms": 1000,
                    "payload": {
                        "tool": "web_search",
                        "status": "ok",
                        "args": {"query": "secret query"},
                        "result": "sensitive result text",
                    },
                }
            ]
        },
    )
    client = _client(journal, monkeypatch)
    tok = _token("tenant::sess", monkeypatch)
    body = client.get(f"/status/{tok}/data").json()
    payload = body["events"][0]["payload"]
    assert payload["tool"] == "web_search"  # structural key kept
    assert payload["status"] == "ok"
    assert "args" not in payload  # sensitive content stripped
    assert "result" not in payload
    assert payload["_redacted"] is True


def test_redaction_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORLINMAN_STATUS_REDACT", "0")
    journal = _FakeJournal(
        turns=[{"turn_id": "t1", "status": "complete"}],
        events={
            "t1": [
                {
                    "turn_id": "t1", "sequence": 0,
                    "event_type": "ToolStateCompleted", "timestamp_ms": 1000,
                    "payload": {"tool": "web_search", "args": {"q": "x"}},
                }
            ]
        },
    )
    client = _client(journal, monkeypatch)
    tok = _token("tenant::sess", monkeypatch)
    body = client.get(f"/status/{tok}/data").json()
    assert body["events"][0]["payload"]["args"] == {"q": "x"}


def test_journal_absent_returns_empty_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(None, monkeypatch)
    tok = _token("tenant::sess", monkeypatch)
    resp = client.get(f"/status/{tok}/data")
    assert resp.status_code == 200
    assert resp.json()["events"] == []
