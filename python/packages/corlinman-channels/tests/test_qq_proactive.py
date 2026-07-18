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
