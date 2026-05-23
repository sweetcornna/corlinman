"""Full-stack servicer test: real gRPC server, fake provider, verify frames."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import grpc
import grpc.aio
import pytest
from corlinman_grpc import agent_pb2, agent_pb2_grpc, common_pb2
from corlinman_providers import AliasEntry, ProviderRegistry
from corlinman_providers.base import ProviderChunk
from corlinman_server.agent_servicer import CorlinmanAgentServicer


class _FakeProvider:
    """Yields a pre-recorded ``ProviderChunk`` sequence.

    Records the kwargs passed to ``chat_stream`` on ``last_kwargs`` so
    tests can assert that merged params flow through.
    """

    def __init__(self, chunks: list[ProviderChunk]) -> None:
        self._chunks = chunks
        self.last_kwargs: dict[str, Any] = {}

    async def chat_stream(self, **kwargs: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        self.last_kwargs = kwargs
        for c in self._chunks:
            yield c


class _FakeContextAssembler:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def assemble(
        self,
        messages: list[dict[str, Any]],
        *,
        session_key: str,
        model_name: str,
        metadata: dict[str, str] | None = None,
    ) -> Any:
        self.calls.append(
            {
                "messages": messages,
                "session_key": session_key,
                "model_name": model_name,
                "metadata": dict(metadata or {}),
            }
        )
        rendered = [dict(m) for m in messages]
        for msg in rendered:
            if msg.get("role") == "system" and isinstance(msg.get("content"), str):
                msg["content"] = msg["content"].replace(
                    "{{memory.backend}}", "memory hit from assembler"
                )
        return SimpleNamespace(messages=rendered)


def _token_stream(deltas: list[str]) -> list[ProviderChunk]:
    """Helper: token chunks + final ``done``."""
    chunks: list[ProviderChunk] = [ProviderChunk(kind="token", text=d) for d in deltas]
    chunks.append(ProviderChunk(kind="done", finish_reason="stop"))
    return chunks


@pytest.mark.asyncio
async def test_servicer_streams_tokens_and_done() -> None:
    def _resolver(_model: str) -> Any:
        return _FakeProvider(_token_stream(["hello ", "world"]))

    servicer = CorlinmanAgentServicer(provider_resolver=_resolver)

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
                        model="claude-sonnet-4-5",
                        messages=[
                            common_pb2.Message(role=common_pb2.USER, content="hi")
                        ],
                    )
                )

            call = stub.Chat(frames())
            received: list[str] = []
            kinds: list[str] = []
            async for f in call:
                kinds.append(f.WhichOneof("kind"))
                if f.WhichOneof("kind") == "token":
                    received.append(f.token.text)
            assert "".join(received) == "hello world"
            assert kinds[-1] == "done"
    finally:
        await server.stop(grace=None)


@pytest.mark.asyncio
async def test_env_mock_provider_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CORLINMAN_TEST_MOCK_PROVIDER`` activates the offline provider."""
    monkeypatch.setenv("CORLINMAN_TEST_MOCK_PROVIDER", "mock-delta")
    servicer = CorlinmanAgentServicer()
    server = grpc.aio.server()
    agent_pb2_grpc.add_AgentServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = agent_pb2_grpc.AgentStub(channel)

            async def frames():
                yield agent_pb2.ClientFrame(
                    start=agent_pb2.ChatStart(model="any-model")
                )

            call = stub.Chat(frames())
            texts: list[str] = []
            async for f in call:
                if f.WhichOneof("kind") == "token":
                    texts.append(f.token.text)
            assert "".join(texts) == "mock-delta"
    finally:
        await server.stop(grace=None)


@pytest.mark.asyncio
async def test_servicer_emits_tool_call_frame() -> None:
    """Provider emits OpenAI-standard tool_call chunks → servicer yields ToolCall frame."""
    chunks = [
        ProviderChunk(
            kind="tool_call_start",
            tool_call_id="call_1",
            tool_name="foo.greet",
        ),
        ProviderChunk(
            kind="tool_call_delta",
            tool_call_id="call_1",
            arguments_delta='{"name":',
        ),
        ProviderChunk(
            kind="tool_call_delta",
            tool_call_id="call_1",
            arguments_delta='"Ada"}',
        ),
        ProviderChunk(kind="tool_call_end", tool_call_id="call_1"),
        ProviderChunk(kind="done", finish_reason="tool_calls"),
    ]

    def _resolver(_model: str) -> Any:
        return _FakeProvider(chunks)

    servicer = CorlinmanAgentServicer(provider_resolver=_resolver)
    server = grpc.aio.server()
    agent_pb2_grpc.add_AgentServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = agent_pb2_grpc.AgentStub(channel)

            async def frames():
                yield agent_pb2.ClientFrame(
                    start=agent_pb2.ChatStart(model="claude-sonnet-4-5")
                )

            call = stub.Chat(frames())
            tool_names: list[str] = []
            async for f in call:
                if f.WhichOneof("kind") == "tool_call":
                    tool_names.append(f.tool_call.tool)
            # tool_name "foo.greet" → ToolCall.tool = "greet" (plugin/tool split) or full string;
            # the servicer layer is free to split on ".", we accept either form.
            assert tool_names and any("greet" in n for n in tool_names)
    finally:
        await server.stop(grace=None)


@pytest.mark.asyncio
async def test_servicer_threads_merged_params_into_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feature C: alias params flow through to the provider's ``chat_stream``.

    Configure a registry with one provider (default ``temperature``) and one
    alias that overrides ``temperature`` + adds ``top_p``. The servicer
    should call the provider with the alias's ``temperature`` and with
    ``top_p`` threaded through ``extra``, and with the **upstream** model
    id rather than the alias name.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    fake = _FakeProvider(_token_stream(["ok"]))

    def _resolver(
        alias_or_model: str, aliases: Any = None
    ) -> tuple[_FakeProvider, str, dict[str, Any]]:
        # Stand in for ProviderRegistry.resolve — we don't need a real one
        # for this test, we just need the servicer to use whatever we return.
        assert alias_or_model == "fast-chat"
        return fake, "gpt-4o-mini", {"temperature": 0.9, "top_p": 0.95}

    servicer = CorlinmanAgentServicer(provider_resolver=_resolver)
    server = grpc.aio.server()
    agent_pb2_grpc.add_AgentServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = agent_pb2_grpc.AgentStub(channel)

            async def frames():
                yield agent_pb2.ClientFrame(
                    start=agent_pb2.ChatStart(model="fast-chat")
                )

            call = stub.Chat(frames())
            async for _ in call:
                pass
    finally:
        await server.stop(grace=None)

    # Provider was called with upstream model id, merged temperature, and
    # the remaining param flowed through via ``extra``.
    assert fake.last_kwargs["model"] == "gpt-4o-mini"
    assert fake.last_kwargs["temperature"] == pytest.approx(0.9)
    extra = fake.last_kwargs.get("extra") or {}
    assert extra.get("top_p") == pytest.approx(0.95)


@pytest.mark.asyncio
async def test_servicer_forwards_openai_tools_json_to_provider() -> None:
    """Client-supplied OpenAI tools must reach the provider call."""
    fake = _FakeProvider(_token_stream(["ok"]))
    tools = [
        {
            "type": "function",
            "function": {
                "name": "search",
                "description": "Search docs",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }
    ]

    def _resolver(_model: str) -> Any:
        return fake

    servicer = CorlinmanAgentServicer(provider_resolver=_resolver)
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
                        model="gpt-4o-mini",
                        tools_json=json.dumps(tools).encode("utf-8"),
                    )
                )

            call = stub.Chat(frames())
            async for _ in call:
                pass
    finally:
        await server.stop(grace=None)

    # The gateway-supplied tool must reach the provider. The servicer
    # also advertises the builtin tools (calculator + web), so the
    # provider sees the client tool *plus* the builtins — assert the
    # client tool is forwarded and the builtins were appended.
    forwarded = fake.last_kwargs["tools"]
    assert tools[0] in forwarded
    forwarded_names = {
        t.get("function", {}).get("name") for t in forwarded
    }
    assert {"search", "calculator", "web_search", "web_fetch"} <= forwarded_names


@pytest.mark.asyncio
async def test_servicer_assembles_context_before_provider_call() -> None:
    fake = _FakeProvider(_token_stream(["ok"]))
    assembler = _FakeContextAssembler()

    def _resolver(_model: str) -> Any:
        return fake

    servicer = CorlinmanAgentServicer(
        provider_resolver=_resolver,
        context_assembler=assembler,
    )
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
                        model="gpt-4o-mini",
                        session_key="sess-ctx",
                        messages=[
                            common_pb2.Message(
                                role=common_pb2.SYSTEM,
                                content="Recall: {{memory.backend}}",
                            ),
                            common_pb2.Message(role=common_pb2.USER, content="hi"),
                        ],
                    )
                )

            call = stub.Chat(frames())
            async for _ in call:
                pass
    finally:
        await server.stop(grace=None)

    assert assembler.calls
    assert assembler.calls[0]["session_key"] == "sess-ctx"
    assert assembler.calls[0]["model_name"] == "gpt-4o-mini"
    provider_messages = fake.last_kwargs["messages"]
    # Caller-supplied system message survives the assembler's placeholder
    # substitution; T1.3 then appends the dynamic env block, so only the
    # prefix is fixed.
    assert provider_messages[0]["content"].startswith(
        "Recall: memory hit from assembler"
    )


@pytest.mark.asyncio
async def test_servicer_registry_end_to_end_resolves_alias() -> None:
    """Wire a real ``ProviderRegistry`` + alias map through the servicer."""
    from corlinman_providers import ProviderKind, ProviderSpec

    fake = _FakeProvider(_token_stream(["ok"]))
    # Build a registry with one pretend-spec, then swap the built adapter
    # for our fake so we can inspect call args without hitting the SDK.
    spec = ProviderSpec(
        name="oai",
        kind=ProviderKind.OPENAI,
        api_key="sk-test",
        params={"temperature": 0.2},
    )
    reg = ProviderRegistry([spec])
    reg._providers["oai"] = fake  # type: ignore[assignment]  # test-only override
    aliases = {
        "creative": AliasEntry(
            provider="oai",
            model="gpt-4o",
            params={"temperature": 1.3},
        )
    }

    servicer = CorlinmanAgentServicer(
        provider_resolver=reg.resolve, aliases=aliases
    )
    server = grpc.aio.server()
    agent_pb2_grpc.add_AgentServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = agent_pb2_grpc.AgentStub(channel)

            async def frames():
                yield agent_pb2.ClientFrame(
                    start=agent_pb2.ChatStart(model="creative")
                )

            call = stub.Chat(frames())
            async for _ in call:
                pass
    finally:
        await server.stop(grace=None)

    assert fake.last_kwargs["model"] == "gpt-4o"
    # alias.temperature (1.3) wins over provider.temperature (0.2).
    assert fake.last_kwargs["temperature"] == pytest.approx(1.3)


# ---------------------------------------------------------------------------
# v0.7 multi-agent: builtin tool interception
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_servicer_dispatches_blackboard_write_in_process(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The servicer's ``_dispatch_builtin`` handles ``blackboard.write``
    without going through the gateway plugin runtime. We exercise the
    method directly because the streaming-loop test fixture is
    quadratic to set up — the unit-level contract here is enough."""
    from corlinman_agent.reasoning_loop import ChatStart, ToolCallEvent
    from corlinman_server.agent_servicer import CorlinmanAgentServicer

    # Point the lazy data dir at an isolated tmp so the test never
    # writes outside its sandbox.
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))

    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    start = ChatStart(
        model="m",
        messages=[],
        tools=[],
        session_key="tenant-x::session-1",
    )
    event = ToolCallEvent(
        call_id="c1",
        plugin="blackboard",
        tool="blackboard.write",
        args_json=b'{"key": "topic", "value": "research the moon"}',
    )

    result_json = await servicer._dispatch_builtin(
        event, start, _FakeProvider([])
    )
    payload = json.loads(result_json)
    assert payload["key"] == "topic"
    assert "error" not in payload
    # Receipt: an int written_at + the parent agent id as written_by.
    assert isinstance(payload["written_at"], int)
    assert "agent" in payload["written_by"] or payload["written_by"] == "m"

    # Read it back via the same method to lock the round-trip.
    read_event = ToolCallEvent(
        call_id="c2",
        plugin="blackboard",
        tool="blackboard.read",
        args_json=b'{"key": "topic"}',
    )
    read_json = await servicer._dispatch_builtin(
        read_event, start, _FakeProvider([])
    )
    read_payload = json.loads(read_json)
    assert read_payload == {
        "key": "topic",
        "value": "research the moon",
        "present": True,
    }


@pytest.mark.asyncio
async def test_servicer_builtin_tool_unknown_envelope() -> None:
    """An unrecognised tool name returns a structured error envelope
    rather than raising — the model's next round still has something
    to read."""
    from corlinman_agent.reasoning_loop import ChatStart, ToolCallEvent
    from corlinman_server.agent_servicer import CorlinmanAgentServicer

    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    start = ChatStart(model="m", messages=[], tools=[], session_key="s")
    event = ToolCallEvent(
        call_id="c",
        plugin="x",
        tool="blackboard.unknown",
        args_json=b"{}",
    )
    # This tool isn't in BUILTIN_TOOLS so the loop wouldn't normally
    # call _dispatch_builtin, but the method itself is defensive.
    result_json = await servicer._dispatch_builtin(
        event, start, _FakeProvider([])
    )
    payload = json.loads(result_json)
    assert "error" in payload


@pytest.mark.asyncio
async def test_servicer_dispatches_spawn_many_round_trip(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end smoke for v0.7 fan-out: a spawn_many ToolCallEvent
    flows through _dispatch_builtin, dispatches two siblings, and
    returns a ``{"tasks": [TaskResult, TaskResult]}`` envelope.

    Uses a stateful agent registry pre-populated with `researcher` and
    `editor` so the per-sibling dispatch can resolve the child cards.
    """
    from corlinman_agent.agents.card import AgentCard
    from corlinman_agent.agents.registry import AgentCardRegistry
    from corlinman_agent.reasoning_loop import ChatStart, ToolCallEvent
    from corlinman_server.agent_servicer import CorlinmanAgentServicer

    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))

    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    # Inject the agent registry directly; the lazy loader would otherwise
    # try to read `agents/*.yaml` from CORLINMAN_DATA_DIR which is empty here.
    servicer._builtin_agents = AgentCardRegistry(
        {
            "researcher": AgentCard(
                name="researcher", description="", system_prompt="you research"
            ),
            "editor": AgentCard(
                name="editor", description="", system_prompt="you edit"
            ),
        }
    )

    # Per-sibling provider: each child gets one chat_stream call that
    # streams a single token + done(stop). The same instance is shared
    # across siblings because the dispatch path doesn't care.
    provider = _FakeProvider(_token_stream(["did the work"]))

    start = ChatStart(
        model="orchestrator",
        messages=[],
        tools=[],
        session_key="tenant-a::sess-1",
    )
    args = json.dumps(
        {
            "tasks": [
                {"agent": "researcher", "goal": "find papers on X"},
                {"agent": "editor", "goal": "tighten the prose"},
            ]
        }
    )
    event = ToolCallEvent(
        call_id="spawn-1",
        plugin="subagent",
        tool="subagent.spawn_many",
        args_json=args.encode(),
    )
    result_json = await servicer._dispatch_builtin(event, start, provider)
    payload = json.loads(result_json)
    assert "error" not in payload, "fan-out happy path must elide outer error"
    assert len(payload["tasks"]) == 2
    # Order preserved from input.
    assert payload["tasks"][0]["child_session_key"].endswith("::child::0")
    assert payload["tasks"][1]["child_session_key"].endswith("::child::1")
    # Both stopped cleanly.
    for sibling in payload["tasks"]:
        assert sibling["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_servicer_threads_parent_tools_into_spawn(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``start.tools`` (the parent's full tool schema list) must reach
    the per-sibling dispatch so the child's allowlist filter can
    intersect against the parent's set. The contract is "child cannot
    request a tool the parent doesn't hold". This locks the wiring
    at the servicer boundary so a regression that drops ``start.tools``
    on the floor would surface here, not on a live deployment."""
    from corlinman_agent.agents.card import AgentCard
    from corlinman_agent.agents.registry import AgentCardRegistry
    from corlinman_agent.reasoning_loop import ChatStart, ToolCallEvent
    from corlinman_server.agent_servicer import CorlinmanAgentServicer

    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    servicer._builtin_agents = AgentCardRegistry(
        {"researcher": AgentCard(name="researcher", description="", system_prompt="r")}
    )

    parent_tools = [
        {
            "type": "function",
            "function": {
                "name": "web.search",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    start = ChatStart(
        model="m",
        messages=[],
        tools=parent_tools,
        session_key="s",
    )
    # The child requests a tool the parent does NOT hold ("file.read").
    # If start.tools threads through correctly, the runner's allowlist
    # filter rejects the spawn with ``tool_allowlist_escalation``.
    args = json.dumps(
        {
            "agent": "researcher",
            "goal": "x",
            "tool_allowlist": ["file.read"],
        }
    )
    event = ToolCallEvent(
        call_id="c",
        plugin="subagent",
        tool="subagent.spawn",
        args_json=args.encode(),
    )
    result_json = await servicer._dispatch_builtin(
        event, start, _FakeProvider([])
    )
    payload = json.loads(result_json)
    # The escalation reject proves parent_tools flowed through; if
    # ``start.tools`` had been dropped, the filter would have seen an
    # empty parent set and the child would have just inherited (silent
    # success with no tools).
    assert payload["finish_reason"] == "rejected"
    assert "tool_allowlist_escalation" in payload["error"]


# ---------------------------------------------------------------------------
# v0.7.1 warm pool: prewarm_providers surface
# ---------------------------------------------------------------------------


def test_servicer_prewarm_providers_populates_pool() -> None:
    """``prewarm_providers`` resolves each model name via the
    configured resolver and parks the result in the pool. We assert
    on the pool stats since the resolver's return value is opaque
    to the servicer's hot path."""
    from corlinman_server.agent_servicer import CorlinmanAgentServicer

    resolved_calls: list[str] = []

    def resolver(model: str) -> Any:
        resolved_calls.append(model)
        return _FakeProvider(_token_stream(["ok"]))

    servicer = CorlinmanAgentServicer(provider_resolver=resolver)
    servicer.prewarm_providers(["alpha", "beta", "gamma"])

    # All three resolutions happened at boot, not at first chat.
    assert resolved_calls == ["alpha", "beta", "gamma"]
    s = servicer.pool_stats()
    assert s.warm_count == 3
    assert s.misses == 0  # prewarm does not count as a miss


def test_servicer_prewarm_swallows_resolution_errors() -> None:
    """An unresolved alias must not crash the boot — the failed entry
    is skipped, others succeed."""
    from corlinman_server.agent_servicer import CorlinmanAgentServicer

    def resolver(model: str) -> Any:
        if model == "bad":
            raise KeyError(model)
        return _FakeProvider([])

    servicer = CorlinmanAgentServicer(provider_resolver=resolver)
    servicer.prewarm_providers(["good", "bad", "also-good"])
    s = servicer.pool_stats()
    # Only the two good ones landed warm.
    assert s.warm_count == 2


# ─── v0.8 builtin web tools ───────────────────────────────────────────


def test_builtin_tools_includes_web_surface() -> None:
    """``BUILTIN_TOOLS`` must list the v0.8 web tools so the streaming
    loop dispatches them in-process instead of emitting a ToolCall
    frame to the (nonexistent) plugin runtime."""
    from corlinman_server.agent_servicer import BUILTIN_TOOLS

    assert {"web_fetch", "web_search", "calculator"} <= BUILTIN_TOOLS


@pytest.mark.asyncio
async def test_servicer_dispatches_calculator_in_process() -> None:
    """``calculator`` flows through ``_dispatch_builtin`` and returns a
    JSON result envelope — no network, fully self-contained."""
    from corlinman_agent.reasoning_loop import ChatStart, ToolCallEvent
    from corlinman_server.agent_servicer import CorlinmanAgentServicer

    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    start = ChatStart(model="m", messages=[], tools=[], session_key="s")
    event = ToolCallEvent(
        call_id="c1",
        plugin="builtin",
        tool="calculator",
        args_json=b'{"expression": "6 * 7"}',
    )
    payload = json.loads(
        await servicer._dispatch_builtin(event, start, _FakeProvider([]))
    )
    assert payload["result"] == 42


@pytest.mark.asyncio
async def test_servicer_web_search_degrades_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``web_search`` builtin call with no reachable backend returns a
    well-formed degraded envelope rather than raising, so the reasoning
    loop keeps going."""
    from corlinman_agent.reasoning_loop import ChatStart, ToolCallEvent
    from corlinman_server.agent_servicer import CorlinmanAgentServicer

    monkeypatch.setenv("CORLINMAN_WEB_SEARCH_BACKEND", "totally-unknown")

    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    start = ChatStart(model="m", messages=[], tools=[], session_key="s")
    event = ToolCallEvent(
        call_id="c2",
        plugin="builtin",
        tool="web_search",
        args_json=b'{"query": "anything"}',
    )
    payload = json.loads(
        await servicer._dispatch_builtin(event, start, _FakeProvider([]))
    )
    assert payload["results"] == []
    assert "error" in payload


# ---------------------------------------------------------------------------
# T1.3 — enriched system prompt + dynamic environment block
# ---------------------------------------------------------------------------


def test_ensure_system_prompt_injects_when_absent() -> None:
    """When no system message exists, the injected one carries both the
    behavioral prompt and the dynamic ``# Environment`` block."""
    from corlinman_agent.reasoning_loop import ChatStart
    from corlinman_server.agent_servicer import _ensure_system_prompt

    start = ChatStart(
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        session_key="s",
    )
    _ensure_system_prompt(start)

    msgs = list(start.messages)
    assert msgs[0]["role"] == "system"
    assert "# Environment" in msgs[0]["content"]
    # Behavioral content survives too.
    assert "corlinman" in msgs[0]["content"]
    # User message untouched, after the system message.
    assert msgs[1] == {"role": "user", "content": "hi"}


def test_ensure_system_prompt_appends_env_to_existing() -> None:
    """An existing system message (e.g. from an agent card) is preserved
    and the env block is appended — still exactly one system message."""
    from corlinman_agent.reasoning_loop import ChatStart
    from corlinman_server.agent_servicer import (
        _build_env_block,
        _ensure_system_prompt,
    )

    start = ChatStart(
        model="m",
        messages=[
            {"role": "system", "content": "you are X"},
            {"role": "user", "content": "hi"},
        ],
        tools=[],
        session_key="s",
    )
    _ensure_system_prompt(start)

    msgs = list(start.messages)
    # Still exactly one system message, in position 0.
    system_msgs = [m for m in msgs if m.get("role") == "system"]
    assert len(system_msgs) == 1
    assert msgs[0]["role"] == "system"

    content = msgs[0]["content"]
    # Behavioral content (the agent card) is preserved at the start...
    assert content.startswith("you are X")
    # ...and the env block is appended at the end. The exact volatile
    # values (date, shell) shift, but the heading + the workspace line
    # come straight from ``_build_env_block`` and pin the suffix.
    env_block = _build_env_block()
    assert content.endswith(env_block)
    # Env block is appended, not prefixed — behavioral content first.
    assert content.index("you are X") < content.index("# Environment")
    # User message untouched, after the system message.
    assert msgs[1] == {"role": "user", "content": "hi"}


def test_env_block_contains_workspace_and_platform() -> None:
    """The dynamic env block names the workspace path and platform."""
    from corlinman_server.agent_servicer import _build_env_block

    block = _build_env_block()
    assert "workspace" in block
    assert "platform" in block
    # Heading shape too.
    assert block.startswith("# Environment")


# ---------------------------------------------------------------------------
# T1.4 — _CostMeter accumulates per-session token totals
# ---------------------------------------------------------------------------


def test_cost_meter_accumulates_per_session() -> None:
    """Two turns on the same session sum input/output tokens + bump requests.

    Shape contract: ``snapshot(session_key)`` returns a dict containing
    each summed usage key plus ``requests``. Future provider fields
    (cached_*, reasoning_tokens) flow through unchanged so the meter
    survives a new vendor without a code edit.
    """
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))

    servicer._cost_meter.add("s1", {"input_tokens": 10, "output_tokens": 20})
    servicer._cost_meter.add("s1", {"input_tokens": 5, "output_tokens": 7})

    snap = servicer.cost_snapshot("s1")
    assert snap == {"input_tokens": 15, "output_tokens": 27, "requests": 2}


def test_cost_meter_keeps_sessions_isolated() -> None:
    """Two distinct session_keys don't bleed into each other."""
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))

    servicer._cost_meter.add("s1", {"input_tokens": 10, "output_tokens": 20})
    servicer._cost_meter.add("s2", {"input_tokens": 3, "output_tokens": 4})

    assert servicer.cost_snapshot("s1") == {
        "input_tokens": 10, "output_tokens": 20, "requests": 1,
    }
    assert servicer.cost_snapshot("s2") == {
        "input_tokens": 3, "output_tokens": 4, "requests": 1,
    }
    # Unknown session returns an empty snapshot, not a KeyError.
    assert servicer.cost_snapshot("s-missing") == {}


def test_cost_meter_tolerates_none_and_empty_usage() -> None:
    """``usage=None`` / empty dict / empty session_key are no-ops.

    Prevents the DoneEvent branch from accidentally inflating
    ``requests`` when the provider didn't actually report cost (mid-
    stream errors, retries that bailed pre-completion).
    """
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))

    servicer._cost_meter.add("s1", None)
    servicer._cost_meter.add("s1", {})
    servicer._cost_meter.add("", {"input_tokens": 5})

    assert servicer.cost_snapshot("s1") == {}
    assert servicer.cost_snapshot("") == {}


def test_cost_meter_preserves_optional_provider_fields() -> None:
    """``cached_input_tokens`` / ``reasoning_tokens`` flow through unchanged."""
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))

    servicer._cost_meter.add(
        "s1",
        {
            "input_tokens": 10,
            "output_tokens": 20,
            "cached_input_tokens": 4,
            "reasoning_tokens": 6,
        },
    )
    servicer._cost_meter.add(
        "s1",
        {
            "input_tokens": 2,
            "output_tokens": 3,
            "cached_input_tokens": 1,
        },
    )

    snap = servicer.cost_snapshot("s1")
    assert snap == {
        "input_tokens": 12,
        "output_tokens": 23,
        "cached_input_tokens": 5,
        "reasoning_tokens": 6,
        "requests": 2,
    }


def test_cost_snapshot_returns_a_copy() -> None:
    """Mutating the snapshot must not corrupt the meter's interior."""
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))

    servicer._cost_meter.add("s1", {"input_tokens": 10, "output_tokens": 20})
    snap = servicer.cost_snapshot("s1")
    snap["input_tokens"] = 99_999  # try to poison the meter

    assert servicer.cost_snapshot("s1")["input_tokens"] == 10


# ---------------------------------------------------------------------------
# T3.1 — permission gate + T3.2 hook bus emit
# ---------------------------------------------------------------------------


class _RecordingHookBus:
    """Captures emit_nonblocking calls for assertions."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit_nonblocking(self, ev: Any) -> None:
        self.events.append(ev)


async def test_dispatch_builtin_emits_pre_and_post_around_tool() -> None:
    """T3.2: PreToolDispatch fires before, ToolCalled fires after."""
    from corlinman_agent.reasoning_loop import ToolCallEvent

    bus = _RecordingHookBus()
    servicer = CorlinmanAgentServicer(
        provider_resolver=lambda _m: _FakeProvider([]),
        hook_bus=bus,
    )

    start = SimpleNamespace(
        session_key="sess-emit",
        model="gpt-test",
        tools=[],
        messages=[],
    )
    event = ToolCallEvent(
        call_id="c1",
        plugin="calculator",
        tool="calculator",
        args_json=b'{"expression": "1 + 2"}',
    )
    result = await servicer._dispatch_builtin(event, start, provider=None)
    assert "result" in result or "error" not in json.loads(result)

    # Two events emitted in order:
    kinds = [type(e).__name__ for e in bus.events]
    assert "_PreToolDispatch" in kinds
    assert "_ToolCalled" in kinds
    pre = next(e for e in bus.events if type(e).__name__ == "_PreToolDispatch")
    post = next(e for e in bus.events if type(e).__name__ == "_ToolCalled")
    assert pre.tool == "calculator"
    assert pre.call_id == "c1"
    assert pre.session_key_ == "sess-emit"
    assert post.tool == "calculator"
    assert post.ok is True
    assert post.duration_ms >= 0


async def test_dispatch_builtin_denies_with_permission_gate() -> None:
    """T3.1: a deny rule blocks the tool, emits ToolCalled ok=False."""
    from corlinman_agent.permission import DENY, PermissionGate, PermissionRule
    from corlinman_agent.reasoning_loop import ToolCallEvent

    bus = _RecordingHookBus()
    gate = PermissionGate([PermissionRule(tool="calculator", action=DENY)])
    servicer = CorlinmanAgentServicer(
        provider_resolver=lambda _m: _FakeProvider([]),
        hook_bus=bus,
        permission_gate=gate,
    )

    start = SimpleNamespace(
        session_key="sess-deny",
        model="gpt-test",
        tools=[],
        messages=[],
    )
    event = ToolCallEvent(
        call_id="c-deny",
        plugin="calculator",
        tool="calculator",
        args_json=b'{"expression": "1 + 1"}',
    )
    result = await servicer._dispatch_builtin(event, start, provider=None)
    payload = json.loads(result)
    assert "permission_denied" in payload["error"]
    assert payload["tool"] == "calculator"

    # Post-event marked ok=False with the right error_code.
    post = next(e for e in bus.events if type(e).__name__ == "_ToolCalled")
    assert post.ok is False
    assert post.error_code == "permission_denied"


async def test_dispatch_builtin_strict_mode_denies_mutating_tools() -> None:
    """Strict mode auto-denies the mutating tool set without per-tool rules."""
    from corlinman_agent.permission import PermissionGate
    from corlinman_agent.reasoning_loop import ToolCallEvent

    gate = PermissionGate(strict=True)
    servicer = CorlinmanAgentServicer(
        provider_resolver=lambda _m: _FakeProvider([]),
        permission_gate=gate,
    )

    start = SimpleNamespace(
        session_key="sess-strict",
        model="gpt-test",
        tools=[],
        messages=[],
    )
    event = ToolCallEvent(
        call_id="c-strict",
        plugin="run_shell",
        tool="run_shell",
        args_json=b'{"command": "echo hi"}',
    )
    result = await servicer._dispatch_builtin(event, start, provider=None)
    payload = json.loads(result)
    assert "permission_denied" in payload["error"]


async def test_dispatch_builtin_no_hook_bus_still_works() -> None:
    """Default servicer (no hook_bus) dispatches without trying to emit."""
    from corlinman_agent.reasoning_loop import ToolCallEvent

    servicer = CorlinmanAgentServicer(
        provider_resolver=lambda _m: _FakeProvider([]),
    )
    start = SimpleNamespace(
        session_key="sess-nobus",
        model="gpt-test",
        tools=[],
        messages=[],
    )
    event = ToolCallEvent(
        call_id="c",
        plugin="calculator",
        tool="calculator",
        args_json=b'{"expression": "5"}',
    )
    result = await servicer._dispatch_builtin(event, start, provider=None)
    # Calculator returns the parsed expression result.
    payload = json.loads(result)
    assert payload["result"] == 5
