"""Tests for the P2 ``DirectProviderBackend`` + ``chat_bootstrap`` seam.

Exercises the provider-chunk → ``ServerFrame`` translation end-to-end
through a real :class:`ChatService`, using a scripted in-memory provider
so no network / credentials are involved.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from corlinman_grpc._generated.corlinman.v1 import agent_pb2, common_pb2
from corlinman_server.gateway.services import (
    ChatService,
    DirectProviderBackend,
    build_chat_service,
)
from corlinman_server.gateway_api import (
    DoneEvent,
    ErrorEvent,
    InternalChatRequest,
    Message,
    Role,
    TokenDeltaEvent,
    ToolCallEvent,
)

# ─── scripted providers ──────────────────────────────────────────────


@dataclass
class _Chunk:
    """Stand-in for corlinman_providers.base.ProviderChunk."""

    kind: str
    text: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments_delta: str | None = None
    finish_reason: str | None = None


class _ScriptedProvider:
    """Provider that yields a fixed list of chunks."""

    name = "scripted"

    def __init__(self, chunks: list[_Chunk]) -> None:
        self._chunks = chunks

    async def chat_stream(self, **_kw: object) -> AsyncIterator[_Chunk]:
        for c in self._chunks:
            yield c


class _RaisingProvider:
    name = "raising"

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def chat_stream(self, **_kw: object) -> AsyncIterator[_Chunk]:
        if False:  # pragma: no cover — make this an async generator
            yield _Chunk(kind="token")
        raise self._exc


class _StubRegistry:
    """Minimal ProviderRegistry: maps model id → provider."""

    def __init__(self, provider: object, model: str = "x") -> None:
        self._provider = provider
        self._model = model

    def resolve(self, alias_or_model: str, *, aliases=None, provider_hint=None):
        if alias_or_model in (self._model, "scripted", "raising"):
            return self._provider, alias_or_model, {}
        raise KeyError(f"no provider for {alias_or_model!r}")


def _start(model: str) -> agent_pb2.ChatStart:
    return agent_pb2.ChatStart(
        model=model,
        messages=[common_pb2.Message(role=common_pb2.USER, content="hi")],
        stream=False,
    )


def _req(model: str) -> InternalChatRequest:
    return InternalChatRequest(
        model=model,
        messages=[Message(role=Role.USER, content="hi")],
        session_key="",
        stream=False,
    )


# ─── DirectProviderBackend.start: frame translation ──────────────────


@pytest.mark.asyncio
async def test_token_and_done_translate_to_frames() -> None:
    provider = _ScriptedProvider(
        [
            _Chunk(kind="token", text="hello "),
            _Chunk(kind="token", text="world"),
            _Chunk(kind="done", finish_reason="stop"),
        ]
    )
    backend = DirectProviderBackend(_StubRegistry(provider, "x"))
    _tx, rx = await backend.start(_start("x"))

    frames = [f async for f in rx]
    kinds = [f.WhichOneof("kind") for f in frames]
    assert kinds == ["token", "token", "done"]
    assert frames[0].token.text == "hello "
    assert frames[1].token.text == "world"
    assert frames[2].done.finish_reason == "stop"


@pytest.mark.asyncio
async def test_tool_call_chunks_buffer_into_one_frame() -> None:
    provider = _ScriptedProvider(
        [
            _Chunk(kind="tool_call_start", tool_call_id="c1", tool_name="search"),
            _Chunk(kind="tool_call_delta", tool_call_id="c1", arguments_delta='{"q":'),
            _Chunk(kind="tool_call_delta", tool_call_id="c1", arguments_delta='"hi"}'),
            _Chunk(kind="tool_call_end", tool_call_id="c1"),
            _Chunk(kind="done", finish_reason="tool_calls"),
        ]
    )
    backend = DirectProviderBackend(_StubRegistry(provider, "x"))
    _tx, rx = await backend.start(_start("x"))

    frames = [f async for f in rx]
    kinds = [f.WhichOneof("kind") for f in frames]
    assert kinds == ["tool_call", "done"]
    tc = frames[0].tool_call
    assert tc.call_id == "c1"
    assert tc.tool == "search"
    assert tc.args_json == b'{"q":"hi"}'


@pytest.mark.asyncio
async def test_unknown_model_yields_error_frame() -> None:
    backend = DirectProviderBackend(_StubRegistry(_ScriptedProvider([]), "x"))
    _tx, rx = await backend.start(_start("nonexistent-model"))

    frames = [f async for f in rx]
    assert [f.WhichOneof("kind") for f in frames] == ["error"]


@pytest.mark.asyncio
async def test_provider_exception_yields_error_frame() -> None:
    backend = DirectProviderBackend(
        _StubRegistry(_RaisingProvider(RuntimeError("boom")), "raising")
    )
    _tx, rx = await backend.start(_start("raising"))

    frames = [f async for f in rx]
    assert [f.WhichOneof("kind") for f in frames] == ["error"]
    assert "boom" in frames[0].error.message


@pytest.mark.asyncio
async def test_stream_without_done_synthesises_terminal() -> None:
    provider = _ScriptedProvider([_Chunk(kind="token", text="x")])
    backend = DirectProviderBackend(_StubRegistry(provider, "x"))
    _tx, rx = await backend.start(_start("x"))

    frames = [f async for f in rx]
    assert [f.WhichOneof("kind") for f in frames] == ["token", "done"]


# ─── tool-call arg assembler: correctness + O(K) accumulation ────────


@pytest.mark.asyncio
async def test_many_arg_deltas_assemble_byte_identical() -> None:
    """A long stream of ``tool_call_delta`` fragments must assemble into
    the exact byte-for-byte concatenation of every fragment.

    Guards the streaming arg assembler: the buffered ``arguments_delta``
    fragments must join in order with nothing dropped, duplicated, or
    reordered, regardless of how the buffer is represented internally.
    """
    frags = [f'{{"i":{i}}}'.ljust(50) for i in range(4000)]
    expected = "".join(frags)

    chunks = [_Chunk(kind="tool_call_start", tool_call_id="c1", tool_name="big")]
    chunks += [
        _Chunk(kind="tool_call_delta", tool_call_id="c1", arguments_delta=f)
        for f in frags
    ]
    chunks += [
        _Chunk(kind="tool_call_end", tool_call_id="c1"),
        _Chunk(kind="done", finish_reason="tool_calls"),
    ]

    provider = _ScriptedProvider(chunks)
    backend = DirectProviderBackend(_StubRegistry(provider, "x"))
    _tx, rx = await backend.start(_start("x"))

    frames = [f async for f in rx]
    tool_frames = [f for f in frames if f.WhichOneof("kind") == "tool_call"]
    assert len(tool_frames) == 1
    assert tool_frames[0].tool_call.args_json.decode("utf-8") == expected


@pytest.mark.asyncio
async def test_arg_assembler_is_linear_not_quadratic() -> None:
    """Perf-smoke: assembling many sizeable arg fragments stays cheap.

    String ``+=`` accumulation held in a container slot is O(K^2) in the
    total argument size (each ``+=`` reallocates and recopies the whole
    accumulated buffer). Joining buffered fragments once is O(K).

    With 8000 × 2KB fragments (~16 MB of arguments) the quadratic path
    measured ~2.1 s on the dev box while the linear ``''.join`` path
    stays ~6 ms — a >300x gap. The 1.0 s bound sits well inside that gap
    so it trips on the O(K^2) regression yet never on CI jitter.
    """
    import time

    frag = "x" * 2048
    n = 8000
    chunks = [_Chunk(kind="tool_call_start", tool_call_id="c1", tool_name="big")]
    chunks += [
        _Chunk(kind="tool_call_delta", tool_call_id="c1", arguments_delta=frag)
        for _ in range(n)
    ]
    chunks += [
        _Chunk(kind="tool_call_end", tool_call_id="c1"),
        _Chunk(kind="done", finish_reason="tool_calls"),
    ]

    provider = _ScriptedProvider(chunks)
    backend = DirectProviderBackend(_StubRegistry(provider, "x"))

    t0 = time.perf_counter()
    _tx, rx = await backend.start(_start("x"))
    frames = [f async for f in rx]
    elapsed = time.perf_counter() - t0

    tool_frames = [f for f in frames if f.WhichOneof("kind") == "tool_call"]
    assert len(tool_frames) == 1
    assert len(tool_frames[0].tool_call.args_json) == n * len(frag)
    assert elapsed < 1.0, f"arg assembly took {elapsed * 1000:.0f}ms (expected O(K))"


# ─── End-to-end through ChatService ──────────────────────────────────


@pytest.mark.asyncio
async def test_chat_service_nonstream_completion() -> None:
    provider = _ScriptedProvider(
        [
            _Chunk(kind="token", text="four"),
            _Chunk(kind="done", finish_reason="stop"),
        ]
    )
    service = ChatService(DirectProviderBackend(_StubRegistry(provider, "x")))
    cancel = asyncio.Event()

    events = [ev async for ev in service.run(_req("x"), cancel)]
    texts = [e.text for e in events if isinstance(e, TokenDeltaEvent)]
    assert "".join(texts) == "four"
    assert isinstance(events[-1], DoneEvent)
    assert events[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_chat_service_surfaces_tool_calls() -> None:
    provider = _ScriptedProvider(
        [
            _Chunk(kind="tool_call_start", tool_call_id="c1", tool_name="calc"),
            _Chunk(kind="tool_call_delta", tool_call_id="c1", arguments_delta="{}"),
            _Chunk(kind="tool_call_end", tool_call_id="c1"),
            _Chunk(kind="done", finish_reason="tool_calls"),
        ]
    )
    service = ChatService(DirectProviderBackend(_StubRegistry(provider, "x")))
    events = [ev async for ev in service.run(_req("x"), asyncio.Event())]
    tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
    assert len(tool_events) == 1
    assert tool_events[0].tool == "calc"
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_chat_service_error_event_on_unknown_model() -> None:
    service = ChatService(DirectProviderBackend(_StubRegistry(_ScriptedProvider([]), "x")))
    events = [ev async for ev in service.run(_req("unknown"), asyncio.Event())]
    assert isinstance(events[-1], ErrorEvent)


# ─── End-to-end with the real MockProvider + ProviderRegistry ────────


@pytest.mark.asyncio
async def test_real_mock_provider_through_registry() -> None:
    from corlinman_providers.registry import ProviderRegistry
    from corlinman_providers.specs import ProviderKind, ProviderSpec

    registry = ProviderRegistry(
        [ProviderSpec(name="mock", kind=ProviderKind.MOCK)]
    )
    backend = DirectProviderBackend(registry, models_config={})
    service = ChatService(backend)

    events = [ev async for ev in service.run(_req("mock"), asyncio.Event())]
    text = "".join(e.text for e in events if isinstance(e, TokenDeltaEvent))
    # MockProvider echoes the reversed last user message after a preamble.
    assert text  # non-empty real completion
    assert isinstance(events[-1], DoneEvent)


# ─── build_chat_service / bootstrap seam ─────────────────────────────


def test_build_chat_service_none_without_registry() -> None:
    @dataclass
    class _State:
        provider_registry: object | None = None
        config: dict | None = None
        chat: object | None = None

    assert build_chat_service(_State()) is None


def test_bootstrap_attaches_chat_when_registry_present() -> None:
    from corlinman_providers.registry import ProviderRegistry
    from corlinman_server.gateway.services import bootstrap

    @dataclass
    class _State:
        provider_registry: object | None = None
        config: dict | None = None
        chat: object | None = None

    state = _State(provider_registry=ProviderRegistry([]), config={})
    bootstrap(state)
    assert isinstance(state.chat, ChatService)


def test_bootstrap_is_idempotent() -> None:
    from corlinman_providers.registry import ProviderRegistry
    from corlinman_server.gateway.services import bootstrap

    @dataclass
    class _State:
        provider_registry: object | None = None
        config: dict | None = None
        chat: object | None = None

    sentinel = object()
    state = _State(provider_registry=ProviderRegistry([]), config={}, chat=sentinel)
    bootstrap(state)
    assert state.chat is sentinel  # not clobbered
