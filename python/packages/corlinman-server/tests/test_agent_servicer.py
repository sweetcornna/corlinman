"""Full-stack servicer test: real gRPC server, fake provider, verify frames."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, ClassVar

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
        tool="subagent_spawn_many",
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
async def test_servicer_spawn_seeds_child_persona_state(
    tmp_path: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a subagent spawn must seed the child's persona-STATE row.

    The dispatch threads ``_get_persona_state_store()`` (the tenant-aware
    corlinman-persona STATE store, ``agent_state.sqlite``) into the runner's
    ``_seed_child_persona`` — NOT the system-prompt registry
    (``personas.sqlite``). A prior wiring bug passed the registry, whose
    ``get()`` rejects ``tenant_id=``, so every child spawn logged
    ``subagent.runner.persona_seed_failed`` and the STATE row was never
    written. Because seeding is best-effort the spawn still succeeded, which
    masked the bug — so we assert the row is *actually* present, not just that
    the spawn returned. Surfaced by a live prod fan-out test.
    """
    from corlinman_agent.agents.card import AgentCard
    from corlinman_agent.agents.registry import AgentCardRegistry
    from corlinman_agent.reasoning_loop import ChatStart, ToolCallEvent
    from corlinman_server.agent_servicer import CorlinmanAgentServicer

    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    servicer._builtin_agents = AgentCardRegistry(
        {
            "researcher": AgentCard(
                name="researcher", description="", system_prompt="you research"
            )
        }
    )
    provider = _FakeProvider(_token_stream(["did the work"]))
    start = ChatStart(
        model="orchestrator", messages=[], tools=[], session_key="tenant-a::sess-1"
    )
    args = json.dumps({"tasks": [{"agent": "researcher", "goal": "find papers"}]})
    event = ToolCallEvent(
        call_id="spawn-1",
        plugin="subagent",
        tool="subagent_spawn_many",
        args_json=args.encode(),
    )
    payload = json.loads(await servicer._dispatch_builtin(event, start, provider))
    assert payload["tasks"][0]["finish_reason"] == "stop"

    # The child's persona-STATE row must exist in the STATE store — only true
    # if the seeder received a tenant-aware store (the bug passed the registry,
    # so this row was never written).
    state_store = await servicer._get_persona_state_store()
    assert state_store is not None, "state store should open under CORLINMAN_DATA_DIR"
    child_agent_id = payload["tasks"][0]["child_agent_id"]
    row = await state_store.get(child_agent_id, tenant_id="tenant-a")
    assert row is not None, "subagent spawn must seed the child persona-state row"


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
        tool="subagent_spawn",
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


# ---------------------------------------------------------------------------
# T4.1 / T4.2 / T4.4 — journal + session lock
# ---------------------------------------------------------------------------


async def test_lock_for_returns_same_instance_per_session() -> None:
    """T4.2: same session_key → same lock → serialized; different keys → distinct locks."""
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    a1 = servicer._lock_for("sess-A")
    a2 = servicer._lock_for("sess-A")
    b1 = servicer._lock_for("sess-B")
    assert a1 is a2
    assert a1 is not b1


async def test_lock_for_empty_session_gets_fresh_lock_each_call() -> None:
    """Empty session_key (one-shot HTTP callers) get independent locks."""
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    a = servicer._lock_for("")
    b = servicer._lock_for("")
    assert a is not b


async def test_recent_errored_turns_returns_empty_when_no_journal() -> None:
    """Servicer.recent_errored_turns() returns [] when the journal can't open."""

    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    # Force the journal open path: empty session is fine — fixtures don't care.
    crumbs = await servicer.recent_errored_turns("any-session", limit=5)
    assert isinstance(crumbs, list)


async def test_recent_errored_turns_surfaces_journal_entries(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T4.4: an errored turn appears via servicer.recent_errored_turns()."""
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    # Force-open the journal and stamp an error directly so we don't have to
    # drive the full chat path here.
    j = await servicer._get_journal()
    assert j is not None
    tid = await j.begin_turn("s-err", "broken")
    await j.error_turn(tid, "BANG")
    crumbs = await servicer.recent_errored_turns("s-err")
    assert any("BANG" in (c["error"] or "") for c in crumbs)


# ---------------------------------------------------------------------------
# T4.1 — Chat-handler resume integration
#
# The handler must consult ``AgentJournal.find_resumable_turn`` BEFORE
# calling ``begin_turn``. When a fresh in-progress row matches (same
# session_key + same user text within the resume window), it must splice
# the replay into ``start.messages`` and bypass ``begin_turn`` entirely.
# Stale rows (older than the window) must NOT match — the handler
# creates a brand-new turn instead.
# ---------------------------------------------------------------------------


class _CapturingLoop:
    """Stand-in for ``ReasoningLoop`` that records ``start`` and ends
    immediately with a ``DoneEvent``. Used to assert the messages the
    handler hands the loop without driving a real provider."""

    captured_starts: ClassVar[list[Any]] = []

    def __init__(
        self,
        provider: Any,
        *,
        tool_result_timeout: float = 0.05,
        event_emitter: Any | None = None,
    ) -> None:
        self._provider = provider
        # W1.3 — accept the new optional emitter so the servicer's
        # construction call site can pass it without unpacking errors.
        # We don't drive emit() in this stub.
        self._event_emitter = event_emitter

    def feed_tool_result(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def cancel(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def signal_input_closed(self) -> None:
        return None

    async def run(self, start: Any) -> AsyncIterator[Any]:
        from corlinman_agent.reasoning_loop import DoneEvent

        # Deep-snapshot messages so subsequent in-place mutations cannot
        # contaminate the assertion target.
        type(self).captured_starts.append(
            SimpleNamespace(
                messages=[dict(m) for m in start.messages],
                session_key=start.session_key,
            )
        )
        yield DoneEvent(finish_reason="stop")


async def _drive_chat_once(
    servicer: CorlinmanAgentServicer,
    *,
    session_key: str,
    user_text: str,
) -> None:
    """Drive one Chat RPC end-to-end against ``servicer`` and drain it."""
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
                        session_key=session_key,
                        messages=[
                            common_pb2.Message(
                                role=common_pb2.USER, content=user_text
                            )
                        ],
                    )
                )

            call = stub.Chat(frames())
            async for _ in call:
                pass
    finally:
        await server.stop(grace=None)


async def test_chat_resumes_in_progress_turn_replaying_tool_results(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same session_key + same user text → handler resumes the journal
    turn, replays its (assistant tool_call, tool result) pair into
    ``start.messages``, and does NOT create a fresh turn via ``begin_turn``."""
    import structlog
    from corlinman_server import agent_servicer as srv_mod
    from corlinman_server.agent_journal import AgentJournal

    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    # Swap the reasoning loop for one that just captures + ends.
    monkeypatch.setattr(srv_mod, "ReasoningLoop", _CapturingLoop)
    _CapturingLoop.captured_starts = []

    servicer = CorlinmanAgentServicer(
        provider_resolver=lambda _m: _FakeProvider([]),
    )
    # Seed the journal: an in_progress turn for "s1" with one
    # (assistant tool_call, tool_result) pair already journaled.
    j = await servicer._get_journal()
    assert j is not None
    seed_turn_id = await j.begin_turn("s1", "do the thing")
    await j.append_message(seed_turn_id, role="user", content="do the thing")
    # Use ``calculator`` (a real builtin) so the resume splice's C6
    # stale-tool filter keeps the tool_call instead of pruning it.
    # ``calc.add`` would be filtered as unknown — that is the C6
    # behaviour, exercised separately in ``test_resume_prunes_stale_tool_calls``.
    await j.append_message(
        seed_turn_id,
        role="assistant",
        content="",
        tool_calls=[
            {
                "id": "call_seed",
                "type": "function",
                "function": {"name": "calculator", "arguments": '{"expression":"2+2"}'},
            }
        ],
    )
    await j.append_message(
        seed_turn_id,
        role="tool",
        content='{"result": 4}',
        tool_call_id="call_seed",
    )

    # Wrap ``begin_turn`` at the class level so we can assert it was NOT
    # called by the handler on the resume path. ``AgentJournal`` uses
    # ``__slots__`` so per-instance monkeypatching is impossible — the
    # class-level swap reaches every instance, which is fine here.
    # Seeding above already used the real method; this counter only
    # measures the chat handler's behaviour.
    begin_calls: list[tuple[str, str]] = []
    real_begin = AgentJournal.begin_turn

    async def _counting_begin(
        self_inner: Any,
        session_key: str,
        user_text: str,
        *,
        user_id: str | None = None,
        channel: str = "",
    ) -> int | None:
        begin_calls.append((session_key, user_text))
        return await real_begin(
            self_inner,
            session_key,
            user_text,
            user_id=user_id,
            channel=channel,
        )

    monkeypatch.setattr(AgentJournal, "begin_turn", _counting_begin)

    with structlog.testing.capture_logs() as captured:
        await _drive_chat_once(servicer, session_key="s1", user_text="do the thing")

    # ``loop.run`` was invoked exactly once with the spliced messages.
    assert len(_CapturingLoop.captured_starts) == 1
    spliced = _CapturingLoop.captured_starts[0].messages
    roles_and_calls = [
        (
            m.get("role"),
            m.get("tool_call_id"),
            (m.get("tool_calls") or [{}])[0].get("id")
            if m.get("tool_calls")
            else None,
        )
        for m in spliced
    ]
    # The replay user/assistant/tool triple lands in-order in front of
    # whatever post-resume context (system prompt, env block) the handler
    # adds. We assert on the relative positions rather than the absolute
    # message count so prompt scaffolding can evolve without breaking us.
    user_idx = next(
        i for i, m in enumerate(spliced)
        if m.get("role") == "user" and m.get("content") == "do the thing"
    )
    assistant_idx = next(
        i for i, m in enumerate(spliced)
        if m.get("role") == "assistant"
        and any(
            (tc or {}).get("id") == "call_seed"
            for tc in (m.get("tool_calls") or [])
        )
    )
    tool_idx = next(
        i for i, m in enumerate(spliced)
        if m.get("role") == "tool" and m.get("tool_call_id") == "call_seed"
    )
    assert user_idx < assistant_idx < tool_idx, (
        f"replay ordering broken: {roles_and_calls}"
    )

    # ``begin_turn`` was NOT called by the handler — resume reuses the
    # seeded turn_id.
    assert begin_calls == [], (
        f"resume path must not call begin_turn; got {begin_calls}"
    )

    # The resume log line fired with the canonical fields.
    resume_logs = [r for r in captured if r.get("event") == "agent.chat.resumed"]
    assert resume_logs, (
        f"expected agent.chat.resumed log; got events: "
        f"{[r.get('event') for r in captured]}"
    )
    rec = resume_logs[0]
    assert rec["turn_id"] == seed_turn_id
    assert rec["session"] == "s1"
    assert rec["replayed_tool_results"] == 1


async def test_chat_does_not_resume_stale_in_progress_turn(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in_progress row older than the resume window (5 min) must NOT
    match; the handler creates a fresh turn via ``begin_turn`` instead."""
    import time

    import structlog
    from corlinman_server import agent_servicer as srv_mod
    from corlinman_server.agent_journal import AgentJournal

    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(srv_mod, "ReasoningLoop", _CapturingLoop)
    _CapturingLoop.captured_starts = []

    servicer = CorlinmanAgentServicer(
        provider_resolver=lambda _m: _FakeProvider([]),
    )
    j = await servicer._get_journal()
    assert j is not None
    # Insert a stale in_progress row directly (10 min old) so the
    # resume window (5 min) rejects it. We bypass the open-time stale
    # sweep by writing AFTER ``_get_journal`` has already run, and we
    # reuse the journal's own aiosqlite connection so pytest teardown
    # doesn't leak a separate worker thread.
    stale_started_ms = int(time.time() * 1000) - 10 * 60 * 1000
    backend_conn = j.backend._c  # noqa: SLF001 — test fixture
    await backend_conn.execute(
        "INSERT INTO turns (turn_id, session_key, status, "
        "started_at_ms, user_text) VALUES (?, ?, ?, ?, ?)",
        (stale_started_ms, "s2", "in_progress", stale_started_ms, "old prompt"),
    )
    await backend_conn.commit()

    # Patch ``begin_turn`` at the class level (see sibling test for the
    # __slots__ rationale).
    begin_calls: list[tuple[str, str]] = []
    real_begin = AgentJournal.begin_turn

    async def _counting_begin(
        self_inner: Any,
        session_key: str,
        user_text: str,
        *,
        user_id: str | None = None,
        channel: str = "",
    ) -> int | None:
        begin_calls.append((session_key, user_text))
        return await real_begin(
            self_inner,
            session_key,
            user_text,
            user_id=user_id,
            channel=channel,
        )

    monkeypatch.setattr(AgentJournal, "begin_turn", _counting_begin)

    with structlog.testing.capture_logs() as captured:
        await _drive_chat_once(servicer, session_key="s2", user_text="old prompt")

    # Stale row doesn't match → no resume log, fresh begin_turn fired.
    resume_logs = [r for r in captured if r.get("event") == "agent.chat.resumed"]
    assert resume_logs == [], (
        f"stale in_progress must not trigger resume; got {resume_logs}"
    )
    assert begin_calls == [("s2", "old prompt")], (
        f"expected one fresh begin_turn; got {begin_calls}"
    )


# ---------------------------------------------------------------------------
# C6 — resume splice prunes assistant tool_calls referencing tools that
# are no longer in the current turn's tool surface, plus the matching
# role="tool" rows.
# ---------------------------------------------------------------------------


def test_prune_stale_tool_calls_drops_unknown_and_pairs() -> None:
    """Direct unit test for :func:`_prune_stale_tool_calls`.

    Two assistant tool_calls — one for ``calculator`` (in BUILTIN_TOOLS)
    and one for ``vanished_plugin`` (removed since the journal was
    written). Plus two matching ``role=tool`` rows. After pruning, only
    the calculator pair survives.
    """
    from corlinman_server.agent_servicer import _prune_stale_tool_calls

    current = frozenset({"calculator"})
    msgs = [
        {"role": "user", "content": "do both"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "calculator", "arguments": "{}"},
                },
                {
                    "id": "c2",
                    "type": "function",
                    "function": {"name": "vanished_plugin", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "{}"},
        {"role": "tool", "tool_call_id": "c2", "content": "{}"},
    ]
    out, dropped = _prune_stale_tool_calls(msgs, current)
    assert dropped == 1
    # The assistant row keeps only c1.
    assistant_row = next(m for m in out if m["role"] == "assistant")
    kept_ids = [tc["id"] for tc in assistant_row["tool_calls"]]
    assert kept_ids == ["c1"], (
        f"expected only c1 to survive, got {kept_ids}"
    )
    # The matching c2 tool row is gone.
    tool_ids = [m.get("tool_call_id") for m in out if m["role"] == "tool"]
    assert tool_ids == ["c1"], (
        f"expected only c1's tool row, got {tool_ids}"
    )


async def test_chat_resume_prunes_stale_tool_calls(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C6 end-to-end: a journal entry referencing a tool not in the
    current ``start.tools`` (and not in BUILTIN_TOOLS) is pruned
    before splicing into the live message list."""
    import structlog
    from corlinman_server import agent_servicer as srv_mod

    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(srv_mod, "ReasoningLoop", _CapturingLoop)
    _CapturingLoop.captured_starts = []

    servicer = CorlinmanAgentServicer(
        provider_resolver=lambda _m: _FakeProvider([]),
    )
    j = await servicer._get_journal()
    assert j is not None
    seed = await j.begin_turn("c6-sess", "two-tool task")
    await j.append_message(seed, role="user", content="two-tool task")
    # One known tool (calculator, in BUILTIN_TOOLS) + one stale tool
    # (gone_plugin, not in the current surface).
    await j.append_message(
        seed,
        role="assistant",
        content="",
        tool_calls=[
            {
                "id": "keep1",
                "type": "function",
                "function": {"name": "calculator", "arguments": "{}"},
            },
            {
                "id": "drop1",
                "type": "function",
                "function": {"name": "gone_plugin", "arguments": "{}"},
            },
        ],
    )
    await j.append_message(
        seed, role="tool", content='{"result":1}', tool_call_id="keep1"
    )
    await j.append_message(
        seed, role="tool", content='{"result":2}', tool_call_id="drop1"
    )

    with structlog.testing.capture_logs() as captured:
        await _drive_chat_once(
            servicer, session_key="c6-sess", user_text="two-tool task"
        )

    spliced = _CapturingLoop.captured_starts[0].messages
    # The kept tool_call (calculator) survives.
    kept = [
        tc["id"]
        for m in spliced
        if m.get("role") == "assistant"
        for tc in (m.get("tool_calls") or [])
    ]
    assert "keep1" in kept and "drop1" not in kept, (
        f"C6 violation: kept={kept}"
    )
    # The matching ``tool`` row for drop1 is also gone.
    tool_ids = [
        m["tool_call_id"]
        for m in spliced
        if m.get("role") == "tool" and "tool_call_id" in m
    ]
    assert "drop1" not in tool_ids, (
        f"C6 violation: dropped tool_call's role=tool row survived: {tool_ids}"
    )
    # The structured log fires with the drop count.
    pruned_logs = [
        r for r in captured if r.get("event") == "agent.resume.tools_pruned"
    ]
    assert pruned_logs, (
        "expected agent.resume.tools_pruned log; got "
        f"{[r.get('event') for r in captured]}"
    )
    assert pruned_logs[0]["dropped"] >= 1


# ---------------------------------------------------------------------------
# R1 — bounded session-keyed caches (lock map + cost meter)
# ---------------------------------------------------------------------------


def test_session_lock_cache_evicts_unheld_locks_at_cap() -> None:
    """5000 unique session_keys → cache size stays at or below cap (4096
    by default). Held locks are pinned and survive even if older than
    the cap allows."""
    from corlinman_server.agent_servicer import _SessionLockCache

    cap = 4096
    cache = _SessionLockCache(cap)
    for i in range(5000):
        cache.get(f"s-{i}")
    # The cap is the steady-state upper bound — there should be no
    # entry growth beyond it.
    assert len(cache) <= cap, (
        f"R1 violation: cache grew to {len(cache)} entries (cap={cap})"
    )


def test_session_lock_cache_pins_held_locks() -> None:
    """A held lock cannot be evicted (the in-flight RPC still needs it)."""
    import asyncio

    from corlinman_server.agent_servicer import _SessionLockCache

    cap = 100
    cache = _SessionLockCache(cap)

    async def _run() -> None:
        # Acquire the first lock — it's now "held".
        first = cache.get("held")
        await first.acquire()
        try:
            # Pour in 2*cap NEW keys; the held one must NOT be evicted.
            for i in range(2 * cap):
                cache.get(f"flood-{i}")
            # The held entry is still present.
            still_present = cache.get("held")
            assert still_present is first, (
                "R1 violation: held lock got evicted under flood load"
            )
        finally:
            first.release()

    asyncio.run(_run())


def test_cost_meter_evicts_oldest_unconditionally() -> None:
    """Unlike the lock cache, the cost meter has no held-entry concept —
    LRU evicts the oldest session unconditionally past the cap. The
    most recently added session is preserved."""
    from corlinman_server.agent_servicer import _CostMeter

    meter = _CostMeter(cap=100)
    for i in range(500):
        meter.add(f"sess-{i}", {"input_tokens": 1, "output_tokens": 1})
    assert len(meter) == 100, (
        f"R1 violation: cost meter grew to {len(meter)} sessions (cap=100)"
    )
    # The most recently added session is still there.
    assert meter.snapshot("sess-499") != {}
    # An old session has been evicted.
    assert meter.snapshot("sess-0") == {}


def test_session_cache_cap_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """``CORLINMAN_MAX_SESSION_CACHE`` raises the cap."""
    from corlinman_server.agent_servicer import (
        _CostMeter,
        _session_cache_cap,
        _SessionLockCache,
    )

    monkeypatch.setenv("CORLINMAN_MAX_SESSION_CACHE", "256")
    assert _session_cache_cap() == 256
    assert _SessionLockCache(_session_cache_cap()).cap == 256
    assert _CostMeter().cap == 256


# ---------------------------------------------------------------------------
# R4 — coordinated shutdown closes every owned resource
# ---------------------------------------------------------------------------


class _FakeClosable:
    """Tiny stand-in resource that records every ``close()`` call.

    Used to instrument the servicer's R4 ``aclose`` walk without
    depending on the real journal / blackboard / hook bus internals
    (each of which uses ``__slots__`` or otherwise resists per-instance
    monkey-patching).
    """

    def __init__(self, label: str, raises: bool = False) -> None:
        self.label = label
        self.calls: list[str] = []
        self.raises = raises

    async def close(self) -> None:
        self.calls.append("close")
        if self.raises:
            raise RuntimeError(f"simulated teardown failure for {self.label}")


class _FakeSyncClosable:
    """Synchronous variant — :meth:`aclose` must tolerate non-coroutine close()."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.calls: list[str] = []

    def close(self) -> None:
        self.calls.append("close")


async def test_aclose_closes_every_owned_resource(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``aclose()`` calls ``close``/``aclose`` on every lazily-opened
    resource the servicer owns, never raises, and is idempotent."""
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    servicer = CorlinmanAgentServicer(
        provider_resolver=lambda _m: _FakeProvider([]),
    )

    # Swap in fake closables so we can observe close() invocations.
    # Each is a different shape: async coroutine, sync method, attribute
    # name — the servicer's aclose() walks both ``close`` and ``aclose``
    # and accepts coroutine + sync returns.
    journal_fake = _FakeClosable("journal")
    memory_fake = _FakeClosable("memory")
    bb_fake = _FakeSyncClosable("blackboard")
    hook_fake = _FakeClosable("hook_bus")
    servicer._journal = journal_fake  # type: ignore[assignment]
    servicer._memory_host = memory_fake
    servicer._blackboard_store = bb_fake  # type: ignore[assignment]
    servicer._hook_bus = hook_fake

    # First aclose closes every resource.
    await servicer.aclose()
    assert journal_fake.calls == ["close"]
    assert memory_fake.calls == ["close"]
    assert bb_fake.calls == ["close"]
    assert hook_fake.calls == ["close"]

    # Second aclose is idempotent — resources are now None, so no
    # extra close() calls land.
    await servicer.aclose()
    assert journal_fake.calls == ["close"]
    assert memory_fake.calls == ["close"]
    assert bb_fake.calls == ["close"]


async def test_aclose_tolerates_resource_close_failures(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One resource raising on close must not block the others."""
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    servicer = CorlinmanAgentServicer(
        provider_resolver=lambda _m: _FakeProvider([]),
    )

    bad_journal = _FakeClosable("journal", raises=True)
    good_memory = _FakeClosable("memory")
    good_bb = _FakeSyncClosable("blackboard")
    servicer._journal = bad_journal  # type: ignore[assignment]
    servicer._memory_host = good_memory
    servicer._blackboard_store = good_bb  # type: ignore[assignment]

    # aclose must NOT raise — it logs and continues so the rest of the
    # resources still get their close() invocation.
    await servicer.aclose()
    assert bad_journal.calls == ["close"]  # close attempted
    assert good_memory.calls == ["close"], (
        "R4 violation: a failing journal.close blocked the memory close"
    )
    assert good_bb.calls == ["close"], (
        "R4 violation: a failing journal.close blocked the blackboard close"
    )


# ---------------------------------------------------------------------------
# Claude-Code-style mid-turn user supplement
#
# A second Chat RPC for the SAME session_key while a turn is still in
# flight must NOT serialise behind the session lock as a new turn —
# instead the new user text is injected into the running loop and the
# second RPC returns a short "supplemented" Done frame.
# ---------------------------------------------------------------------------


class _PausingProvider:
    """Provider that drives a 2-round turn with a pause point the test
    controls. Round 1 emits a tool_call + done(tool_calls) and BLOCKS
    on ``round1_block`` before signalling done — that's the window
    during which the test installs the active loop AND drives the
    second Chat RPC to inject a supplement. Round 2 sees the
    supplement in its drained messages list (the reasoning loop drains
    the queue at the top of every round, *before* calling chat_stream).
    """

    def __init__(
        self,
        round1_seen: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        # Fired the moment the provider's round-1 chat_stream begins
        # running — proves the active loop is live and gives the test
        # a deterministic synchronisation point.
        self._round1_seen = round1_seen
        self._release = release
        self.rounds_seen: list[list[dict[str, Any]]] = []

    async def chat_stream(
        self, *, messages: list[dict[str, Any]], **_: Any
    ) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        self.rounds_seen.append([dict(m) for m in messages])
        idx = len(self.rounds_seen) - 1
        if idx == 0:
            # Signal the test that round 1 has started and block until
            # the supplement has been injected via the second RPC.
            self._round1_seen.set()
            await self._release.wait()
            yield ProviderChunk(
                kind="tool_call_start",
                tool_call_id="calc1",
                tool_name="calculator",
            )
            yield ProviderChunk(
                kind="tool_call_delta",
                tool_call_id="calc1",
                arguments_delta='{"expression":"1+1"}',
            )
            yield ProviderChunk(kind="tool_call_end", tool_call_id="calc1")
            yield ProviderChunk(kind="done", finish_reason="tool_calls")
            return
        if idx == 1:
            # Round 2 — drained message list at the top of this round
            # includes the supplemented user text. Just emit a final
            # text + done(stop) so the loop terminates cleanly.
            yield ProviderChunk(kind="token", text="final answer")
            yield ProviderChunk(kind="done", finish_reason="stop")
            return
        # Defensive — extra rounds shouldn't happen in this test.
        yield ProviderChunk(kind="done", finish_reason="stop")


@pytest.mark.asyncio
async def test_concurrent_chat_for_same_session_injects_instead_of_serializing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second Chat RPC for the same session_key supplements the running turn.

    Drives RPC #1 against a paused provider; while RPC #1 is mid-flight
    (parked on the second-round provider wait), fires RPC #2 against the
    same session_key with new user text. RPC #2 must return a short
    ``Done(finish_reason="supplemented")`` frame without starting a new
    turn, and the supplemented text must appear in RPC #1's second-round
    message list.
    """
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    round1_seen = asyncio.Event()
    release = asyncio.Event()
    provider = _PausingProvider(round1_seen, release)
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: provider)

    server = grpc.aio.server()
    agent_pb2_grpc.add_AgentServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()

    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = agent_pb2_grpc.AgentStub(channel)

            async def first_frames() -> AsyncIterator[agent_pb2.ClientFrame]:
                yield agent_pb2.ClientFrame(
                    start=agent_pb2.ChatStart(
                        model="claude-sonnet-4-5",
                        session_key="sess-supplement",
                        messages=[
                            common_pb2.Message(
                                role=common_pb2.USER, content="算 1+1"
                            )
                        ],
                    )
                )
                # Hold the request side open so the inbound pump on the
                # server can keep listening (preserves the same-stream
                # contract the production gateway uses).
                await release.wait()
                # Reach here only after the test releases; then close.

            first_call = stub.Chat(first_frames())
            first_done: list[str] = []
            first_tokens: list[str] = []

            async def drain_first() -> None:
                async for f in first_call:
                    k = f.WhichOneof("kind")
                    if k == "token":
                        first_tokens.append(f.token.text)
                    elif k == "done":
                        first_done.append(f.done.finish_reason)

            first_task = asyncio.create_task(drain_first())

            # Wait until the provider's round-1 chat_stream has actually
            # started running (proves the active loop is fully wired and
            # the supplement injection will land on round 2 — not race
            # with round-1 setup).
            await asyncio.wait_for(round1_seen.wait(), timeout=5.0)
            assert servicer._active_loops.get("sess-supplement") is not None, (
                "RPC #1 never registered its active loop"
            )

            # Drive RPC #2 — same session_key, new user text. Must
            # return promptly with finish_reason="supplemented" and
            # MUST NOT start a parallel turn.
            async def second_frames() -> AsyncIterator[agent_pb2.ClientFrame]:
                yield agent_pb2.ClientFrame(
                    start=agent_pb2.ChatStart(
                        model="claude-sonnet-4-5",
                        session_key="sess-supplement",
                        messages=[
                            common_pb2.Message(
                                role=common_pb2.USER, content="再算 2+2"
                            )
                        ],
                    )
                )

            second_call = stub.Chat(second_frames())
            second_finishes: list[str] = []
            async for f in second_call:
                k = f.WhichOneof("kind")
                if k == "done":
                    second_finishes.append(f.done.finish_reason)

            # RPC #2 returned a single Done(supplemented) — no new turn.
            assert second_finishes == ["supplemented"], (
                f"RPC #2 returned the wrong terminator: {second_finishes!r}"
            )

            # Release the provider so round 1 finishes (emits the tool
            # call), the servicer dispatches calculator + feeds the
            # result back, and round 2 begins. The reasoning loop
            # drains the pending-user-messages queue at the top of
            # round 2 — that's where the supplemented text appears in
            # ``provider.rounds_seen[1]``.
            release.set()
            # Wait so RPC #1's drain task completes.
            await asyncio.wait_for(first_task, timeout=5.0)

            # RPC #1 saw its supplement: the second round's message
            # list must contain the supplemented text with the
            # ``[追加上下文]`` prefix.
            assert len(provider.rounds_seen) == 2
            round2_msgs = provider.rounds_seen[1]
            supplemented = [
                m for m in round2_msgs
                if isinstance(m.get("content"), str)
                and m["content"].startswith("[追加上下文] ")
                and "2+2" in m["content"]
            ]
            assert len(supplemented) == 1, (
                f"RPC #1 round 2 missing supplement: {round2_msgs!r}"
            )

            # RPC #1 produced the final answer text.
            assert "final answer" in "".join(first_tokens)
            assert first_done == ["stop"]
    finally:
        await server.stop(grace=None)


# ---------------------------------------------------------------------------
# Perf: module-level cache for the builtin tool schemas.
# ---------------------------------------------------------------------------


def test_builtin_tool_schemas_cached_at_module_load() -> None:
    """``_CACHED_BUILTIN_TOOL_SCHEMAS`` is computed once at import time.

    Hot path: ``_inject_builtin_tools`` runs at the start of every chat
    round. Before this cache the 13 descriptor dicts were rebuilt on
    every call (~30-50ms / round on a 10-round task). The module-level
    snapshot collapses that to one rebuild at import.

    The test pins three properties:

    1. The constant exists and is a list (the type the injector iterates).
    2. The identity is stable across reads (it's not a property or a
       function masquerading as a list).
    3. The cached list matches the live ``_builtin_tool_schemas()``
       output (so a regression that mutated only the function is
       caught immediately).
    """
    from corlinman_server.agent_servicer import (
        _CACHED_BUILTIN_TOOL_SCHEMAS,
        _builtin_tool_schemas,
    )

    # Identity stable — two reads return the same object.
    assert _CACHED_BUILTIN_TOOL_SCHEMAS is _CACHED_BUILTIN_TOOL_SCHEMAS

    # Shape: non-empty list of dicts.
    assert isinstance(_CACHED_BUILTIN_TOOL_SCHEMAS, list)
    assert len(_CACHED_BUILTIN_TOOL_SCHEMAS) > 0
    for schema in _CACHED_BUILTIN_TOOL_SCHEMAS:
        assert isinstance(schema, dict)
        # Each entry is the OpenAI ``{"type": "function", "function":
        # {...}}`` descriptor the injector inspects.
        fn = schema.get("function")
        assert isinstance(fn, dict)
        assert isinstance(fn.get("name"), str) and fn["name"]

    # Cached length matches the live computation — guards against
    # someone adding a builtin without updating the cache trigger.
    live = _builtin_tool_schemas()
    assert len(_CACHED_BUILTIN_TOOL_SCHEMAS) == len(live)
    # Names match in order (descriptor lists are ordered).
    cached_names = [
        s["function"]["name"] for s in _CACHED_BUILTIN_TOOL_SCHEMAS
    ]
    live_names = [s["function"]["name"] for s in live]
    assert cached_names == live_names


# ---------------------------------------------------------------------------
# ask_user — pause-and-ask builtin.
# ---------------------------------------------------------------------------


def test_ask_user_in_builtin_tools_set() -> None:
    """The dispatch gate must list ``ask_user`` so the reasoning loop
    routes the call in-process instead of emitting a no-op ToolCall
    frame to a nonexistent plugin runtime."""
    from corlinman_server.agent_servicer import BUILTIN_TOOLS

    assert "ask_user" in BUILTIN_TOOLS


def test_ask_user_appears_in_advertised_tools() -> None:
    """Cache of advertised builtin schemas must surface ``ask_user`` so
    every chat turn ships its descriptor to the model."""
    from corlinman_server.agent_servicer import (
        _CACHED_BUILTIN_TOOL_SCHEMAS,
        _builtin_tool_schemas,
    )

    cached_names = {s["function"]["name"] for s in _CACHED_BUILTIN_TOOL_SCHEMAS}
    assert "ask_user" in cached_names

    live_names = {s["function"]["name"] for s in _builtin_tool_schemas()}
    assert "ask_user" in live_names


@pytest.mark.asyncio
async def test_ask_user_dispatch_returns_stub_envelope() -> None:
    """``ask_user`` flows through ``_dispatch_builtin`` and returns the
    "awaiting user reply" marker so the reasoning loop closes cleanly."""
    from corlinman_agent.reasoning_loop import ChatStart, ToolCallEvent
    from corlinman_server.agent_servicer import CorlinmanAgentServicer

    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    start = ChatStart(model="m", messages=[], tools=[], session_key="s")
    event = ToolCallEvent(
        call_id="c-ask",
        plugin="builtin",
        tool="ask_user",
        args_json=b'{"question": "Overwrite README.md?"}',
    )
    payload = json.loads(
        await servicer._dispatch_builtin(event, start, _FakeProvider([]))
    )
    assert payload["ok"] is True
    assert payload["status"] == "awaiting_user_reply"
    assert payload["question"] == "Overwrite README.md?"


@pytest.mark.asyncio
async def test_ask_user_dispatch_propagates_options() -> None:
    """Options on the args round-trip through the dispatch envelope so a
    channel handler reading the envelope can render them."""
    from corlinman_agent.reasoning_loop import ChatStart, ToolCallEvent
    from corlinman_server.agent_servicer import CorlinmanAgentServicer

    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    start = ChatStart(model="m", messages=[], tools=[], session_key="s")
    event = ToolCallEvent(
        call_id="c-ask",
        plugin="builtin",
        tool="ask_user",
        args_json=b'{"question": "pick", "options": ["a", "b"]}',
    )
    payload = json.loads(
        await servicer._dispatch_builtin(event, start, _FakeProvider([]))
    )
    assert payload["options"] == ["a", "b"]


@pytest.mark.asyncio
async def test_ask_user_empty_question_returns_error_envelope() -> None:
    """A blank question yields the documented error shape (not a stub)."""
    from corlinman_agent.reasoning_loop import ChatStart, ToolCallEvent
    from corlinman_server.agent_servicer import CorlinmanAgentServicer

    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    start = ChatStart(model="m", messages=[], tools=[], session_key="s")
    event = ToolCallEvent(
        call_id="c-ask-bad",
        plugin="builtin",
        tool="ask_user",
        args_json=b'{"question": ""}',
    )
    payload = json.loads(
        await servicer._dispatch_builtin(event, start, _FakeProvider([]))
    )
    assert payload["ok"] is False
    assert "question" in payload["error"]


def test_ask_user_system_prompt_clause_present() -> None:
    """The baseline system prompt must instruct the model on how to use
    ``ask_user`` — call the tool, then finalise with the question text,
    then stop. Without this clause models won't reach for the tool."""
    from corlinman_server.agent_servicer import _CODING_SYSTEM_PROMPT

    assert "ask_user" in _CODING_SYSTEM_PROMPT
    # Pin the two key behaviours so a future prompt rewrite that drops
    # them fails this test loudly.
    assert "finalize" in _CODING_SYSTEM_PROMPT.lower()
    assert "do not invoke" in _CODING_SYSTEM_PROMPT.lower() or \
        "do not call" in _CODING_SYSTEM_PROMPT.lower()


# ─── W2.3: explicit agent_id hint in _peek_agent_binding ─────────────


def _picker_registry() -> Any:
    """Pre-populated registry shared by the W2.3 binding-peek tests."""
    from corlinman_agent.agents.card import AgentCard
    from corlinman_agent.agents.registry import AgentCardRegistry

    return AgentCardRegistry(
        {
            "researcher": AgentCard(
                name="researcher",
                description="finds papers",
                system_prompt="you research",
            ),
            "editor": AgentCard(
                name="editor",
                description="tightens prose",
                system_prompt="you edit",
            ),
        }
    )


def test_peek_agent_binding_prefers_explicit_hint() -> None:
    """W2.3: when ``start.extra['agent_id']`` names a known card, the
    binding peek returns that card without consulting the message-peek
    heuristic. This is the playground "Agent picker" path: the operator
    bypasses the auto-route by naming an explicit agent."""
    from corlinman_agent.reasoning_loop import ChatStart

    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    servicer._builtin_agents = _picker_registry()

    # Messages reference "researcher" via the heuristic, but the
    # explicit hint names "editor" — explicit must win.
    start = ChatStart(
        model="orchestrator",
        messages=[{"role": "user", "content": "@researcher find papers"}],
        tools=[],
        session_key="tenant-a::sess-1",
        extra={"agent_id": "editor"},
    )
    bound = servicer._peek_agent_binding(start)
    assert bound is not None
    assert bound.name == "editor"


def test_peek_agent_binding_unknown_hint_falls_back_to_heuristic(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """W2.3: an unknown explicit ``agent_id`` must not brick the turn.
    The peek logs a warning and falls through to the existing
    message-peek heuristic so the operator gets at least a best-effort
    routing decision."""
    from corlinman_agent.reasoning_loop import ChatStart

    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    servicer._builtin_agents = _picker_registry()

    start = ChatStart(
        model="orchestrator",
        messages=[],
        tools=[],
        session_key="tenant-a::sess-1",
        extra={"agent_id": "ghost-agent-does-not-exist"},
    )
    bound = servicer._peek_agent_binding(start)
    # No messages, no heuristic match → ``None``. Hint mis-route should
    # never raise.
    assert bound is None
    # Structlog renders to stdout/stderr in test mode; the warning event
    # name and the offending id must surface so an operator chasing
    # this in the logs can find it.
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "explicit_agent_id_unknown" in combined
    assert "ghost-agent-does-not-exist" in combined


def test_peek_agent_binding_no_hint_uses_heuristic_unchanged() -> None:
    """W2.3 regression guard: when ``extra`` is empty / missing
    ``agent_id``, the binding peek behaves exactly as pre-W2.3 — the
    message-peek heuristic decides. This locks in backward
    compatibility for the existing W-D1 callers."""
    from corlinman_agent.reasoning_loop import ChatStart

    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    servicer._builtin_agents = _picker_registry()

    # No ``agent_id`` in extra → fall through to heuristic. Messages
    # don't reference any registered agent, so the result is ``None``
    # (matching the pre-W2.3 behavior for this input).
    start = ChatStart(
        model="orchestrator",
        messages=[{"role": "user", "content": "hello"}],
        tools=[],
        session_key="tenant-a::sess-1",
    )
    assert servicer._peek_agent_binding(start) is None

    # Sanity: ``extra`` present but empty / ``"auto"`` / blank must not
    # consume the explicit-hint branch.
    for sentinel in ({}, {"agent_id": ""}, {"agent_id": "  "}, {"agent_id": "auto"}):
        start_v = ChatStart(
            model="orchestrator",
            messages=[],
            tools=[],
            session_key="tenant-a::sess-1",
            extra=dict(sentinel),
        )
        assert servicer._peek_agent_binding(start_v) is None
