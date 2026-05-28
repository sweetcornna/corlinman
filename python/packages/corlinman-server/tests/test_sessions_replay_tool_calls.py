"""Replay endpoint passes journal ``tool_calls`` through to the UI.

When a chat session is replayed from the per-turn journal, assistant
messages that issued tool calls used to round-trip as ``{role,
content="", ts}`` — losing the tool_calls field entirely. The /chat
surface then rendered them as empty bubbles on session resume.

These tests pin the journal-replay path: tool_calls are surfaced in
the transcript, and a matching ``role="tool"`` row is folded into the
originating call's ``result`` field so the bubble can show both the
invocation and what came back.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Iterator
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from corlinman_server.agent_journal import AgentJournal
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


def _seed_journal(
    data_dir: Path,
    session_key: str,
    messages: list[dict[str, object]],
) -> None:
    """Open the journal at the standard ``<data_dir>/agent_journal.sqlite``
    path and append one turn with ``messages`` in the given seq order."""

    async def _run() -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        j = await AgentJournal.open(data_dir / "agent_journal.sqlite")
        try:
            tid = await j.begin_turn(session_key, "tool turn")
            for msg in messages:
                await j.append_message(
                    tid,
                    str(msg["role"]),
                    str(msg.get("content", "")),
                    tool_call_id=msg.get("tool_call_id"),  # type: ignore[arg-type]
                    tool_calls=msg.get("tool_calls"),
                )
            await j.complete_turn(tid)
        finally:
            await j.close()

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


def test_replay_passes_tool_calls_through(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_journal(
        tmp_path,
        "sess-tool",
        [
            {"role": "user", "content": "what's 2+2?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {
                            "name": "calculator",
                            "arguments": '{"expression":"2+2"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "content": '{"result":4}',
                "tool_call_id": "c1",
            },
            {"role": "assistant", "content": "It's 4."},
        ],
    )

    resp = client.post(
        "/admin/sessions/sess-tool/replay", json={"mode": "transcript"}
    )
    assert resp.status_code == 200, resp.text

    body = resp.json()
    # ``tool`` rows are not surfaced as their own transcript bubble; their
    # content is folded back into the originating call's ``result``.
    roles = [m["role"] for m in body["transcript"]]
    assert roles == ["user", "assistant", "assistant"]

    tool_assistant = body["transcript"][1]
    assert tool_assistant["content"] == ""
    assert isinstance(tool_assistant["tool_calls"], list)
    assert len(tool_assistant["tool_calls"]) == 1
    tc = tool_assistant["tool_calls"][0]
    assert tc["id"] == "c1"
    assert tc["function"]["name"] == "calculator"
    assert tc["function"]["arguments"] == '{"expression":"2+2"}'
    # The tool result is folded back onto the originating tool_call so
    # the chat UI can show invocation + result on resume.
    assert tc["result"] == '{"result":4}'

    # Plain text assistant rows don't carry a tool_calls field.
    assert "tool_calls" not in body["transcript"][2]
