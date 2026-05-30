"""Public status-card route tests.

These exercise the token-gated, no-admin surface that backs links minted by
``agent_status_card``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest_asyncio
from corlinman_agent.events import (
    BlockStart,
    BlockStop,
    EventEnvelope,
    ToolInputDelta,
    ToolStateCompleted,
    ToolStateRunning,
    TurnComplete,
)
from corlinman_server.agent_journal import AgentJournal
from corlinman_server.gateway.lifecycle.entrypoint import build_app
from corlinman_server.gateway.routes.register import (
    GatewayState,
    build_app_router,
)
from corlinman_server.gateway.status_token import (
    make_status_token,
    resolve_signing_key,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest_asyncio.fixture
async def journal(tmp_path: Path) -> AsyncIterator[AgentJournal]:
    j = await AgentJournal.open(tmp_path / "agent_journal.sqlite")
    try:
        yield j
    finally:
        await j.close()


def _client(*, journal: AgentJournal | None, data_dir: Path) -> TestClient:
    app_state = SimpleNamespace(journal=journal, data_dir=data_dir)
    app = FastAPI()
    app.include_router(build_app_router(GatewayState(app_state=app_state)))
    return TestClient(app)


def _token(session_key: str, data_dir: Path) -> str:
    return make_status_token(
        session_key,
        resolve_signing_key(data_dir),
    )


def _envelope(
    event: object,
    *,
    turn_id: int,
    sequence: int,
    session_key: str = "sess-public",
) -> EventEnvelope:
    return EventEnvelope(
        turn_id=str(turn_id),
        session_key=session_key,
        sequence=sequence,
        timestamp_ms=1_700_000_000_000 + sequence,
        event=event,
    )


async def test_public_status_returns_token_scoped_turns_and_current_step(
    tmp_path: Path,
    journal: AgentJournal,
    monkeypatch,
) -> None:
    monkeypatch.delenv("CORLINMAN_STATUS_SIGNING_KEY", raising=False)
    token = _token("sess-public", tmp_path)

    older = await journal.begin_turn("sess-public", "first request")
    assert older is not None
    await journal.complete_turn(older)

    current = await journal.begin_turn("sess-public", "current request")
    assert current is not None
    await journal.append_event(
        _envelope(
            ToolStateRunning(
                tool_call_id="call-running",
                tool_name="search_docs",
                args_json='{"query":"status card"}',
                started_at_ms=1_700_000_000_000,
            ),
            turn_id=current,
            sequence=0,
        )
    )
    await journal.append_event(
        _envelope(
            ToolStateCompleted(
                tool_call_id="call-finished",
                result_summary="done",
            ),
            turn_id=current,
            sequence=1,
        )
    )

    other = await journal.begin_turn("sess-private", "private request")
    assert other is not None

    client = _client(journal=journal, data_dir=tmp_path)
    resp = client.get(f"/status/{token}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["session_key"] == "sess-public"
    assert body["status"] == "in_progress"
    assert body["started_at_ms"] is not None
    assert body["last_activity_at_ms"] is not None
    assert [turn["turn_id"] for turn in body["turns"]] == [
        str(current),
        str(older),
    ]
    assert all("sess-private" not in str(turn) for turn in body["turns"])
    assert body["current_step"] == {
        "kind": "tool",
        "turn_id": str(current),
        "call_id": "call-running",
        "name": "search_docs",
        "event_type": "ToolStateRunning",
    }


async def test_public_status_reads_journal_added_after_router_mount(
    tmp_path: Path,
    journal: AgentJournal,
    monkeypatch,
) -> None:
    monkeypatch.delenv("CORLINMAN_STATUS_SIGNING_KEY", raising=False)
    token = _token("sess-late", tmp_path)
    turn_id = await journal.begin_turn("sess-late", "late journal")
    assert turn_id is not None

    app_state = SimpleNamespace(journal=None, data_dir=tmp_path)
    app = FastAPI()
    app.include_router(build_app_router(GatewayState(app_state=app_state)))

    app_state.journal = journal
    client = TestClient(app)
    resp = client.get(f"/status/{token}")

    assert resp.status_code == 200
    assert resp.json()["turns"][0]["turn_id"] == str(turn_id)


async def test_public_status_browser_request_serves_static_shell_but_json_stays_api(
    tmp_path: Path,
    journal: AgentJournal,
    monkeypatch,
) -> None:
    monkeypatch.delenv("CORLINMAN_STATUS_SIGNING_KEY", raising=False)
    ui_dir = tmp_path / "ui-static"
    (ui_dir / "status").mkdir(parents=True)
    (ui_dir / "status" / "__token__.html").write_text(
        "<main data-test='status-shell'>public status shell</main>",
        encoding="utf-8",
    )
    monkeypatch.setenv("CORLINMAN_UI_DIR", str(ui_dir))

    token = _token("sess-browser", tmp_path)
    turn_id = await journal.begin_turn("sess-browser", "browser request")
    assert turn_id is not None
    app = build_app(config_path=None, data_dir=tmp_path)
    app.state.corlinman_journal = journal
    app.state.corlinman_state.journal = journal
    client = TestClient(app)

    html = client.get(f"/status/{token}", headers={"accept": "text/html"})

    assert html.status_code == 200, html.text
    assert "text/html" in html.headers["content-type"]
    assert "public status shell" in html.text

    json_resp = client.get(
        f"/status/{token}?format=json",
        headers={"accept": "text/html"},
    )

    assert json_resp.status_code == 200
    assert json_resp.json()["turns"][0]["turn_id"] == str(turn_id)


async def test_public_status_events_returns_token_scoped_replay(
    tmp_path: Path,
    journal: AgentJournal,
    monkeypatch,
) -> None:
    monkeypatch.delenv("CORLINMAN_STATUS_SIGNING_KEY", raising=False)
    token = _token("sess-public", tmp_path)
    turn_id = await journal.begin_turn("sess-public", "needs trajectory")
    assert turn_id is not None
    private_turn = await journal.begin_turn("sess-private", "private")
    assert private_turn is not None

    await journal.append_event(
        _envelope(
            BlockStart(
                index=0,
                block_type="tool_use",
                tool_name="search_docs",
                tool_call_id="call-replay",
            ),
            turn_id=turn_id,
            sequence=0,
        )
    )
    await journal.append_event(
        _envelope(
            ToolInputDelta(index=0, partial_json='{"query":"status card"}'),
            turn_id=turn_id,
            sequence=1,
        )
    )
    await journal.append_event(
        _envelope(
            BlockStop(index=0, elapsed_ms=5),
            turn_id=turn_id,
            sequence=2,
        )
    )
    await journal.append_event(
        _envelope(
            ToolStateRunning(
                tool_call_id="call-replay",
                tool_name="search_docs",
                args_json='{"query":"status card"}',
                started_at_ms=1_700_000_000_010,
            ),
            turn_id=turn_id,
            sequence=3,
        )
    )
    await journal.append_event(
        _envelope(
            ToolStateCompleted(
                tool_call_id="call-replay",
                result_summary="found notes",
                elapsed_ms=25,
            ),
            turn_id=turn_id,
            sequence=4,
        )
    )
    await journal.append_event(
        _envelope(
            TurnComplete(finish_reason="stop", usage={}, elapsed_ms=30),
            turn_id=turn_id,
            sequence=5,
        )
    )
    await journal.append_event(
        _envelope(
            ToolStateRunning(
                tool_call_id="private-call",
                tool_name="read_secret",
                args_json="{}",
                started_at_ms=1_700_000_000_020,
            ),
            turn_id=private_turn,
            sequence=0,
            session_key="sess-private",
        )
    )

    client = _client(journal=journal, data_dir=tmp_path)
    resp = client.get(f"/status/{token}/events")

    assert resp.status_code == 200
    body = resp.json()
    assert body["session_key"] == "sess-public"
    assert [event["event_type"] for event in body["events"]] == [
        "BlockStart",
        "ToolInputDelta",
        "BlockStop",
        "ToolStateRunning",
        "ToolStateCompleted",
        "TurnComplete",
    ]
    assert {event["session_key"] for event in body["events"]} == {"sess-public"}
    assert "private-call" not in str(body)


async def test_public_status_is_mounted_on_full_gateway_without_auth(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("CORLINMAN_STATUS_SIGNING_KEY", raising=False)
    token = _token("sess-full-gateway", tmp_path)
    seed = await AgentJournal.open(tmp_path / "agent_journal.sqlite")
    try:
        turn_id = await seed.begin_turn("sess-full-gateway", "full app")
        assert turn_id is not None
    finally:
        await seed.close()

    app = build_app(config_path=None, data_dir=tmp_path)
    with TestClient(app) as client:
        resp = client.get(f"/status/{token}")

    assert resp.status_code == 200
    assert resp.json()["turns"][0]["turn_id"] == str(turn_id)


def test_public_status_rejects_invalid_token(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CORLINMAN_STATUS_SIGNING_KEY", raising=False)
    client = _client(journal=None, data_dir=tmp_path)

    resp = client.get("/status/not-a-token")

    assert resp.status_code == 403
    assert resp.json()["detail"]["error"] == "invalid_status_token"


def test_public_status_without_journal_returns_503(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("CORLINMAN_STATUS_SIGNING_KEY", raising=False)
    token = _token("sess-public", tmp_path)
    client = _client(journal=None, data_dir=tmp_path)

    resp = client.get(f"/status/{token}")

    assert resp.status_code == 503
    assert resp.json()["detail"]["error"] == "observability_disabled"
