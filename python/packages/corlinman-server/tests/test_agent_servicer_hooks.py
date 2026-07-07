"""Hook bus lifecycle events fire from the agent servicer's Chat RPC.

Pins down the wiring added in T-hooks-event-bus:

- ``UserPromptSubmit`` fires at chat-handler entry, before journal lookup.
- ``TurnComplete`` fires right before the terminal ``DoneEvent`` gRPC
  frame on the success path.
- ``TurnErrored`` fires right before the terminal ``ErrorEvent`` gRPC
  frame on the failure paths (the loop's ``ErrorEvent`` branch and the
  catch-all ``except Exception`` in the handler).

The servicer is exercised through a real in-process gRPC server so the
test also pins the actual frame ordering observers care about (admin
live feed gets the event before the corresponding wire frame).
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
    """Yields a pre-recorded ``ProviderChunk`` sequence on ``chat_stream``."""

    def __init__(self, chunks: list[ProviderChunk]) -> None:
        self._chunks = chunks

    async def chat_stream(self, **_kwargs: Any) -> AsyncIterator[ProviderChunk]:
        for c in self._chunks:
            yield c


class _FailingProvider:
    """Raises mid-stream so the reasoning loop emits an ``ErrorEvent``."""

    async def chat_stream(self, **_kwargs: Any) -> AsyncIterator[ProviderChunk]:
        # Yield one token then explode — exercises the loop's
        # ``except Exception`` branch in ``_run_one_round`` which maps
        # to an ``ErrorEvent``.
        yield ProviderChunk(kind="token", text="partial")
        raise RuntimeError("upstream blew up")
        yield  # pragma: no cover — unreachable, satisfies the async-gen type


def _token_stream(deltas: list[str], finish_reason: str = "stop") -> list[ProviderChunk]:
    chunks: list[ProviderChunk] = [ProviderChunk(kind="token", text=d) for d in deltas]
    chunks.append(
        ProviderChunk(
            kind="done",
            finish_reason=finish_reason,
        )
    )
    return chunks


async def _drive_chat(
    servicer: CorlinmanAgentServicer,
    *,
    model: str = "claude-sonnet-4-5",
    user_text: str = "hello",
    session_key: str = "sess-hook",
) -> tuple[list[str], list[str]]:
    """Run one Chat RPC against ``servicer`` and return ``(frame_kinds, tokens)``."""
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
            tokens: list[str] = []
            async for f in call:
                kind = f.WhichOneof("kind")
                frame_kinds.append(kind)
                if kind == "token":
                    tokens.append(f.token.text)
            return frame_kinds, tokens
    finally:
        await server.stop(grace=None)


@pytest.mark.asyncio
async def test_chat_emits_user_prompt_submit_then_turn_complete() -> None:
    """Success path: both lifecycle events fire, in order, exactly once."""

    bus = HookBus()
    seen: list[HookEvent] = []
    bus.subscribe(
        match_kind("UserPromptSubmit", "TurnComplete", "TurnErrored"),
        lambda ev: seen.append(ev),
    )

    def _resolver(_model: str) -> Any:
        return _FakeProvider(_token_stream(["hello ", "world"]))

    servicer = CorlinmanAgentServicer(
        provider_resolver=_resolver,
        hook_bus=bus,
    )

    frame_kinds, tokens = await _drive_chat(
        servicer,
        user_text="give me a haiku",
        session_key="sess-success",
    )

    # Sanity: the wire stream looks normal.
    assert "".join(tokens) == "hello world"
    assert frame_kinds[-1] == "done"

    kinds = [type(ev).__name__ for ev in seen]
    assert kinds == ["_UserPromptSubmit", "_TurnComplete"], (
        f"expected exactly UserPromptSubmit then TurnComplete, got {kinds!r}"
    )

    prompt_ev = seen[0]
    assert isinstance(prompt_ev, HookEvent.UserPromptSubmit)
    assert prompt_ev.user_text == "give me a haiku"
    assert prompt_ev.session_key() == "sess-success"
    assert prompt_ev.model  # populated by the resolver path

    done_ev = seen[1]
    assert isinstance(done_ev, HookEvent.TurnComplete)
    assert done_ev.session_key() == "sess-success"
    assert done_ev.finish_reason == "stop"
    assert done_ev.duration_ms >= 0


@pytest.mark.asyncio
async def test_chat_emits_turn_errored_on_provider_failure() -> None:
    """Failure path: ``TurnErrored`` fires; ``TurnComplete`` does not."""

    bus = HookBus()
    seen: list[HookEvent] = []
    bus.subscribe(
        match_kind("UserPromptSubmit", "TurnComplete", "TurnErrored"),
        lambda ev: seen.append(ev),
    )

    def _resolver(_model: str) -> Any:
        return _FailingProvider()

    servicer = CorlinmanAgentServicer(
        provider_resolver=_resolver,
        hook_bus=bus,
    )

    frame_kinds, _tokens = await _drive_chat(
        servicer,
        user_text="trigger the failure",
        session_key="sess-fail",
    )

    # Whatever the wire frames look like, the *last* one must be ``error``
    # — the reasoning loop maps provider exceptions to ``ErrorEvent`` which
    # the servicer translates to ``ServerFrame.error``.
    assert frame_kinds[-1] == "error", (
        f"expected last frame to be 'error', got {frame_kinds!r}"
    )

    kinds = [type(ev).__name__ for ev in seen]
    # UserPromptSubmit + TurnErrored, no TurnComplete.
    assert "_TurnErrored" in kinds, f"TurnErrored not emitted: {kinds!r}"
    assert "_TurnComplete" not in kinds, (
        f"TurnComplete must NOT fire on the error path: {kinds!r}"
    )
    assert kinds[0] == "_UserPromptSubmit", (
        f"UserPromptSubmit should fire first: {kinds!r}"
    )

    errored = next(ev for ev in seen if isinstance(ev, HookEvent.TurnErrored))
    assert errored.session_key() == "sess-fail"
    assert errored.reason  # populated from the loop / catch-all
    assert errored.message  # human-readable detail


@pytest.mark.asyncio
async def test_chat_without_hook_bus_still_completes() -> None:
    """A servicer constructed without ``hook_bus`` is a silent no-op path."""

    def _resolver(_model: str) -> Any:
        return _FakeProvider(_token_stream(["ok"]))

    # No hook_bus kwarg → ``self._hook_bus is None``; ``_emit_hook_event``
    # short-circuits without raising.
    servicer = CorlinmanAgentServicer(provider_resolver=_resolver)
    frame_kinds, tokens = await _drive_chat(servicer, user_text="hi")
    assert "".join(tokens) == "ok"
    assert frame_kinds[-1] == "done"


@pytest.mark.asyncio
async def test_chat_hook_subscriber_exception_does_not_break_stream() -> None:
    """A misbehaving subscriber must not tear down the chat RPC."""

    bus = HookBus()

    def angry(_ev: HookEvent) -> None:
        raise RuntimeError("subscriber boom")

    healthy_hits: list[str] = []
    bus.subscribe(match_kind("TurnComplete"), angry)
    bus.subscribe(
        match_kind("TurnComplete"),
        lambda ev: healthy_hits.append(ev.kind()),
    )

    def _resolver(_model: str) -> Any:
        return _FakeProvider(_token_stream(["a", "b"]))

    servicer = CorlinmanAgentServicer(
        provider_resolver=_resolver,
        hook_bus=bus,
    )

    frame_kinds, tokens = await _drive_chat(servicer, user_text="hello")

    # The stream still terminates with ``done`` — the angry subscriber's
    # exception was isolated by the bus.
    assert frame_kinds[-1] == "done"
    assert "".join(tokens) == "ab"
    # And the healthy subscriber still observed the event.
    assert healthy_hits == ["turn_complete"], (
        f"healthy subscriber must still receive TurnComplete: {healthy_hits!r}"
    )


# ---------------------------------------------------------------------------
# session_start (Dim 9 residuals) — once per session_key per process
# ---------------------------------------------------------------------------


class _EventRecordingRunner:
    """Hook-runner stand-in capturing ``run_event_async`` fires."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict, dict]] = []

    async def run_event_async(self, event, payload=None, ctx=None):
        from types import SimpleNamespace

        self.events.append((event, dict(payload or {}), dict(ctx or {})))
        return SimpleNamespace(allow=True, reason=None, inject_message=None)


@pytest.mark.asyncio
async def test_session_start_fires_once_per_session_key() -> None:
    runner = _EventRecordingRunner()

    def _resolver(_model: str) -> Any:
        return _FakeProvider(_token_stream(["hi"]))

    servicer = CorlinmanAgentServicer(
        provider_resolver=_resolver,
        hook_runner=runner,
    )

    await _drive_chat(servicer, user_text="one", session_key="sess-a")
    await _drive_chat(servicer, user_text="two", session_key="sess-a")
    await _drive_chat(servicer, user_text="three", session_key="sess-b")

    starts = [e for e in runner.events if e[0] == "session_start"]
    assert [e[2].get("session_key") for e in starts] == ["sess-a", "sess-b"]
