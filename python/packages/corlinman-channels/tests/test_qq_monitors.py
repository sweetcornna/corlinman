"""``_qq_monitor_*`` — scheduled group-digest monitor helpers."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from corlinman_channels import service as svc
from corlinman_channels.onebot import (
    MessageEvent,
    MessageType,
    Sender,
    SendGroupMsg,
    SendPrivateMsg,
    TextSegment,
)
from corlinman_channels.router import ChannelRouter


class _Cfg(SimpleNamespace):
    """Attribute-style config stub matching ``_attr`` reads."""


def _entry(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": "m1",
        "sources": [{"group": "123"}],
        "schedule_type": "interval",
        "interval_minutes": 60,
        "target_type": "group",
        "target_id": "456",
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def _clean_monitor_globals() -> None:
    svc._QQ_MONITOR_STATUS.clear()
    svc._QQ_MONITOR_PENDING_TRIGGERS.clear()
    svc._QQ_MONITOR_WAKE.clear()
    yield
    svc._QQ_MONITOR_STATUS.clear()
    svc._QQ_MONITOR_PENDING_TRIGGERS.clear()
    svc._QQ_MONITOR_WAKE.clear()


class TestMonitorsConfig:
    def test_absent_or_empty_is_off(self) -> None:
        assert svc._qq_monitors_config(_Cfg()) is None
        assert svc._qq_monitors_config(_Cfg(monitors=[])) is None
        assert svc._qq_monitors_config(_Cfg(monitors="junk")) is None

    def test_interval_entry_parses_with_window_default(self) -> None:
        specs = svc._qq_monitors_config(_Cfg(monitors=[_entry()]))
        assert specs is not None and len(specs) == 1
        spec = specs[0]
        assert spec.monitor_id == "m1"
        assert [s.group for s in spec.sources] == ["123"]
        assert spec.interval_minutes == 60
        assert spec.window_minutes == 60  # defaults to the interval
        assert spec.sources[0].watch_user_ids == ()
        assert spec.send_when_empty is False

    def test_daily_entry_parses_with_day_window_default(self) -> None:
        specs = svc._qq_monitors_config(
            _Cfg(
                monitors=[
                    _entry(
                        schedule_type="daily",
                        daily_time="09:30",
                        interval_minutes=None,
                        target_type="user",
                        sources=[
                            {
                                "group": "123",
                                "watch_user_ids": [11111, " 22222 ", ""],
                            }
                        ],
                    )
                ]
            )
        )
        assert specs is not None
        spec = specs[0]
        assert (spec.daily_hour, spec.daily_minute) == (9, 30)
        assert spec.window_minutes == 1440
        assert spec.target_type == "user"
        assert spec.sources[0].watch_user_ids == ("11111", "22222")

    def test_legacy_flat_shape_is_lifted(self) -> None:
        """#190 configs (source_group + top-level watch_user_ids) keep
        working — lifted into a single-source task."""
        entry = {
            "id": "old",
            "source_group": "777",
            "watch_user_ids": ["1", "2"],
            "schedule_type": "interval",
            "interval_minutes": 30,
            "target_type": "user",
            "target_id": "9",
        }
        specs = svc._qq_monitors_config(_Cfg(monitors=[entry]))
        assert specs is not None
        source = specs[0].sources[0]
        assert source.group == "777"
        assert source.watch_user_ids == ("1", "2")
        assert source.focus_user_ids == ()

    def test_multi_source_task_with_focus(self) -> None:
        specs = svc._qq_monitors_config(
            _Cfg(
                monitors=[
                    _entry(
                        sources=[
                            {"group": "123", "focus_user_ids": ["7"]},
                            {
                                "group": "456",
                                "watch_user_ids": ["1"],
                                "focus_user_ids": ["2"],
                            },
                            {"group": "123"},  # duplicate group ignored
                            {"group": "not-a-number"},  # junk ignored
                        ]
                    )
                ]
            )
        )
        assert specs is not None
        spec = specs[0]
        assert [s.group for s in spec.sources] == ["123", "456"]
        # Focus works alongside "everyone" (no collection filter)…
        assert spec.sources[0].focus_user_ids == ("7",)
        assert spec.sources[0].collection_ids() == ()
        # …and focus members always join a narrowed collection filter.
        assert spec.sources[1].collection_ids() == ("1", "2")

    def test_invalid_entries_are_skipped(self) -> None:
        bad = [
            _entry(id="BAD ID"),
            _entry(sources=[{"group": "not-a-number"}]),
            _entry(sources=[]),
            _entry(schedule_type="hourly"),
            _entry(interval_minutes=3),
            _entry(schedule_type="daily", daily_time="25:99"),
            _entry(target_type="channel"),
            "not-a-table",
        ]
        assert svc._qq_monitors_config(_Cfg(monitors=bad)) is None
        # One good row among junk still comes through.
        specs = svc._qq_monitors_config(_Cfg(monitors=[*bad, _entry(id="ok1")]))
        assert specs is not None
        assert [s.monitor_id for s in specs] == ["ok1"]

    def test_disabled_and_duplicate_entries_are_skipped(self) -> None:
        assert (
            svc._qq_monitors_config(_Cfg(monitors=[_entry(enabled=False)])) is None
        )
        specs = svc._qq_monitors_config(
            _Cfg(monitors=[_entry(), _entry(target_id="999")])
        )
        assert specs is not None and len(specs) == 1
        assert specs[0].target_id == "456"

    def test_timezone_falls_back_to_proactive_timezone(self) -> None:
        specs = svc._qq_monitors_config(
            _Cfg(monitors=[_entry()], proactive_timezone="Asia/Shanghai")
        )
        assert specs is not None
        assert specs[0].timezone == "Asia/Shanghai"
        specs = svc._qq_monitors_config(
            _Cfg(
                monitors=[_entry(timezone="UTC")],
                proactive_timezone="Asia/Shanghai",
            )
        )
        assert specs is not None
        assert specs[0].timezone == "UTC"

    def test_monitor_groups_projection(self) -> None:
        specs = svc._qq_monitors_config(
            _Cfg(
                monitors=[
                    _entry(),
                    _entry(
                        id="m2",
                        sources=[{"group": "777"}, {"group": "888"}],
                    ),
                ]
            )
        )
        assert svc._qq_monitor_groups(specs) == frozenset({"123", "777", "888"})
        assert svc._qq_monitor_groups(None) == frozenset()


def _spec(**overrides: object) -> svc._QqMonitorSpec:
    specs = svc._qq_monitors_config(_Cfg(monitors=[_entry(**overrides)]))
    assert specs is not None
    return specs[0]


class TestMonitorDue:
    def test_interval_due_after_gap(self) -> None:
        spec = _spec()
        now = int(datetime(2026, 7, 27, 9, 5, tzinfo=UTC).timestamp() * 1000)
        assert svc._qq_monitor_due_fire_ms(spec, now - 61 * 60_000, now) == now
        assert svc._qq_monitor_due_fire_ms(spec, now - 10 * 60_000, now) is None

    def test_daily_fires_once_per_slot(self) -> None:
        spec = _spec(
            schedule_type="daily",
            daily_time="09:00",
            interval_minutes=None,
            timezone="UTC",
        )
        slot = int(datetime(2026, 7, 27, 9, 0, tzinfo=UTC).timestamp() * 1000)
        now = slot + 5 * 60_000  # 09:05
        yesterday_fire = slot - 24 * 3600_000
        assert svc._qq_monitor_due_fire_ms(spec, yesterday_fire, now) == slot
        # Already fired this slot → nothing due.
        assert svc._qq_monitor_due_fire_ms(spec, slot, now) is None
        # Before the slot → yesterday's slot is stale (outside grace).
        before = slot - 30 * 60_000
        assert svc._qq_monitor_due_fire_ms(spec, yesterday_fire, before) is None

    def test_daily_slot_outside_grace_is_dropped(self) -> None:
        spec = _spec(
            schedule_type="daily",
            daily_time="09:00",
            interval_minutes=None,
            timezone="UTC",
        )
        slot = int(datetime(2026, 7, 27, 9, 0, tzinfo=UTC).timestamp() * 1000)
        late = slot + int(svc._QQ_MONITOR_GRACE_SECS * 1000) + 60_000
        assert svc._qq_monitor_due_fire_ms(spec, slot - 24 * 3600_000, late) is None


class _FakeHistory:
    def __init__(self, messages: list[SimpleNamespace] | None = None) -> None:
        self.messages = messages or []
        self.last_fire: dict[str, int] = {}
        self.list_calls: list[dict[str, object]] = []

    async def list_window(self, **kwargs: object) -> list[SimpleNamespace]:
        self.list_calls.append(kwargs)
        return list(self.messages)

    async def get_last_fire(self, key: str) -> int | None:
        return self.last_fire.get(key)

    async def set_last_fire(self, key: str, ts_ms: int) -> None:
        self.last_fire[key] = ts_ms

    async def prune(self, *, older_than_ms: int) -> int:
        return 0


class _FakeAdapter:
    def __init__(self) -> None:
        self.actions: list[object] = []

    async def send_action(self, action: object) -> None:
        self.actions.append(action)


class _FakeChat:
    def __init__(self, text: str = "汇总内容。", *, fail: bool = False) -> None:
        self.text = text
        self.fail = fail
        self.requests: list[object] = []

    def run(self, request: object, cancel: object) -> object:
        self.requests.append(request)

        async def _stream():
            if self.fail:
                yield SimpleNamespace(kind="error", error="provider exploded")
                return
            yield SimpleNamespace(kind="token_delta", text=self.text)
            yield SimpleNamespace(kind="done")

        return _stream()


def _msg(sender_id: str, name: str, text: str, ts_ms: int) -> SimpleNamespace:
    return SimpleNamespace(
        sender_user_id=sender_id,
        sender_name=name,
        received_at_ms=ts_ms,
        text=text,
    )


def _params(chat: _FakeChat | None, history: _FakeHistory) -> svc.QqChannelParams:
    return svc.QqChannelParams(
        config={},
        instance_id="inst",
        chat_service=chat,
        group_history=history,
    )


class TestMonitorRunOnce:
    @pytest.mark.asyncio
    async def test_digest_generated_and_sent_to_group(self) -> None:
        history = _FakeHistory([_msg("11", "小明", "早饭吃了吗", 1_000)])
        chat = _FakeChat("大家聊了早饭。")
        adapter = _FakeAdapter()
        spec = _spec()
        await svc._qq_monitor_run_once(
            adapter,
            _params(chat, history),
            spec,
            history,
            asyncio.Event(),
            until_ms=10_000,
            reason="schedule",
        )
        assert len(chat.requests) == 1
        prompt = chat.requests[0].messages[0].content
        assert "小明(11)" in prompt and "早饭吃了吗" in prompt
        assert svc._QQ_MONITOR_STYLE_PROMPT.splitlines()[0].split("。")[0] in prompt
        assert len(adapter.actions) == 1
        action = adapter.actions[0]
        assert isinstance(action, SendGroupMsg)
        assert action.group_id == 456
        # Window bounds flow into the store query.
        call = history.list_calls[0]
        assert call["until_ms"] == 10_000
        assert call["since_ms"] == 10_000 - spec.window_minutes * 60_000
        status = svc.qq_monitor_status_snapshot("inst")["m1"]
        assert status["last_ok"] is True
        assert status["last_count"] == 1
        assert status["last_delivered"] is True

    @pytest.mark.asyncio
    async def test_user_target_sends_private_message(self) -> None:
        history = _FakeHistory([_msg("11", "小明", "hi", 1_000)])
        adapter = _FakeAdapter()
        await svc._qq_monitor_run_once(
            adapter,
            _params(_FakeChat(), history),
            _spec(target_type="user", target_id="789"),
            history,
            asyncio.Event(),
            until_ms=10_000,
            reason="manual",
        )
        assert isinstance(adapter.actions[0], SendPrivateMsg)
        assert adapter.actions[0].user_id == 789

    @pytest.mark.asyncio
    async def test_watch_filter_passed_to_store(self) -> None:
        history = _FakeHistory()
        await svc._qq_monitor_run_once(
            _FakeAdapter(),
            _params(_FakeChat(), history),
            _spec(
                sources=[
                    {
                        "group": "123",
                        "watch_user_ids": ["11", "22"],
                        "focus_user_ids": ["33"],
                    }
                ]
            ),
            history,
            asyncio.Event(),
            until_ms=10_000,
            reason="schedule",
        )
        # Focus members always join a narrowed collection filter.
        assert history.list_calls[0]["sender_ids"] == ("11", "22", "33")

    @pytest.mark.asyncio
    async def test_multi_source_digest_sections_and_focus(self) -> None:
        """A merged task queries every source and builds one combined
        prompt: per-group sections, ★ markers on focus members, and the
        dedicated focus instruction."""

        class _PerGroupHistory(_FakeHistory):
            async def list_window(self, **kwargs: object) -> list[SimpleNamespace]:
                self.list_calls.append(kwargs)
                if kwargs["group_id"] == "123":
                    return [_msg("7", "小红", "去爬山吗", 1_000)]
                return [_msg("1", "小明", "代码写完了", 2_000)]

        history = _PerGroupHistory()
        chat = _FakeChat("两群汇总。")
        adapter = _FakeAdapter()
        await svc._qq_monitor_run_once(
            adapter,
            _params(chat, history),
            _spec(
                sources=[
                    {"group": "123", "focus_user_ids": ["7"]},
                    {"group": "456"},
                ]
            ),
            history,
            asyncio.Event(),
            until_ms=10_000,
            reason="schedule",
        )
        assert [c["group_id"] for c in history.list_calls] == ["123", "456"]
        prompt = chat.requests[0].messages[0].content
        assert "### 群 123（1 条）" in prompt and "### 群 456（1 条）" in prompt
        assert "重点关注：7" in prompt
        assert "★[" in prompt  # focus member's line is marked
        assert svc._QQ_MONITOR_FOCUS_PROMPT in prompt
        assert "按群分开小节" in prompt
        assert len(adapter.actions) == 1
        status = svc.qq_monitor_status_snapshot("inst")["m1"]
        assert status["last_count"] == 2

    @pytest.mark.asyncio
    async def test_empty_window_skips_quietly_by_default(self) -> None:
        history = _FakeHistory()
        chat = _FakeChat()
        adapter = _FakeAdapter()
        await svc._qq_monitor_run_once(
            adapter,
            _params(chat, history),
            _spec(),
            history,
            asyncio.Event(),
            until_ms=10_000,
            reason="schedule",
        )
        assert adapter.actions == []
        assert chat.requests == []
        status = svc.qq_monitor_status_snapshot("inst")["m1"]
        assert status["last_ok"] is True
        assert status["last_delivered"] is False

    @pytest.mark.asyncio
    async def test_empty_window_sends_notice_when_opted_in(self) -> None:
        history = _FakeHistory()
        chat = _FakeChat()
        adapter = _FakeAdapter()
        await svc._qq_monitor_run_once(
            adapter,
            _params(chat, history),
            _spec(send_when_empty=True),
            history,
            asyncio.Event(),
            until_ms=10_000,
            reason="schedule",
        )
        assert chat.requests == []  # no LLM turn for the empty notice
        assert len(adapter.actions) == 1
        assert "没有新消息" in adapter.actions[0].message[0].text

    @pytest.mark.asyncio
    async def test_emergency_mute_blocks_group_digest_not_private(self) -> None:
        """``group_replies_enabled=false`` silences ALL group speech —
        digests to a group included; private-chat digests still deliver."""
        history = _FakeHistory([_msg("11", "小明", "hi", 1_000)])
        chat = _FakeChat()
        adapter = _FakeAdapter()
        muted = svc.QqChannelParams(
            config={"group_replies_enabled": False},
            instance_id="inst",
            chat_service=chat,
            group_history=history,
        )
        await svc._qq_monitor_run_once(
            adapter,
            muted,
            _spec(),
            history,
            asyncio.Event(),
            until_ms=10_000,
            reason="schedule",
        )
        assert adapter.actions == []
        assert chat.requests == []  # mute short-circuits before the LLM turn
        assert svc.qq_monitor_status_snapshot("inst")["m1"]["last_ok"] is False
        await svc._qq_monitor_run_once(
            adapter,
            muted,
            _spec(target_type="user", target_id="789"),
            history,
            asyncio.Event(),
            until_ms=10_000,
            reason="schedule",
        )
        assert len(adapter.actions) == 1
        assert isinstance(adapter.actions[0], SendPrivateMsg)

    @pytest.mark.asyncio
    async def test_chat_failure_lands_in_status_not_exception(self) -> None:
        history = _FakeHistory([_msg("11", "小明", "hi", 1_000)])
        adapter = _FakeAdapter()
        await svc._qq_monitor_run_once(
            adapter,
            _params(_FakeChat(fail=True), history),
            _spec(),
            history,
            asyncio.Event(),
            until_ms=10_000,
            reason="schedule",
        )
        assert adapter.actions == []
        status = svc.qq_monitor_status_snapshot("inst")["m1"]
        assert status["last_ok"] is False
        assert "provider exploded" in status["last_error"]


class TestMonitorTrigger:
    def test_trigger_without_loop_returns_false(self) -> None:
        assert svc.qq_monitor_trigger("inst", "m1") is False

    def test_trigger_with_loop_wakes_and_queues(self) -> None:
        wake = asyncio.Event()
        svc._QQ_MONITOR_WAKE["inst"] = wake
        assert svc.qq_monitor_trigger("inst", "m1") is True
        assert wake.is_set()
        assert svc._QQ_MONITOR_PENDING_TRIGGERS["inst"] == {"m1"}


class TestDispatchCapture:
    @pytest.mark.asyncio
    async def test_group_message_captured_before_whitelist(self) -> None:
        """A message from a monitored group lands in the history store even
        when the router whitelist filters the group entirely."""

        recorded: list[dict[str, object]] = []

        class _CaptureStore:
            async def record(self, **kwargs: object) -> int:
                recorded.append(kwargs)
                return 1

        payload = MessageEvent(
            self_id=999,
            message_type=MessageType.GROUP,
            user_id=11111,
            message_id=42,
            message=[TextSegment(text="监控我这条")],
            time=1_753_600_000,
            group_id=123,
            raw_message="监控我这条",
            sender=Sender(user_id=11111, nickname="小明", card="老明"),
        )

        class _Adapter:
            async def inbound(self):
                yield SimpleNamespace(payload=payload, attachments=[])

            def inbound_iter(self):  # pragma: no cover - compat shim
                return self.inbound()

        class _StubInbox:
            async def list_pending(self, **kwargs: object) -> list[object]:
                return []

        # Whitelist excludes group 123 → the router filters the message…
        router = ChannelRouter(
            group_keywords={},
            self_ids=[999],
            group_whitelist=frozenset({"999999"}),
        )
        params = svc.QqChannelParams(
            config={},
            instance_id="inst",
            group_history=_CaptureStore(),
            inbox=_StubInbox(),
        )
        adapter = _Adapter()
        await svc._qq_dispatch_loop(
            adapter,  # type: ignore[arg-type]
            router,
            params,
            asyncio.Event(),
            monitored_groups=frozenset({"123"}),
        )
        # …but the capture already happened.
        assert len(recorded) == 1
        row = recorded[0]
        assert row["instance_id"] == "inst"
        assert row["group_id"] == "123"
        assert row["sender_user_id"] == "11111"
        assert row["sender_name"] == "老明"  # card wins over nickname
        assert row["message_id"] == "42"
        assert row["text"] == "监控我这条"
        assert row["event_time_ms"] == 1_753_600_000 * 1000

    @pytest.mark.asyncio
    async def test_unmonitored_group_is_not_captured(self) -> None:
        recorded: list[dict[str, object]] = []

        class _CaptureStore:
            async def record(self, **kwargs: object) -> int:
                recorded.append(kwargs)
                return 1

        payload = MessageEvent(
            self_id=999,
            message_type=MessageType.GROUP,
            user_id=1,
            message_id=1,
            message=[TextSegment(text="别的群")],
            time=0,
            group_id=777,
            raw_message="别的群",
        )

        class _Adapter:
            async def inbound(self):
                yield SimpleNamespace(payload=payload, attachments=[])

        class _StubInbox:
            async def list_pending(self, **kwargs: object) -> list[object]:
                return []

        params = svc.QqChannelParams(
            config={},
            instance_id="inst",
            group_history=_CaptureStore(),
            inbox=_StubInbox(),
        )
        await svc._qq_dispatch_loop(
            _Adapter(),  # type: ignore[arg-type]
            ChannelRouter(group_keywords={}, self_ids=[999]),
            params,
            asyncio.Event(),
            monitored_groups=frozenset({"123"}),
        )
        assert recorded == []


# ---------------------------------------------------------------------------
# Map-reduce digests — big windows split into 1000-message chunks that
# are summarised by parallel turns, then merged by one reduce turn.
# ---------------------------------------------------------------------------


class _MapChat:
    """Chat stub with per-marker replies/failures + a concurrency probe."""

    def __init__(
        self,
        reply_for: dict[str, str] | None = None,
        default: str = "段摘要。",
        fail_markers: dict[str, int] | None = None,
    ) -> None:
        self.requests: list[object] = []
        self.default = default
        self.reply_for = dict(reply_for or {})
        self.fail_remaining = dict(fail_markers or {})
        self.in_flight = 0
        self.max_in_flight = 0

    def run(self, request: object, cancel: object) -> object:
        self.requests.append(request)
        prompt = request.messages[0].content  # type: ignore[attr-defined]
        fail = False
        for marker, left in list(self.fail_remaining.items()):
            if marker in prompt and left > 0:
                self.fail_remaining[marker] = left - 1
                fail = True
                break
        reply = self.default
        for marker, text in self.reply_for.items():
            if marker in prompt:
                reply = text
                break

        async def _stream():
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            await asyncio.sleep(0.005)  # let sibling map turns overlap
            self.in_flight -= 1
            if fail:
                yield SimpleNamespace(kind="error", error="boom")
                return
            yield SimpleNamespace(kind="token_delta", text=reply)
            yield SimpleNamespace(kind="done")

        return _stream()


def _many_msgs(n: int) -> list[SimpleNamespace]:
    return [_msg(str(i % 7), f"u{i % 7}", f"msg-{i}", 1_000 + i) for i in range(n)]


class TestMonitorMapReduce:
    @pytest.mark.asyncio
    async def test_chunk_boundary_stays_single_turn(self) -> None:
        history = _FakeHistory(_many_msgs(svc._QQ_MONITOR_CHUNK_MESSAGES))
        chat = _MapChat(default="单轮汇总。")
        adapter = _FakeAdapter()
        await svc._qq_monitor_run_once(
            adapter,
            _params(chat, history),
            _spec(),
            history,
            asyncio.Event(),
            until_ms=10_000,
            reason="schedule",
        )
        assert len(chat.requests) == 1  # exactly at the boundary → no fan-out
        assert "msg-0" in chat.requests[0].messages[0].content

    @pytest.mark.asyncio
    async def test_big_window_fans_out_and_reduces(self) -> None:
        history = _FakeHistory(_many_msgs(2_500))
        chat = _MapChat(
            reply_for={"分段摘要": "最终汇总文本。"},
            default="段摘要。",
        )
        adapter = _FakeAdapter()
        await svc._qq_monitor_run_once(
            adapter,
            _params(chat, history),
            _spec(),
            history,
            asyncio.Event(),
            until_ms=10_000,
            reason="schedule",
        )
        # 2500 → chunks of 1000/1000/500 → 3 map turns + 1 reduce.
        assert len(chat.requests) == 4
        map_prompts = [r.messages[0].content for r in chat.requests[:3]]
        assert "第 1/3 段" in map_prompts[0] and "本段 1000 条" in map_prompts[0]
        assert "第 2/3 段" in map_prompts[1]
        assert "第 3/3 段" in map_prompts[2] and "本段 500 条" in map_prompts[2]
        assert all("中间摘要" in p for p in map_prompts)
        reduce_prompt = chat.requests[3].messages[0].content
        assert "共 2500 条" in reduce_prompt
        assert "段摘要。" in reduce_prompt  # map outputs fed to reduce
        assert "msg-0" not in reduce_prompt  # raw chat never reaches reduce
        # Fresh session per turn → parallel maps can't supplement-absorb.
        assert len({r.session_key for r in chat.requests}) == 4
        assert chat.max_in_flight >= 2  # maps actually ran concurrently
        assert len(adapter.actions) == 1
        assert "最终汇总文本。" in adapter.actions[0].message[0].text
        status = svc.qq_monitor_status_snapshot("inst")["m1"]
        assert status["last_ok"] is True
        assert status["last_count"] == 2_500

    @pytest.mark.asyncio
    async def test_failed_chunk_retries_then_notes_incompleteness(self) -> None:
        history = _FakeHistory(_many_msgs(1_500))
        # Chunk 1 fails once then succeeds on retry; chunk 2 always fails.
        chat = _MapChat(
            reply_for={"分段摘要": "最终汇总文本。"},
            fail_markers={"第 1/2 段": 1, "第 2/2 段": 99},
        )
        adapter = _FakeAdapter()
        await svc._qq_monitor_run_once(
            adapter,
            _params(chat, history),
            _spec(),
            history,
            asyncio.Event(),
            until_ms=10_000,
            reason="schedule",
        )
        sent = adapter.actions[0].message[0].text
        assert "最终汇总文本。" in sent
        assert "1 段记录处理失败" in sent  # deterministic honesty note
        status = svc.qq_monitor_status_snapshot("inst")["m1"]
        assert status["last_ok"] is True and status["last_delivered"] is True

    @pytest.mark.asyncio
    async def test_all_chunks_failed_fails_the_run(self) -> None:
        history = _FakeHistory(_many_msgs(1_500))
        chat = _MapChat(fail_markers={"": 999})  # every turn errors
        adapter = _FakeAdapter()
        await svc._qq_monitor_run_once(
            adapter,
            _params(chat, history),
            _spec(),
            history,
            asyncio.Event(),
            until_ms=10_000,
            reason="schedule",
        )
        assert adapter.actions == []  # nothing delivered
        # 2 chunks × 2 attempts each, reduce never runs.
        assert len(chat.requests) == 4
        status = svc.qq_monitor_status_snapshot("inst")["m1"]
        assert status["last_ok"] is False
        assert "map chunks failed" in status["last_error"]


class TestMonitorSendFolding:
    """Long digests compress into a merged-forward card; short ones
    stay plain messages."""

    @pytest.mark.asyncio
    async def test_short_digest_sends_plain_group_message(self) -> None:
        adapter = _FakeAdapter()
        await svc._qq_monitor_send(adapter, _spec(), "短摘要。")
        assert len(adapter.actions) == 1
        assert isinstance(adapter.actions[0], SendGroupMsg)

    @pytest.mark.asyncio
    async def test_long_digest_folds_into_group_forward_card(self) -> None:
        from corlinman_channels.onebot import SendGroupForwardMsg

        adapter = _FakeAdapter()
        long_text = "长" * (svc._QQ_FORWARD_TEXT_THRESHOLD + 1)
        await svc._qq_monitor_send(adapter, _spec(), long_text)
        assert len(adapter.actions) == 1
        card = adapter.actions[0]
        assert isinstance(card, SendGroupForwardMsg)
        assert card.group_id == 456
        card_text = "".join(
            seg.text
            for node in card.messages
            for seg in node.content
            if isinstance(seg, TextSegment)
        )
        assert card_text == long_text

    @pytest.mark.asyncio
    async def test_long_digest_folds_for_user_target(self) -> None:
        from corlinman_channels.onebot import SendPrivateForwardMsg

        adapter = _FakeAdapter()
        long_text = "长" * (svc._QQ_FORWARD_TEXT_THRESHOLD + 1)
        await svc._qq_monitor_send(
            adapter, _spec(target_type="user", target_id="789"), long_text
        )
        assert len(adapter.actions) == 1
        card = adapter.actions[0]
        assert isinstance(card, SendPrivateForwardMsg)
        assert card.user_id == 789
