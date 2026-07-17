"""Memory W0 — ``[memory.recall]`` config plumbing + shared-host preference.

The conversational recall knobs (recent-turn count, notes top_k, query
char cap) were hardcoded in the servicer. W0 publishes them as
``state.memory_recall_config`` from the ``[memory.recall]`` TOML section
and has the servicer read them with the legacy values as defaults; it
also makes ``_get_memory_host`` prefer the gateway-shared
``app_state.memory_host`` so all lanes write through one connection.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_server.agent_servicer import CorlinmanAgentServicer


class _FakeProvider:
    def __init__(self) -> None:  # pragma: no cover — never streamed here
        pass


class _RecordingHost:
    """Fake host recording ``recent`` limits and ``query`` requests."""

    def __init__(self) -> None:
        self.recent_calls: list[tuple[str, int]] = []
        self.queries: list[Any] = []

    async def recent(self, session_key: str, limit: int) -> list[Any]:
        self.recent_calls.append((session_key, limit))
        return [SimpleNamespace(content="remembered fact")]

    async def query(self, req: Any) -> list[Any]:
        self.queries.append(req)
        return []


def _servicer() -> CorlinmanAgentServicer:
    return CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider())


def test_recall_config_defaults_without_app_state() -> None:
    servicer = _servicer()
    assert servicer._memory_recall_config() == {
        "recent_turns": 8,
        "notes_top_k": 4,
        "query_chars": 500,
    }


def test_recall_config_reads_app_state_and_sanitises() -> None:
    servicer = _servicer()
    servicer.set_app_state(
        SimpleNamespace(
            memory_recall_config={
                "recent_turns": 12,
                "notes_top_k": "6",  # string ints are accepted
                "query_chars": -5,  # negative → legacy default
                "unknown_key": 99,  # ignored
            }
        )
    )
    assert servicer._memory_recall_config() == {
        "recent_turns": 12,
        "notes_top_k": 6,
        "query_chars": 500,
    }


async def test_recall_and_notes_lanes_honor_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from corlinman_agent.reasoning_loop import ChatStart as _AgentChatStart

    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    servicer = _servicer()
    host = _RecordingHost()
    servicer.set_app_state(
        SimpleNamespace(
            memory_host=host,
            memory_recall_config={
                "recent_turns": 3,
                "notes_top_k": 2,
                "query_chars": 7,
            },
        )
    )
    try:
        start = _AgentChatStart(
            model="m",
            messages=[{"role": "user", "content": "alpha beta gamma delta"}],
            session_key="s1",
        )
        await servicer._recall_memory(start)

        assert host.recent_calls == [("s1", 3)]
        assert len(host.queries) == 1
        req = host.queries[0]
        assert req.top_k == 2
        assert req.text == "alpha b"  # capped at query_chars=7
    finally:
        await servicer.aclose()


async def test_get_memory_host_prefers_shared_app_state_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    servicer = _servicer()
    shared = _RecordingHost()
    servicer.set_app_state(SimpleNamespace(memory_host=shared))
    try:
        assert await servicer._get_memory_host() is shared
        # The lazy self-open path must not have been taken.
        assert servicer._memory_host is None
        assert servicer._memory_init_done is False
    finally:
        await servicer.aclose()


async def test_wire_c2_publishes_memory_recall_config(tmp_path: Path) -> None:
    from corlinman_server.gateway.core.state import AppState
    from corlinman_server.gateway.lifecycle.entrypoint import _wire_c2_handles

    state = AppState()
    state.data_dir = tmp_path
    admin_a = SimpleNamespace(identity_store=None, persona_resolver=None)
    app = SimpleNamespace(state=SimpleNamespace())

    cfg = {"memory": {"recall": {"recent_turns": 10}}}
    await _wire_c2_handles(app, state, admin_a, tmp_path, cfg=cfg)
    assert state.memory_recall_config == {"recent_turns": 10}

    # Absent section → empty dict (servicer applies legacy defaults).
    state2 = AppState()
    state2.data_dir = tmp_path
    await _wire_c2_handles(app, state2, admin_a, tmp_path, cfg={})
    assert state2.memory_recall_config == {}
