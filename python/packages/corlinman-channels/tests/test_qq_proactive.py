"""``_qq_proactive_*`` — human-paced proactive group speech helpers."""

from __future__ import annotations

import asyncio
import random
from types import SimpleNamespace

import pytest
from corlinman_channels import service as svc


class _Cfg(SimpleNamespace):
    """Attribute-style config stub matching ``_attr`` reads."""


class TestProactiveConfig:
    def test_disabled_by_default(self) -> None:
        assert svc._qq_proactive_config(_Cfg(), frozenset({"1"})) is None

    def test_enabled_with_explicit_groups(self) -> None:
        cfg = svc._qq_proactive_config(
            _Cfg(proactive_enabled=True, proactive_groups=[123, "456"]),
            None,
        )
        assert cfg is not None
        assert cfg.groups == ("123", "456")
        assert cfg.min_gap_minutes == 45
        assert cfg.max_gap_minutes == 45 * 4
        assert cfg.daily_max == 4
        assert cfg.prompt == svc._QQ_PROACTIVE_DEFAULT_PROMPT

    def test_groups_fall_back_to_whitelist(self) -> None:
        cfg = svc._qq_proactive_config(
            _Cfg(proactive_enabled=True), frozenset({"777", "888"})
        )
        assert cfg is not None
        assert cfg.groups == ("777", "888")

    def test_enabled_without_any_target_stays_off(self) -> None:
        assert svc._qq_proactive_config(_Cfg(proactive_enabled=True), None) is None
        assert (
            svc._qq_proactive_config(_Cfg(proactive_enabled=True), frozenset())
            is None
        )

    def test_custom_pacing_and_prompt(self) -> None:
        cfg = svc._qq_proactive_config(
            _Cfg(
                proactive_enabled=True,
                proactive_groups=[1],
                proactive_min_gap_minutes=10,
                proactive_max_gap_minutes=30,
                proactive_daily_max=2,
                proactive_active_start_hour=8,
                proactive_active_end_hour=22,
                proactive_prompt="说点什么",
            ),
            None,
        )
        assert cfg is not None
        assert (cfg.min_gap_minutes, cfg.max_gap_minutes) == (10, 30)
        assert cfg.daily_max == 2
        assert (cfg.active_start_hour, cfg.active_end_hour) == (8, 22)
        assert cfg.prompt == "说点什么"


class TestActiveHours:
    def test_normal_window(self) -> None:
        assert svc._qq_proactive_in_active_hours(9, 9, 23)
        assert svc._qq_proactive_in_active_hours(22, 9, 23)
        assert not svc._qq_proactive_in_active_hours(23, 9, 23)
        assert not svc._qq_proactive_in_active_hours(3, 9, 23)

    def test_overnight_window_wraps(self) -> None:
        assert svc._qq_proactive_in_active_hours(23, 22, 2)
        assert svc._qq_proactive_in_active_hours(1, 22, 2)
        assert not svc._qq_proactive_in_active_hours(12, 22, 2)

    def test_degenerate_window_is_always_on(self) -> None:
        assert svc._qq_proactive_in_active_hours(5, 9, 9)


class TestDelayDraw:
    def test_delay_within_configured_window(self) -> None:
        cfg = svc._qq_proactive_config(
            _Cfg(
                proactive_enabled=True,
                proactive_groups=[1],
                proactive_min_gap_minutes=10,
                proactive_max_gap_minutes=20,
            ),
            None,
        )
        assert cfg is not None
        rng = random.Random(42)
        for _ in range(50):
            d = svc._qq_proactive_next_delay_secs(cfg, rng)
            assert 10 * 60 <= d <= 20 * 60


class TestProactiveSleep:
    @pytest.mark.asyncio
    async def test_cancel_interrupts_sleep(self) -> None:
        cancel = asyncio.Event()
        loop = asyncio.get_running_loop()
        loop.call_later(0.05, cancel.set)
        assert await svc._qq_proactive_sleep(cancel, 30.0) is True

    @pytest.mark.asyncio
    async def test_timeout_returns_false(self) -> None:
        cancel = asyncio.Event()
        assert await svc._qq_proactive_sleep(cancel, 0.0) is False


class TestProactiveGenerate:
    @pytest.mark.asyncio
    async def test_generate_runs_persona_turn_and_collects_text(self) -> None:
        seen: dict[str, object] = {}

        class _FakeChat:
            def run(self, request, cancel):  # noqa: ANN001
                seen["request"] = request

                async def _stream():
                    yield SimpleNamespace(kind="token_delta", text="早上好，")
                    yield SimpleNamespace(kind="token_delta", text="今天有点忙。")
                    yield SimpleNamespace(kind="done")

                return _stream()

        params = svc.QqChannelParams(
            config=_Cfg(), model="m1", chat_service=_FakeChat()
        )
        text = await svc._qq_proactive_generate(
            params, "9999", "说点什么", asyncio.Event()
        )
        assert text == "早上好，今天有点忙。"
        req = seen["request"]
        assert req.model == "m1"
        assert req.binding.thread == "9999"
        # Dedicated proactive session — never collides with a user chat
        # (the key is a hash of the binding; the binding carries the
        # distinct sender slot).
        assert req.binding.sender == "proactive"
        assert req.session_key

    @pytest.mark.asyncio
    async def test_generate_raises_on_chat_error(self) -> None:
        class _FakeChat:
            def run(self, request, cancel):  # noqa: ANN001
                async def _stream():
                    yield SimpleNamespace(kind="error", error="boom")

                return _stream()

        params = svc.QqChannelParams(
            config=_Cfg(), model="m1", chat_service=_FakeChat()
        )
        with pytest.raises(RuntimeError, match="boom"):
            await svc._qq_proactive_generate(
                params, "9999", "说点什么", asyncio.Event()
            )


@pytest.fixture(autouse=True)
def _reset_speech_state() -> None:
    """Module-level speech state must not leak across tests."""
    svc._QQ_GROUP_SPEECH._events.clear()
    svc._QQ_GROUP_RECENT.clear()
    svc._QQ_PROACTIVE_SENT.clear()
    svc._QQ_PROACTIVE_LAST_MONO.clear()


class TestProactiveConfigHumanization:
    def test_explicit_groups_intersect_whitelist(self) -> None:
        cfg = svc._qq_proactive_config(
            _Cfg(proactive_enabled=True, proactive_groups=[1, 2, 3]),
            frozenset({"2", "3"}),
        )
        assert cfg is not None
        assert cfg.groups == ("2", "3")

    def test_explicit_groups_all_outside_whitelist_stays_off(self) -> None:
        # Whitelist wins even over an explicit proactive_groups — falls back
        # to the whitelist itself rather than speaking outside it.
        cfg = svc._qq_proactive_config(
            _Cfg(proactive_enabled=True, proactive_groups=[1]),
            frozenset({"9"}),
        )
        assert cfg is not None
        assert cfg.groups == ("9",)

    def test_probability_parsing_and_clamping(self) -> None:
        base = {"proactive_enabled": True, "proactive_groups": [1]}
        assert svc._qq_proactive_config(_Cfg(**base), None).probability == 1.0
        cfg = svc._qq_proactive_config(
            _Cfg(**base, proactive_probability=0.4), None
        )
        assert cfg.probability == 0.4
        assert (
            svc._qq_proactive_config(
                _Cfg(**base, proactive_probability=0), None
            ).probability
            == 0.0
        )
        assert (
            svc._qq_proactive_config(
                _Cfg(**base, proactive_probability=7), None
            ).probability
            == 1.0
        )
        assert (
            svc._qq_proactive_config(
                _Cfg(**base, proactive_probability="junk"), None
            ).probability
            == 1.0
        )

    def test_timezone_and_context_messages(self) -> None:
        base = {"proactive_enabled": True, "proactive_groups": [1]}
        cfg = svc._qq_proactive_config(
            _Cfg(**base, proactive_timezone="Asia/Shanghai", proactive_context_messages=5),
            None,
        )
        assert cfg.timezone == "Asia/Shanghai"
        assert cfg.context_messages == 5
        cfg = svc._qq_proactive_config(
            _Cfg(**base, proactive_context_messages=0), None
        )
        assert cfg.context_messages == 0  # explicit off is honoured
        # Default = the full recent buffer so the persona sees the whole room.
        assert (
            svc._qq_proactive_config(_Cfg(**base), None).context_messages
            == svc._QQ_GROUP_RECENT_MAX
        )


class TestProactiveNowParts:
    def test_timezone_is_applied(self) -> None:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        day, hour = svc._qq_proactive_now_parts("Asia/Shanghai")
        expected = datetime.now(ZoneInfo("Asia/Shanghai"))
        assert day == expected.strftime("%Y-%m-%d")
        assert hour == expected.hour

    def test_invalid_timezone_falls_back_to_local(self) -> None:
        import time as _time

        day, hour = svc._qq_proactive_now_parts("Not/AZone")
        assert day == _time.strftime("%Y-%m-%d")
        assert hour == int(_time.strftime("%H"))


class TestSkipDetection:
    @pytest.mark.parametrize(
        "text", ["SKIP", "skip", " Skip ", "[SKIP]", "SKIP。", "[skip]."]
    )
    def test_skip_variants(self, text: str) -> None:
        assert svc._qq_proactive_is_skip(text)

    @pytest.mark.parametrize("text", ["skip今天不聊", "我先skip一下", "好的"])
    def test_non_skip_text(self, text: str) -> None:
        assert not svc._qq_proactive_is_skip(text)


class TestContextAndPrompt:
    def test_record_and_render_context_lines(self) -> None:
        svc._qq_record_group_message("default", "42", "张三", "今晚打球吗")
        svc._qq_record_group_message("default", "42", "李四", "打！")
        svc._qq_record_group_message("default", "42", "", "")  # ignored
        cfg = svc._qq_proactive_config(
            _Cfg(proactive_enabled=True, proactive_groups=[42]), None
        )
        lines = svc._qq_proactive_context_lines("default", "42", cfg)
        assert len(lines) == 2
        assert "张三: 今晚打球吗" in lines[0]
        assert "李四: 打！" in lines[1]

    def test_context_respects_limit_and_off_switch(self) -> None:
        for i in range(20):
            svc._qq_record_group_message("default", "42", "u", f"msg{i}")
        cfg = svc._qq_proactive_config(
            _Cfg(
                proactive_enabled=True,
                proactive_groups=[42],
                proactive_context_messages=3,
            ),
            None,
        )
        lines = svc._qq_proactive_context_lines("default", "42", cfg)
        assert [ln.split(": ")[-1] for ln in lines] == ["msg17", "msg18", "msg19"]
        off = svc._qq_proactive_config(
            _Cfg(
                proactive_enabled=True,
                proactive_groups=[42],
                proactive_context_messages=0,
            ),
            None,
        )
        assert svc._qq_proactive_context_lines("default", "42", off) == []

    def test_compose_prompt_includes_context_and_skip_hint(self) -> None:
        cfg = svc._qq_proactive_config(
            _Cfg(proactive_enabled=True, proactive_groups=[1], proactive_prompt="冒个泡"),
            None,
        )
        with_ctx = svc._qq_proactive_compose_prompt(cfg, ["[10:00] a: hi"])
        assert "[10:00] a: hi" in with_ctx
        assert "冒个泡" in with_ctx
        assert "SKIP" in with_ctx
        bare = svc._qq_proactive_compose_prompt(cfg, [])
        assert "聊天记录" not in bare
        assert "SKIP" in bare

    def test_self_posts_render_with_marker(self) -> None:
        svc._qq_record_group_message("default", "42", "张三", "机器人能修 bug 吗")
        svc._qq_record_group_message("default", "42", "", "能，发过来看看", is_self=True)
        cfg = svc._qq_proactive_config(
            _Cfg(proactive_enabled=True, proactive_groups=[42]), None
        )
        lines = svc._qq_proactive_context_lines("default", "42", cfg)
        assert len(lines) == 2
        assert "张三: 机器人能修 bug 吗" in lines[0]
        assert "你自己: 能，发过来看看" in lines[1]

    def test_last_message_is_self_detection(self) -> None:
        assert not svc._qq_last_group_message_is_self("default", "42")  # empty buffer
        svc._qq_record_group_message("default", "42", "张三", "在吗")
        assert not svc._qq_last_group_message_is_self("default", "42")
        svc._qq_record_group_message("default", "42", "", "在的", is_self=True)
        assert svc._qq_last_group_message_is_self("default", "42")
        svc._qq_record_group_message("default", "42", "李四", "聊聊")
        assert not svc._qq_last_group_message_is_self("default", "42")

    def test_compose_prompt_renders_rag_snippets(self) -> None:
        cfg = svc._qq_proactive_config(
            _Cfg(proactive_enabled=True, proactive_groups=[1]), None
        )
        prompt = svc._qq_proactive_compose_prompt(
            cfg,
            ["[10:00] a: 今晚吃什么"],
            rag_snippets=["食堂周三有烤鸭", "", "  ", "x" * 1000, "四", "五(超出上限)"],
        )
        assert "食堂周三有烤鸭" in prompt
        assert "资料库" in prompt
        # Blank snippets dropped, per-snippet char cap applied, top-k capped.
        assert "x" * (svc._QQ_PROACTIVE_RAG_SNIPPET_CHARS + 1) not in prompt
        assert "五(超出上限)" not in prompt
        # No snippets → no RAG section at all.
        assert "资料库" not in svc._qq_proactive_compose_prompt(cfg, [])

    def test_rag_query_uses_human_chatter_only(self) -> None:
        assert svc._qq_proactive_rag_query("default", "42") == ""
        svc._qq_record_group_message("default", "42", "张三", "GPU 报错了")
        svc._qq_record_group_message("default", "42", "", "我看看日志", is_self=True)
        q = svc._qq_proactive_rag_query("default", "42")
        assert "GPU 报错了" in q
        assert "我看看日志" not in q


class TestSpeechWindowCfg:
    def test_defaults_off(self) -> None:
        assert svc._qq_speech_window_cfg(_Cfg()) == (0.0, 0)

    def test_reads_minutes_and_count(self) -> None:
        cfg = _Cfg(
            group_rate_limit_window_minutes=10,
            group_rate_limit_max_messages=5,
        )
        assert svc._qq_speech_window_cfg(cfg) == (600.0, 5)

    def test_garbage_values_disable(self) -> None:
        cfg = _Cfg(
            group_rate_limit_window_minutes="junk",
            group_rate_limit_max_messages=None,
        )
        assert svc._qq_speech_window_cfg(cfg) == (0.0, 0)


class TestDailyBudgetState:
    def test_counts_roll_over_by_day(self) -> None:
        key = "default:42"
        assert svc._qq_proactive_sent_today(key, "2026-07-26") == 0
        svc._qq_proactive_mark_sent(key, "2026-07-26")
        svc._qq_proactive_mark_sent(key, "2026-07-26")
        assert svc._qq_proactive_sent_today(key, "2026-07-26") == 2
        assert svc._qq_proactive_sent_today(key, "2026-07-27") == 0
        svc._qq_proactive_mark_sent(key, "2026-07-27")
        assert svc._qq_proactive_sent_today(key, "2026-07-27") == 1


class _FakeAdapter:
    def __init__(self) -> None:
        self.sent: list[object] = []

    async def send_action(self, action: object) -> None:
        self.sent.append(action)


class TestProactiveSend:
    @pytest.mark.asyncio
    async def test_send_normalizes_markdown_and_splits_bubbles(self) -> None:
        adapter = _FakeAdapter()
        await svc._qq_proactive_send(
            adapter, "42", "**今晚**有空[MSG_BREAK]一起打球？"
        )
        assert len(adapter.sent) == 2
        first = adapter.sent[0].message[0].text
        assert "**" not in first
        assert adapter.sent[1].message[0].text == "一起打球？"
        assert all(a.group_id == 42 for a in adapter.sent)


def _loop_params(chat, cfg_ns) -> svc.QqChannelParams:
    return svc.QqChannelParams(
        config=cfg_ns, model="m1", chat_service=chat, instance_id="default"
    )


def _one_beat_sleep(monkeypatch) -> None:
    """First sleep proceeds (one beat), second breaks the loop."""
    calls = {"n": 0}

    async def _sleep(cancel, secs):  # noqa: ANN001
        calls["n"] += 1
        return calls["n"] > 1

    monkeypatch.setattr(svc, "_qq_proactive_sleep", _sleep)


class _EchoChat:
    """Chat stub returning a fixed reply."""

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


_ONLINE = {"online": True, "account_online": True, "account_qq": 10001}


class TestProactiveLoopGates:
    @pytest.mark.asyncio
    async def test_one_beat_posts_and_records_budget(self, monkeypatch) -> None:
        _one_beat_sleep(monkeypatch)
        monkeypatch.setattr(
            svc, "_qq_proactive_now_parts", lambda tz: ("2026-07-26", 12)
        )
        adapter = _FakeAdapter()
        chat = _EchoChat("大家下午好呀")
        cfg_ns = _Cfg(proactive_enabled=True, proactive_groups=[42])
        cfg = svc._qq_proactive_config(cfg_ns, None)
        params = _loop_params(chat, cfg_ns)
        await svc._qq_proactive_loop(
            adapter, params, cfg, asyncio.Event(), health=dict(_ONLINE)
        )
        assert len(adapter.sent) == 1
        assert svc._qq_proactive_sent_today("default:42", "2026-07-26") == 1
        assert "default:42" in svc._QQ_PROACTIVE_LAST_MONO
        assert svc._QQ_GROUP_SPEECH.count("default:42", 3600.0) == 1

    @pytest.mark.asyncio
    async def test_emergency_mute_blocks_proactive(self, monkeypatch) -> None:
        _one_beat_sleep(monkeypatch)
        monkeypatch.setattr(
            svc, "_qq_proactive_now_parts", lambda tz: ("2026-07-26", 12)
        )
        adapter = _FakeAdapter()
        chat = _EchoChat("不该出现")
        cfg_ns = _Cfg(
            proactive_enabled=True,
            proactive_groups=[42],
            group_replies_enabled=False,
        )
        cfg = svc._qq_proactive_config(cfg_ns, None)
        await svc._qq_proactive_loop(
            adapter, _loop_params(chat, cfg_ns), cfg, asyncio.Event(),
            health=dict(_ONLINE),
        )
        assert adapter.sent == []
        assert chat.calls == 0

    @pytest.mark.asyncio
    async def test_probability_zero_never_posts(self, monkeypatch) -> None:
        _one_beat_sleep(monkeypatch)
        monkeypatch.setattr(
            svc, "_qq_proactive_now_parts", lambda tz: ("2026-07-26", 12)
        )
        adapter = _FakeAdapter()
        chat = _EchoChat("不该出现")
        cfg_ns = _Cfg(
            proactive_enabled=True,
            proactive_groups=[42],
            proactive_probability=0,
        )
        cfg = svc._qq_proactive_config(cfg_ns, None)
        await svc._qq_proactive_loop(
            adapter, _loop_params(chat, cfg_ns), cfg, asyncio.Event(),
            health=dict(_ONLINE),
        )
        assert adapter.sent == []
        assert chat.calls == 0

    @pytest.mark.asyncio
    async def test_model_skip_stays_silent(self, monkeypatch) -> None:
        _one_beat_sleep(monkeypatch)
        monkeypatch.setattr(
            svc, "_qq_proactive_now_parts", lambda tz: ("2026-07-26", 12)
        )
        adapter = _FakeAdapter()
        chat = _EchoChat("SKIP")
        cfg_ns = _Cfg(proactive_enabled=True, proactive_groups=[42])
        cfg = svc._qq_proactive_config(cfg_ns, None)
        await svc._qq_proactive_loop(
            adapter, _loop_params(chat, cfg_ns), cfg, asyncio.Event(),
            health=dict(_ONLINE),
        )
        assert chat.calls == 1  # the turn ran…
        assert adapter.sent == []  # …but the persona chose silence
        assert svc._qq_proactive_sent_today("default:42", "2026-07-26") == 0

    @pytest.mark.asyncio
    async def test_speech_window_blocks_eligibility(self, monkeypatch) -> None:
        _one_beat_sleep(monkeypatch)
        monkeypatch.setattr(
            svc, "_qq_proactive_now_parts", lambda tz: ("2026-07-26", 12)
        )
        adapter = _FakeAdapter()
        chat = _EchoChat("不该出现")
        cfg_ns = _Cfg(
            proactive_enabled=True,
            proactive_groups=[42],
            group_rate_limit_window_minutes=10,
            group_rate_limit_max_messages=1,
        )
        svc._QQ_GROUP_SPEECH.record("default:42")  # window already spent
        cfg = svc._qq_proactive_config(cfg_ns, None)
        await svc._qq_proactive_loop(
            adapter, _loop_params(chat, cfg_ns), cfg, asyncio.Event(),
            health=dict(_ONLINE),
        )
        assert adapter.sent == []
        assert chat.calls == 0

    @pytest.mark.asyncio
    async def test_per_group_min_gap_blocks_repeat(self, monkeypatch) -> None:
        _one_beat_sleep(monkeypatch)
        monkeypatch.setattr(
            svc, "_qq_proactive_now_parts", lambda tz: ("2026-07-26", 12)
        )
        adapter = _FakeAdapter()
        chat = _EchoChat("不该出现")
        cfg_ns = _Cfg(proactive_enabled=True, proactive_groups=[42])
        import time as _time

        svc._QQ_PROACTIVE_LAST_MONO["default:42"] = _time.monotonic()
        cfg = svc._qq_proactive_config(cfg_ns, None)
        await svc._qq_proactive_loop(
            adapter, _loop_params(chat, cfg_ns), cfg, asyncio.Event(),
            health=dict(_ONLINE),
        )
        assert adapter.sent == []

    @pytest.mark.asyncio
    async def test_last_message_is_self_blocks_group(self, monkeypatch) -> None:
        """The repeat-send fix: if the bot spoke last, stay silent."""
        _one_beat_sleep(monkeypatch)
        monkeypatch.setattr(
            svc, "_qq_proactive_now_parts", lambda tz: ("2026-07-26", 12)
        )
        adapter = _FakeAdapter()
        chat = _EchoChat("不该出现")
        cfg_ns = _Cfg(proactive_enabled=True, proactive_groups=[42])
        svc._qq_record_group_message("default", "42", "张三", "帮我修个 bug")
        svc._qq_record_group_message("default", "42", "", "能，发过来", is_self=True)
        cfg = svc._qq_proactive_config(cfg_ns, None)
        await svc._qq_proactive_loop(
            adapter, _loop_params(chat, cfg_ns), cfg, asyncio.Event(),
            health=dict(_ONLINE),
        )
        assert adapter.sent == []
        assert chat.calls == 0

    @pytest.mark.asyncio
    async def test_posted_message_recorded_as_self(self, monkeypatch) -> None:
        _one_beat_sleep(monkeypatch)
        monkeypatch.setattr(
            svc, "_qq_proactive_now_parts", lambda tz: ("2026-07-26", 12)
        )
        adapter = _FakeAdapter()
        chat = _EchoChat("大家下午好呀")
        cfg_ns = _Cfg(proactive_enabled=True, proactive_groups=[42])
        cfg = svc._qq_proactive_config(cfg_ns, None)
        await svc._qq_proactive_loop(
            adapter, _loop_params(chat, cfg_ns), cfg, asyncio.Event(),
            health=dict(_ONLINE),
        )
        assert len(adapter.sent) == 1
        buf = svc._QQ_GROUP_RECENT.get("default:42")
        assert buf is not None and len(buf) == 1
        assert buf[-1][2] == "大家下午好呀"
        assert buf[-1][3] is True
        assert svc._qq_last_group_message_is_self("default", "42")

    @pytest.mark.asyncio
    async def test_rag_snippets_reach_the_prompt(self, monkeypatch) -> None:
        _one_beat_sleep(monkeypatch)
        monkeypatch.setattr(
            svc, "_qq_proactive_now_parts", lambda tz: ("2026-07-26", 12)
        )
        adapter = _FakeAdapter()
        seen_prompts: list[str] = []

        class _CapturingChat(_EchoChat):
            def run(self, request, cancel):  # noqa: ANN001
                seen_prompts.append(request.messages[0].content)
                return super().run(request, cancel)

        chat = _CapturingChat("好嘞")
        queries: list[tuple[str, int]] = []

        async def _rag(query: str, k: int) -> list[str]:
            queries.append((query, k))
            return ["食堂周三有烤鸭"]

        svc._qq_record_group_message("default", "42", "张三", "今晚食堂吃什么")
        cfg_ns = _Cfg(proactive_enabled=True, proactive_groups=[42])
        cfg = svc._qq_proactive_config(cfg_ns, None)
        params = svc.QqChannelParams(
            config=cfg_ns,
            model="m1",
            chat_service=chat,
            instance_id="default",
            rag_search=_rag,
        )
        await svc._qq_proactive_loop(
            adapter, params, cfg, asyncio.Event(), health=dict(_ONLINE)
        )
        assert len(adapter.sent) == 1
        assert queries and "今晚食堂吃什么" in queries[0][0]
        assert "食堂周三有烤鸭" in seen_prompts[0]

    @pytest.mark.asyncio
    async def test_rag_failure_never_blocks_the_post(self, monkeypatch) -> None:
        _one_beat_sleep(monkeypatch)
        monkeypatch.setattr(
            svc, "_qq_proactive_now_parts", lambda tz: ("2026-07-26", 12)
        )
        adapter = _FakeAdapter()
        chat = _EchoChat("照常营业")

        async def _rag(query: str, k: int) -> list[str]:
            raise RuntimeError("kb offline")

        svc._qq_record_group_message("default", "42", "张三", "在吗")
        cfg_ns = _Cfg(proactive_enabled=True, proactive_groups=[42])
        cfg = svc._qq_proactive_config(cfg_ns, None)
        params = svc.QqChannelParams(
            config=cfg_ns,
            model="m1",
            chat_service=chat,
            instance_id="default",
            rag_search=_rag,
        )
        await svc._qq_proactive_loop(
            adapter, params, cfg, asyncio.Event(), health=dict(_ONLINE)
        )
        assert len(adapter.sent) == 1
