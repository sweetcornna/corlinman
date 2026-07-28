"""Behavior-config hot-apply — the running channel picks up in-place
mutations of ``params.config`` (what a behavior-only reconcile does)
without a restart: router gates per event, proactive config per beat,
monitor specs per tick."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_channels import service as svc
from corlinman_channels.common import ChannelBinding, InboundEvent
from corlinman_channels.onebot import (
    MessageEvent,
    MessageType,
    TextSegment,
)
from corlinman_channels.router import ChannelRouter


@pytest.fixture(autouse=True)
def _clean_globals() -> None:
    svc._QQ_GROUP_SPEECH._events.clear()
    svc._QQ_GROUP_RECENT.clear()
    svc._QQ_PROACTIVE_SENT.clear()
    svc._QQ_PROACTIVE_LAST_MONO.clear()
    svc._QQ_MONITOR_STATUS.clear()
    svc._QQ_MONITOR_PENDING_TRIGGERS.clear()
    svc._QQ_MONITOR_WAKE.clear()
    yield
    svc._QQ_MONITOR_STATUS.clear()
    svc._QQ_MONITOR_PENDING_TRIGGERS.clear()
    svc._QQ_MONITOR_WAKE.clear()


def _group_event(i: int, text: str, group: int = 42) -> InboundEvent[MessageEvent]:
    msg = MessageEvent(
        self_id=100,
        message_type=MessageType.GROUP,
        sub_type="normal",
        group_id=group,
        user_id=2000 + i,
        message_id=i,
        message=[TextSegment(text=text)],
        raw_message=text,
        time=0,
        sender=SimpleNamespace(user_id=2000 + i, nickname=f"u{i}", card=None),  # type: ignore[arg-type]
    )
    return InboundEvent(
        channel="qq",
        binding=ChannelBinding.qq_group(100, group, msg.user_id),
        text=text,
        message_id=str(i),
        timestamp=0,
        mentioned=False,
        payload=msg,
    )


class _MutatingAdapter:
    """Yields events, running a callback between the first and second."""

    def __init__(
        self, events: list[InboundEvent[MessageEvent]], between: Any = None
    ) -> None:
        self.events = events
        self.between = between
        self.sent: list[Any] = []

    async def inbound(self):  # type: ignore[no-untyped-def]
        for i, ev in enumerate(self.events):
            yield ev
            if i == 0 and self.between is not None:
                self.between()
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


class TestDispatchGateHotApply:
    @pytest.mark.asyncio
    async def test_keyword_added_in_place_routes_next_event(self) -> None:
        """Event 1: no keywords → non-mention dropped. Config mutated in
        place (what hot-apply does) → event 2 matches the new keyword."""
        config: dict[str, Any] = {"ws_url": "ws://x", "self_ids": [100]}

        def _add_keyword() -> None:
            config["group_keywords"] = {"42": ["ping"]}

        events = [_group_event(0, "ping 帮个忙"), _group_event(1, "ping 再来一次")]
        adapter = _MutatingAdapter(events, between=_add_keyword)
        chat = _CountingChat()
        params = svc.QqChannelParams(
            config=config, model="m", chat_service=chat, instance_id="default"
        )
        gates = svc._qq_router_gates(config)
        router = ChannelRouter(
            group_keywords=gates[0],
            self_ids=[100],
            group_replies_enabled=gates[1],
            group_whitelist=gates[2],
            group_reply_policy=gates[3],
            group_reply_cooldown_secs=gates[4],
        )
        cancel = asyncio.Event()

        async def stop_soon() -> None:
            for _ in range(300):
                await asyncio.sleep(0.01)
                if chat.calls >= 1:
                    break
            await asyncio.sleep(0.05)
            cancel.set()

        await asyncio.gather(
            svc._qq_dispatch_loop(adapter, router, params, cancel),  # type: ignore[arg-type]
            stop_soon(),
        )
        # Only the SECOND event (after the hot keyword add) dispatched.
        assert chat.calls == 1

    @pytest.mark.asyncio
    async def test_whitelist_added_in_place_mutes_next_event(self) -> None:
        """Mention-policy 'all' answers event 1; an in-place whitelist
        (not containing the group) mutes event 2."""
        config: dict[str, Any] = {
            "ws_url": "ws://x",
            "self_ids": [100],
            "group_reply_policy": "all",
            "group_reply_cooldown_secs": 0,
        }

        def _add_whitelist() -> None:
            config["group_whitelist"] = ["999"]

        events = [_group_event(0, "第一条"), _group_event(1, "第二条")]
        adapter = _MutatingAdapter(events, between=_add_whitelist)
        chat = _CountingChat()
        params = svc.QqChannelParams(
            config=config, model="m", chat_service=chat, instance_id="default"
        )
        gates = svc._qq_router_gates(config)
        router = ChannelRouter(
            group_keywords=gates[0],
            self_ids=[100],
            group_replies_enabled=gates[1],
            group_whitelist=gates[2],
            group_reply_policy=gates[3],
            group_reply_cooldown_secs=gates[4],
        )
        cancel = asyncio.Event()

        async def stop_soon() -> None:
            for _ in range(300):
                await asyncio.sleep(0.01)
                if chat.calls >= 1:
                    break
            await asyncio.sleep(0.05)
            cancel.set()

        await asyncio.gather(
            svc._qq_dispatch_loop(adapter, router, params, cancel),  # type: ignore[arg-type]
            stop_soon(),
        )
        assert chat.calls == 1  # second event silenced by the hot whitelist


class _EchoChat:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls = 0

    def run(self, request, cancel):  # noqa: ANN001
        self.calls += 1
        reply = self.reply

        async def _stream():
            yield SimpleNamespace(kind="token_delta", text=reply)
            yield SimpleNamespace(kind="done")

        return _stream()


class _FakeSendAdapter:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send_action(self, action: Any) -> None:
        self.sent.append(action)


_ONLINE = {"online": True, "account_online": True, "account_qq": 10001}


class TestProactiveHotApply:
    @pytest.mark.asyncio
    async def test_enable_mid_loop_takes_effect(self, monkeypatch) -> None:
        """Loop starts with proactive DISABLED; the config is mutated
        while it sleeps — the very next beat posts."""
        monkeypatch.setattr(
            svc, "_qq_proactive_now_parts", lambda tz: ("2026-07-29", 12)
        )
        cfg_ns = SimpleNamespace()  # proactive off
        calls = {"n": 0}

        async def _sleep(cancel, secs):  # noqa: ANN001
            calls["n"] += 1
            if calls["n"] == 1:
                cfg_ns.proactive_enabled = True
                cfg_ns.proactive_groups = [42]
                return False
            return True

        monkeypatch.setattr(svc, "_qq_proactive_sleep", _sleep)
        adapter = _FakeSendAdapter()
        chat = _EchoChat("热启用后冒个泡")
        params = svc.QqChannelParams(
            config=cfg_ns, model="m1", chat_service=chat, instance_id="default"
        )
        await svc._qq_proactive_loop(
            adapter, params, None, asyncio.Event(), health=dict(_ONLINE)
        )
        assert chat.calls == 1
        assert len(adapter.sent) == 1

    @pytest.mark.asyncio
    async def test_disable_mid_loop_goes_silent(self, monkeypatch) -> None:
        monkeypatch.setattr(
            svc, "_qq_proactive_now_parts", lambda tz: ("2026-07-29", 12)
        )
        cfg_ns = SimpleNamespace(proactive_enabled=True, proactive_groups=[42])
        calls = {"n": 0}

        async def _sleep(cancel, secs):  # noqa: ANN001
            calls["n"] += 1
            if calls["n"] == 1:
                cfg_ns.proactive_enabled = False
                return False
            return True

        monkeypatch.setattr(svc, "_qq_proactive_sleep", _sleep)
        adapter = _FakeSendAdapter()
        chat = _EchoChat("不该出现")
        params = svc.QqChannelParams(
            config=cfg_ns, model="m1", chat_service=chat, instance_id="default"
        )
        cfg = svc._qq_proactive_config(cfg_ns, None)
        await svc._qq_proactive_loop(
            adapter, params, cfg, asyncio.Event(), health=dict(_ONLINE)
        )
        assert chat.calls == 0
        assert adapter.sent == []


class _FakeHistory:
    def __init__(self, messages: list[SimpleNamespace] | None = None) -> None:
        self.messages = messages or []
        self.last_fire: dict[str, int] = {}

    async def list_window(self, **kwargs: object) -> list[SimpleNamespace]:
        return list(self.messages)

    async def get_last_fire(self, key: str) -> int | None:
        return self.last_fire.get(key)

    async def set_last_fire(self, key: str, ts_ms: int) -> None:
        self.last_fire[key] = ts_ms

    async def prune(self, *, older_than_ms: int) -> int:
        return 0


class _RecordingChat:
    def __init__(self) -> None:
        self.requests: list[Any] = []

    def run(self, request: Any, cancel: Any):  # type: ignore[no-untyped-def]
        self.requests.append(request)

        async def _stream():
            yield SimpleNamespace(kind="token_delta", text="汇总好了")
            yield SimpleNamespace(kind="done")

        return _stream()


class TestMonitorHotApply:
    @pytest.mark.asyncio
    async def test_rule_added_in_place_activates(self, monkeypatch) -> None:
        """Digest loop starts with ZERO rules; a monitor rule is added to
        the live config → the next tick picks it up and a manual trigger
        produces a digest — no restart."""
        monkeypatch.setattr(svc, "_QQ_MONITOR_TICK_SECS", 0.02)
        cfg_ns = SimpleNamespace()
        history = _FakeHistory(
            [
                SimpleNamespace(
                    sender_user_id="11",
                    sender_name="小明",
                    received_at_ms=1_000,
                    text="热添加前的消息",
                )
            ]
        )
        chat = _RecordingChat()
        adapter = _FakeSendAdapter()
        params = svc.QqChannelParams(
            config=cfg_ns,
            model="m1",
            chat_service=chat,
            instance_id="inst",
            group_history=history,
        )
        cancel = asyncio.Event()
        task = asyncio.create_task(
            svc._qq_monitor_digest_loop(
                adapter,  # type: ignore[arg-type]
                params,
                (),
                cancel,
                health=dict(_ONLINE),
            )
        )
        await asyncio.sleep(0.05)  # a few empty ticks
        assert chat.requests == []
        cfg_ns.monitors = [
            {
                "id": "hotrule",
                "sources": [{"group": "123"}],
                "schedule_type": "interval",
                "interval_minutes": 60,
                "target_type": "group",
                "target_id": "456",
            }
        ]
        await asyncio.sleep(0.06)  # tick resolves the hot-added spec
        svc._QQ_MONITOR_PENDING_TRIGGERS["inst"] = {"hotrule"}
        wake = svc._QQ_MONITOR_WAKE.get("inst")
        assert wake is not None
        wake.set()
        for _ in range(200):
            await asyncio.sleep(0.01)
            if chat.requests:
                break
        cancel.set()
        wake.set()
        await task
        assert chat.requests, "hot-added monitor rule never produced a digest"
        assert "热添加前的消息" in chat.requests[0].messages[0].content
        assert adapter.sent, "digest was not delivered"
