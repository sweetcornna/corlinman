"""W3-3 gateway side: AwaitingApproval forwarding + the approval broker.

The chat service used to silently drop ``awaiting`` frames (M5 scope);
these tests pin the new behaviour: the frame surfaces as
:class:`AwaitingApprovalEvent`, is registered with the process-global
broker, and a ``decide()`` lands the ``ApprovalDecision`` client frame on
THIS stream's ``tx`` queue. Stream teardown force-unregisters leftovers.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from corlinman_grpc._generated.corlinman.v1 import agent_pb2
from corlinman_server.gateway.services.approval_broker import (
    get_approval_broker,
    reset_approval_broker,
)
from corlinman_server.gateway.services.chat_service import ChatService, _build_chat_start
from corlinman_server.gateway_api import (
    AwaitingApprovalEvent,
    DoneEvent,
    InternalChatRequest,
    Message,
    Role,
)


@pytest.fixture(autouse=True)
def _fresh_broker() -> Any:
    reset_approval_broker()
    yield
    reset_approval_broker()


class _ScriptedBackend:
    """Yields the scripted frames; records the tx queue handed out."""

    def __init__(self, frames: list[agent_pb2.ServerFrame], *, hold: bool = False):
        self._frames = frames
        self.tx: asyncio.Queue[Any] = asyncio.Queue()
        #: When set, the rx iterator parks after the scripted frames until
        #: released — models an agent stream waiting on a decision.
        self._release = asyncio.Event()
        self._hold = hold

    def release(self) -> None:
        self._release.set()

    async def start(
        self, start: agent_pb2.ChatStart
    ) -> tuple[asyncio.Queue[Any], AsyncIterator[agent_pb2.ServerFrame]]:
        async def _rx() -> AsyncIterator[agent_pb2.ServerFrame]:
            for frame in self._frames:
                yield frame
            if self._hold:
                await self._release.wait()
            yield agent_pb2.ServerFrame(done=agent_pb2.Done(finish_reason="stop"))

        return self.tx, _rx()


def _awaiting_frame() -> agent_pb2.ServerFrame:
    return agent_pb2.ServerFrame(
        awaiting=agent_pb2.AwaitingApproval(
            call_id="call-w33",
            plugin="",
            tool="github_create_issue",
            args_preview_json=b'{"title": "hi"}',
            reason="permission rule requires approval",
        )
    )


def _request() -> InternalChatRequest:
    return InternalChatRequest(
        model="m",
        messages=[Message(role=Role.USER, content="hello")],
        session_key="t::s1",
        stream=True,
    )


@pytest.mark.asyncio
async def test_awaiting_frame_surfaces_event_and_registers_broker() -> None:
    backend = _ScriptedBackend([_awaiting_frame()], hold=True)
    service = ChatService(backend)
    cancel = asyncio.Event()

    events: list[Any] = []
    stream = service.run(_request(), cancel)
    # First event must be the awaiting-approval surface.
    first = await stream.__anext__()
    events.append(first)
    assert isinstance(first, AwaitingApprovalEvent)
    assert first.call_id == "call-w33"
    assert first.tool == "github_create_issue"
    assert first.args_preview_json == '{"title": "hi"}'

    broker = get_approval_broker()
    assert broker.pending_ids() == ["call-w33"]

    # Decide → the ApprovalDecision client frame lands on THIS stream's tx.
    delivered = await broker.decide(
        "call-w33", approved=True, scope="session", deny_message=""
    )
    assert delivered is True
    frame = backend.tx.get_nowait()
    assert frame.WhichOneof("kind") == "approval"
    assert frame.approval.call_id == "call-w33"
    assert frame.approval.approved is True
    assert frame.approval.scope == "session"
    # Entry consumed — a second decide is a not-found.
    assert await broker.decide("call-w33", approved=True) is False

    backend.release()
    async for ev in stream:
        events.append(ev)
    assert isinstance(events[-1], DoneEvent)


@pytest.mark.asyncio
async def test_stream_teardown_unregisters_pending_approvals() -> None:
    backend = _ScriptedBackend([_awaiting_frame()])
    service = ChatService(backend)
    cancel = asyncio.Event()

    events = [ev async for ev in service.run(_request(), cancel)]
    assert any(isinstance(ev, AwaitingApprovalEvent) for ev in events)
    # The stream completed → the registration must be gone.
    assert get_approval_broker().pending_ids() == []


def test_build_chat_start_carries_approval_capable() -> None:
    req = _request()
    assert _build_chat_start(req).approval_capable is False
    req.approval_capable = True
    assert _build_chat_start(req).approval_capable is True
