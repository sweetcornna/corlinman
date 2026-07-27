"""W3-3 e2e: the cross-surface prompt channel over the gRPC stream.

Drives the real servicer with a provider that calls an external tool
guarded by an ``ask`` rule and asserts the full frame choreography:

* ``approval_capable=True`` → an ``AwaitingApproval`` server frame is
  emitted; an approving ``ApprovalDecision`` client frame lets the tool
  frame through; a denying one feeds the model an ``approval_denied``
  envelope.
* an approving decision with ``scope="session"`` lands in the shared
  GrantStore (keyed with args) so the identical call stops prompting.
* no decision within the ask timeout → fail-closed deny with a
  user-visible "timed out" reason (acceptance 7).
* ``approval_capable=False`` (--print / attach / legacy callers) →
  fail-closed ``authz_no_channel`` with NO AwaitingApproval frame
  (acceptance 4).
* ``approval_requested`` / ``approval_decided`` hook events fire
  (acceptance 6).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import grpc
import grpc.aio
import pytest
from corlinman_agent.authz.defaults import apply_permissions_config
from corlinman_agent.authz.grants import get_grant_store
from corlinman_agent.authz.model import Subject
from corlinman_grpc import agent_pb2, agent_pb2_grpc
from corlinman_providers.base import ProviderChunk
from corlinman_server.agent_servicer import CorlinmanAgentServicer

_EXTERNAL_TOOL = "github_create_issue"
_TOOL_ARGS = {"title": "hi"}


def _tool_call_chunks(tool: str, args: dict[str, Any]) -> list[ProviderChunk]:
    return [
        ProviderChunk(kind="tool_call_start", tool_call_id="call-apr-1", tool_name=tool),
        ProviderChunk(
            kind="tool_call_delta",
            tool_call_id="call-apr-1",
            arguments_delta=json.dumps(args),
        ),
        ProviderChunk(kind="tool_call_end", tool_call_id="call-apr-1"),
        ProviderChunk(kind="done", finish_reason="tool_calls"),
    ]


class _ToolThenDoneProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.round2_messages: list[dict[str, Any]] = []

    async def chat_stream(self, **kwargs: Any) -> AsyncIterator[ProviderChunk]:
        self.calls += 1
        if self.calls == 1:
            for chunk in _tool_call_chunks(_EXTERNAL_TOOL, _TOOL_ARGS):
                yield chunk
            return
        self.round2_messages = list(kwargs.get("messages") or [])
        yield ProviderChunk(kind="done", finish_reason="stop")


class _Driver:
    """One Chat RPC with scripted approval decisions."""

    def __init__(
        self,
        servicer: CorlinmanAgentServicer,
        *,
        approval_capable: bool,
        decision: tuple[bool, str, str] | None,
        binding: Any | None = None,
    ) -> None:
        self._servicer = servicer
        self._approval_capable = approval_capable
        #: (approved, scope, deny_message); None = never answer.
        self._decision = decision
        self._binding = binding
        self.awaiting_frames: list[agent_pb2.AwaitingApproval] = []
        self.external_tool_frames: list[agent_pb2.ToolCall] = []

    async def run(self) -> None:
        server = grpc.aio.server()
        agent_pb2_grpc.add_AgentServicer_to_server(self._servicer, server)
        port = server.add_insecure_port("127.0.0.1:0")
        await server.start()
        outbound: asyncio.Queue[agent_pb2.ClientFrame] = asyncio.Queue()
        try:
            async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
                stub = agent_pb2_grpc.AgentStub(channel)

                async def client_frames() -> AsyncIterator[agent_pb2.ClientFrame]:
                    start = agent_pb2.ChatStart(
                        model="m",
                        session_key="t::s-appr",
                        approval_capable=self._approval_capable,
                    )
                    if self._binding is not None:
                        start.binding.CopyFrom(self._binding)
                    yield agent_pb2.ClientFrame(start=start)
                    while True:
                        yield await outbound.get()

                async for frame in stub.Chat(client_frames()):
                    kind = frame.WhichOneof("kind")
                    if kind == "awaiting":
                        self.awaiting_frames.append(frame.awaiting)
                        if self._decision is not None:
                            approved, scope, deny_message = self._decision
                            await outbound.put(
                                agent_pb2.ClientFrame(
                                    approval=agent_pb2.ApprovalDecision(
                                        call_id=frame.awaiting.call_id,
                                        approved=approved,
                                        scope=scope,
                                        deny_message=deny_message,
                                    )
                                )
                            )
                    elif kind == "tool_call" and not frame.tool_call.plugin.startswith(
                        "_builtin"
                    ):
                        self.external_tool_frames.append(frame.tool_call)
                        await outbound.put(
                            agent_pb2.ClientFrame(
                                tool_result=agent_pb2.ToolResult(
                                    call_id=frame.tool_call.call_id,
                                    result_json=b'{"ok": true}',
                                    is_error=False,
                                    duration_ms=1,
                                )
                            )
                        )
        finally:
            await self._servicer.aclose()
            await server.stop(grace=None)


def _ask_rule() -> None:
    apply_permissions_config({"rules": [{"tool": "mcp:github/*", "action": "ask"}]})


@pytest.mark.asyncio
async def test_ask_emits_awaiting_and_approval_lets_tool_through() -> None:
    _ask_rule()
    provider = _ToolThenDoneProvider()
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: provider)
    driver = _Driver(
        servicer, approval_capable=True, decision=(True, "once", "")
    )
    await driver.run()

    assert len(driver.awaiting_frames) == 1
    aw = driver.awaiting_frames[0]
    assert aw.tool == _EXTERNAL_TOOL
    assert aw.call_id
    assert b"hi" in aw.args_preview_json
    # Approved → the external ToolCall frame reached the gateway side.
    assert [f.tool for f in driver.external_tool_frames] == [_EXTERNAL_TOOL]


@pytest.mark.asyncio
async def test_deny_decision_blocks_tool_and_feeds_reason() -> None:
    _ask_rule()
    provider = _ToolThenDoneProvider()
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: provider)
    driver = _Driver(
        servicer,
        approval_capable=True,
        decision=(False, "once", "nope, not today"),
    )
    await driver.run()

    assert len(driver.awaiting_frames) == 1
    assert driver.external_tool_frames == []
    round2 = json.dumps(provider.round2_messages)
    assert "approval_denied" in round2
    assert "nope, not today" in round2


@pytest.mark.asyncio
async def test_session_scope_approval_records_grant() -> None:
    _ask_rule()
    provider = _ToolThenDoneProvider()
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: provider)
    driver = _Driver(
        servicer, approval_capable=True, decision=(True, "session", "")
    )
    await driver.run()

    assert len(driver.awaiting_frames) == 1
    # The grant is keyed under the FULL subject (tenant from the
    # session-key prefix) + the stable external grant key (the bare
    # advertised name — ``external_candidate_keys(...)[0]``) + the args
    # digest, which is exactly what ``AuthzGate.resolve_external``
    # consults before re-asking.
    subject = Subject(session_key="t::s-appr", tenant_id="t")
    grants = get_grant_store()
    assert grants.is_granted(subject, _EXTERNAL_TOOL, _TOOL_ARGS)
    # A different argument surface still prompts (arg-digest keying).
    assert not grants.is_granted(subject, _EXTERNAL_TOOL, {"title": "other"})


@pytest.mark.asyncio
async def test_timeout_fail_closes_with_visible_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import corlinman_server.agent_servicer as servicer_mod

    monkeypatch.setattr(servicer_mod, "APPROVAL_ASK_TIMEOUT_S", 0.3)
    _ask_rule()
    provider = _ToolThenDoneProvider()
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: provider)
    driver = _Driver(servicer, approval_capable=True, decision=None)
    await driver.run()

    assert len(driver.awaiting_frames) == 1
    assert driver.external_tool_frames == []
    round2 = json.dumps(provider.round2_messages)
    assert "approval_denied" in round2
    assert "timed out" in round2


@pytest.mark.asyncio
async def test_not_capable_fail_closes_authz_no_channel() -> None:
    """--print / attach / legacy callers: no AwaitingApproval frame is
    ever emitted and the envelope is the diagnosable authz_no_channel."""
    _ask_rule()
    provider = _ToolThenDoneProvider()
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: provider)
    driver = _Driver(servicer, approval_capable=False, decision=None)
    await driver.run()

    assert driver.awaiting_frames == []
    assert driver.external_tool_frames == []
    assert "authz_no_channel" in json.dumps(provider.round2_messages)


@pytest.mark.asyncio
async def test_channel_turn_increments_surface_labelled_metric() -> None:
    """Acceptance 5: corlinman_approvals_total{decision,surface} counts a
    CHANNEL turn's decision under its channel surface (previously only
    the console path ever incremented, with no surface dimension)."""
    from corlinman_grpc import common_pb2
    from corlinman_server.gateway.core.metrics import APPROVALS_TOTAL

    _ask_rule()
    provider = _ToolThenDoneProvider()
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: provider)
    binding = common_pb2.ChannelBinding(
        channel="qq", account="100", thread="12345", sender="555"
    )
    before = APPROVALS_TOTAL.labels(decision="approved", surface="qq")._value.get()
    driver = _Driver(
        servicer,
        approval_capable=True,
        decision=(True, "once", ""),
        binding=binding,
    )
    await driver.run()

    after = APPROVALS_TOTAL.labels(decision="approved", surface="qq")._value.get()
    assert after == before + 1


@pytest.mark.asyncio
async def test_subagent_ask_flows_to_the_same_prompt_channel() -> None:
    """A child executor's ask lands on the SAME per-turn prompt channel
    the parent stream owns (contextvar inheritance), the subject is the
    subagent surface with the parent's session key — so an approving
    grant belongs to the parent session, never to the child alone."""
    from corlinman_agent.authz import AuthzAnswer
    from corlinman_server import agent_servicer as servicer_mod

    _ask_rule()
    provider = _ToolThenDoneProvider()
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: provider)

    requests: list[Any] = []

    class _FakeBridge:
        async def request(self, req: Any) -> AuthzAnswer:
            requests.append(req)
            return AuthzAnswer(approved=True)

    from corlinman_agent.reasoning_loop import ChatStart, ToolCallEvent

    token = servicer_mod._ACTIVE_PROMPT_CHANNEL.set(_FakeBridge())  # type: ignore[arg-type]
    try:
        start = ChatStart(
            model="m", messages=[], tools=[], session_key="t::parent"
        )
        execute = servicer._make_child_tool_executor(start, provider, None)
        event = ToolCallEvent(
            call_id="call-child-1",
            plugin=_EXTERNAL_TOOL,
            tool=_EXTERNAL_TOOL,
            args_json=json.dumps(_TOOL_ARGS).encode("utf-8"),
        )
        result = await execute(event)
    finally:
        servicer_mod._ACTIVE_PROMPT_CHANNEL.reset(token)
        await servicer.aclose()

    # The ask was APPROVED through the bridge, so dispatch proceeded past
    # the gate — this bare harness has no plugin runtime, so the call then
    # falls through to the unknown-builtin envelope (crucially NOT an
    # approval_denied / authz_no_channel one).
    assert "unknown_builtin_tool" in json.loads(result)["error"]
    assert len(requests) == 1
    subject = requests[0].subject
    assert subject.surface == "subagent"
    assert subject.session_key == "t::parent"


@pytest.mark.asyncio
async def test_hook_events_fire_around_the_ask() -> None:
    _ask_rule()
    provider = _ToolThenDoneProvider()
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: provider)

    events: list[Any] = []

    class _Bus:
        async def emit(self, event: Any) -> None:
            events.append(event)

    servicer._hook_bus = _Bus()
    driver = _Driver(
        servicer, approval_capable=True, decision=(False, "once", "no")
    )
    await driver.run()

    kinds = [getattr(type(e), "KIND", "") for e in events]
    assert "approval_requested" in kinds
    assert "approval_decided" in kinds
    decided = next(e for e in events if getattr(type(e), "KIND", "") == "approval_decided")
    assert decided.decision == "deny"
