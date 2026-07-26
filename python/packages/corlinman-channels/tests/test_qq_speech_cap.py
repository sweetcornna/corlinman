"""QQ group speech cap — the shared N-messages-per-M-minutes hard gate.

The cap is enforced in ``_qq_dispatch_loop`` (reactive path, mentions NOT
exempt) and consulted by ``_qq_proactive_loop`` (covered in
``test_qq_proactive.py``). Both count into the module-level
``_QQ_GROUP_SPEECH`` window so replies and proactive posts share one
per-group budget that survives channel-task restarts.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_channels import service as svc
from corlinman_channels.common import ChannelBinding, InboundEvent
from corlinman_channels.onebot import (
    AtSegment,
    MessageEvent,
    MessageType,
    TextSegment,
)
from corlinman_channels.router import ChannelRouter


@pytest.fixture(autouse=True)
def _reset_speech_state() -> None:
    svc._QQ_GROUP_SPEECH._events.clear()
    svc._QQ_GROUP_RECENT.clear()


def _group_mention_event(i: int, group: int = 42, self_id: int = 100) -> InboundEvent[MessageEvent]:
    msg = MessageEvent(
        self_id=self_id,
        message_type=MessageType.GROUP,
        sub_type="normal",
        group_id=group,
        user_id=2000 + i,
        message_id=i,
        message=[AtSegment(qq=str(self_id)), TextSegment(text=f"在吗 {i}")],
        raw_message=f"在吗 {i}",
        time=0,
        sender=SimpleNamespace(user_id=2000 + i, nickname=f"user{i}", card=None),  # type: ignore[arg-type]
    )
    return InboundEvent(
        channel="qq",
        binding=ChannelBinding.qq_group(self_id, group, msg.user_id),
        text=msg.raw_message,
        message_id=str(msg.message_id),
        timestamp=0,
        mentioned=True,
        payload=msg,
    )


class _FakeAdapter:
    def __init__(self, events: list[InboundEvent[MessageEvent]]) -> None:
        self.events = events
        self.sent: list[Any] = []

    async def inbound(self):  # type: ignore[no-untyped-def]
        for ev in self.events:
            yield ev
        await asyncio.Event().wait()

    async def send_action(self, action: Any) -> None:
        self.sent.append(action)


class _CountingChat:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, request: Any, cancel: Any):  # type: ignore[no-untyped-def]
        self.calls += 1

        async def _gen():
            yield SimpleNamespace(kind="token_delta", text="ok")
            yield SimpleNamespace(kind="done")

        return _gen()


async def _run_dispatch(
    events: list[InboundEvent[MessageEvent]],
    config: SimpleNamespace,
    chat: _CountingChat,
    hook_calls: list[tuple[str, str]],
) -> _FakeAdapter:
    adapter = _FakeAdapter(events)
    router = ChannelRouter(self_ids=[100])
    params = svc.QqChannelParams(
        config=config,
        model="m",
        chat_service=chat,  # type: ignore[arg-type]
        rate_limit_hook=lambda ch, reason: hook_calls.append((ch, reason)),
    )
    cancel = asyncio.Event()

    async def stop_soon() -> None:
        for _ in range(300):
            await asyncio.sleep(0.01)
            if chat.calls + len(hook_calls) >= len(events):
                break
        await asyncio.sleep(0.05)
        cancel.set()

    await asyncio.gather(
        svc._qq_dispatch_loop(adapter, router, params, cancel),  # type: ignore[arg-type]
        stop_soon(),
    )
    return adapter


class TestGroupSpeechCap:
    @pytest.mark.asyncio
    async def test_cap_drops_over_budget_even_for_mentions(self) -> None:
        events = [_group_mention_event(i) for i in range(4)]
        chat = _CountingChat()
        hooks: list[tuple[str, str]] = []
        config = SimpleNamespace(
            ws_url="ws://x",
            self_ids=[100],
            group_rate_limit_window_minutes=10,
            group_rate_limit_max_messages=2,
        )
        await _run_dispatch(events, config, chat, hooks)
        assert chat.calls == 2
        assert hooks.count(("qq", "group_window")) == 2

    @pytest.mark.asyncio
    async def test_cap_disabled_by_default(self) -> None:
        events = [_group_mention_event(i) for i in range(4)]
        chat = _CountingChat()
        hooks: list[tuple[str, str]] = []
        config = SimpleNamespace(ws_url="ws://x", self_ids=[100])
        await _run_dispatch(events, config, chat, hooks)
        assert chat.calls == 4
        assert hooks == []

    @pytest.mark.asyncio
    async def test_inbound_group_chatter_feeds_context_buffer(self) -> None:
        events = [_group_mention_event(i) for i in range(2)]
        chat = _CountingChat()
        config = SimpleNamespace(ws_url="ws://x", self_ids=[100])
        await _run_dispatch(events, config, chat, [])
        buf = svc._QQ_GROUP_RECENT.get("default:42")
        assert buf is not None and len(buf) == 2
        # Sender display name and text are captured for proactive context.
        assert buf[0][1] == "user0"
        assert "在吗 0" in buf[0][2]
