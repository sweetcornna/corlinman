"""Hook events carry the journal ``turn_id`` for every chat-handler exit.

R1-002 bug: in three branches of ``CorlinmanAgentServicer.Chat`` the local
``journal_turn_id`` is reset to ``None`` BEFORE being passed into the
``HookEvent.TurnComplete`` / ``HookEvent.TurnErrored`` constructor, so
every subscriber sees ``turn_id=None`` and the audit trail is broken.

These tests cover the three branches:

1. Successful turn → ``TurnComplete`` must carry the real journal row id.
2. Provider failure → ``ErrorEvent`` branch → ``TurnErrored`` must carry
   the real journal row id.
3. Catch-all ``except Exception`` in the handler → ``TurnErrored`` must
   carry the real journal row id.

They drive the servicer through the same in-process gRPC harness used by
``test_agent_servicer_hooks.py`` so the wiring exercised matches prod.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import grpc
import grpc.aio
import pytest
from corlinman_grpc import agent_pb2, agent_pb2_grpc, common_pb2
from corlinman_hooks import HookBus, HookEvent, match_kind
from corlinman_providers.base import ProviderChunk
from corlinman_server.agent_servicer import CorlinmanAgentServicer


class _FakeProvider:
    def __init__(self, chunks: list[ProviderChunk]) -> None:
        self._chunks = chunks

    async def chat_stream(self, **_kwargs: Any) -> AsyncIterator[ProviderChunk]:
        for c in self._chunks:
            yield c


class _FailingProvider:
    """Yields a token, then raises — drives the loop's ``ErrorEvent`` branch."""

    async def chat_stream(self, **_kwargs: Any) -> AsyncIterator[ProviderChunk]:
        yield ProviderChunk(kind="token", text="partial")
        raise RuntimeError("upstream blew up")
        yield  # pragma: no cover


def _token_stream(deltas: list[str], finish_reason: str = "stop") -> list[ProviderChunk]:
    chunks: list[ProviderChunk] = [ProviderChunk(kind="token", text=d) for d in deltas]
    chunks.append(ProviderChunk(kind="done", finish_reason=finish_reason))
    return chunks


async def _drive_chat(
    servicer: CorlinmanAgentServicer,
    *,
    user_text: str,
    session_key: str,
    model: str = "claude-sonnet-4-5",
) -> list[str]:
    server = grpc.aio.server()
    agent_pb2_grpc.add_AgentServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = agent_pb2_grpc.AgentStub(channel)

            async def frames():
                yield agent_pb2.ClientFrame(
                    start=agent_pb2.ChatStart(
                        model=model,
                        session_key=session_key,
                        messages=[
                            common_pb2.Message(
                                role=common_pb2.USER, content=user_text
                            )
                        ],
                    )
                )

            call = stub.Chat(frames())
            frame_kinds: list[str] = []
            async for f in call:
                frame_kinds.append(f.WhichOneof("kind"))
            return frame_kinds
    finally:
        await server.stop(grace=None)


@pytest.mark.asyncio
async def test_turn_complete_carries_journal_turn_id() -> None:
    """Success path — ``TurnComplete.turn_id`` is the real journal row id."""

    bus = HookBus()
    seen: list[HookEvent] = []
    bus.subscribe(match_kind("TurnComplete"), lambda ev: seen.append(ev))

    def _resolver(_model: str) -> Any:
        return _FakeProvider(_token_stream(["hi"]))

    servicer = CorlinmanAgentServicer(provider_resolver=_resolver, hook_bus=bus)
    frame_kinds = await _drive_chat(
        servicer, user_text="hi", session_key="sess-r1-002-ok"
    )
    assert frame_kinds[-1] == "done"

    assert len(seen) == 1, f"expected exactly one TurnComplete, got {seen!r}"
    ev = seen[0]
    assert isinstance(ev, HookEvent.TurnComplete)
    assert ev.turn_id is not None, (
        "TurnComplete.turn_id was None — R1-002 BUG-002 not fixed "
        "(journal_turn_id cleared before hook emit)"
    )
    assert isinstance(ev.turn_id, int)
    assert ev.turn_id > 0


@pytest.mark.asyncio
async def test_turn_errored_from_error_event_carries_journal_turn_id() -> None:
    """Loop ``ErrorEvent`` branch — ``TurnErrored.turn_id`` is the row id."""

    bus = HookBus()
    seen: list[HookEvent] = []
    bus.subscribe(match_kind("TurnErrored"), lambda ev: seen.append(ev))

    def _resolver(_model: str) -> Any:
        return _FailingProvider()

    servicer = CorlinmanAgentServicer(provider_resolver=_resolver, hook_bus=bus)
    frame_kinds = await _drive_chat(
        servicer, user_text="boom", session_key="sess-r1-002-err"
    )
    assert frame_kinds[-1] == "error"

    assert len(seen) == 1, f"expected exactly one TurnErrored, got {seen!r}"
    ev = seen[0]
    assert isinstance(ev, HookEvent.TurnErrored)
    assert ev.turn_id is not None, (
        "TurnErrored.turn_id was None — R1-002 BUG-001 not fixed "
        "(journal_turn_id cleared before hook emit in ErrorEvent branch)"
    )
    assert isinstance(ev.turn_id, int)
    assert ev.turn_id > 0


@pytest.mark.asyncio
async def test_turn_errored_from_catch_all_carries_journal_turn_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handler catch-all ``except`` — ``TurnErrored.turn_id`` is the row id.

    The reasoning loop converts every in-loop exception into an
    ``ErrorEvent``, so to reach the chat handler's outer ``except`` we
    have to raise from servicer-internal code that runs INSIDE the
    ``async for event in loop.run(...)`` body — after ``begin_turn`` has
    assigned a real ``journal_turn_id``. We patch ``_store_memory`` to
    raise; it's called on the DoneEvent path before any of the in-branch
    ``journal_turn_id = None`` clears, so the catch-all sees the real
    row id (and must forward it to the hook).
    """

    bus = HookBus()
    seen: list[HookEvent] = []
    bus.subscribe(match_kind("TurnErrored"), lambda ev: seen.append(ev))

    def _resolver(_model: str) -> Any:
        return _FakeProvider(_token_stream(["done"]))

    servicer = CorlinmanAgentServicer(provider_resolver=_resolver, hook_bus=bus)

    async def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("memory store kaboom")

    monkeypatch.setattr(servicer, "_store_memory", _boom)

    frame_kinds = await _drive_chat(
        servicer, user_text="explode", session_key="sess-r1-002-fatal"
    )
    assert frame_kinds[-1] == "error"

    assert seen, f"expected at least one TurnErrored, got {seen!r}"
    ev = seen[-1]
    assert isinstance(ev, HookEvent.TurnErrored)
    assert ev.turn_id is not None, (
        "TurnErrored.turn_id was None — R1-002 BUG-003 not fixed "
        "(journal_turn_id cleared before hook emit in catch-all)"
    )
    assert isinstance(ev.turn_id, int)
    assert ev.turn_id > 0
