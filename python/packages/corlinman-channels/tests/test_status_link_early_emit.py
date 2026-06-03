"""Tests for EARLY surfacing of the agent-status share link.

Bug: when a turn dispatched sub-agent(s), the shareable ``/status/{token}``
link was only ever appended to the FINAL reply (end-of-turn). A user on
Telegram (and every other channel) therefore could not open the live status
view until the work had already finished — defeating the whole "watch me
work" purpose for exactly the long-running multi-agent turns where it matters
most.

Fix: the moment the first ``subagent_spawn`` / ``subagent_spawn_many`` /
``subagent_spawn_inline`` tool_call is seen mid-turn, the channel pushes the
link as its own standalone message; the end-of-turn footer then skips the
link so it is never sent twice. Turns with zero spawns are unchanged.

Layers covered here:

* :func:`service._drive_spinner` — the shared event loop for the four
  mutable-spinner channels (Telegram / Discord / Slack / Feishu). Asserts the
  ``on_subagent_spawn`` callback fires once on the first spawn, never on
  non-spawn tools, and not at all when the link feature is off.
* :func:`service._build_footer_for_outcome` — the end-of-turn de-dup: when
  ``outcome.status_link_emitted`` is set, the status line is suppressed.
* :func:`service.handle_one_qq` — end-to-end on a summary-style channel
  (no shared spinner): the early link is sent as a standalone QQ message and
  the final reply does not re-append it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_channels import service
from corlinman_channels._status import MutableSpinner
from corlinman_channels.common import ChannelBinding, InboundEvent
from corlinman_channels.onebot import MessageEvent, MessageType, TextSegment
from corlinman_channels.router import RoutedRequest

# ---------------------------------------------------------------------------
# Local fakes (mirror test_service.py; inlined so this module is import-safe
# from the repo root without test_service on sys.path).
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Ev:
    kind: str
    text: str = ""
    error: str = ""
    plugin: str = ""
    tool: str = ""
    args_json: bytes = b""
    is_reasoning: bool = False
    call_id: str = ""
    duration_ms: int = 0
    is_error: bool = False
    error_summary: str = ""
    finish_reason: str = ""


class _ScriptedChatService:
    def __init__(self, events: list[_Ev]) -> None:
        self.events = events
        self.calls: list[Any] = []

    def run(self, request: Any, cancel: Any) -> Any:
        self.calls.append(request)

        async def _gen() -> Any:
            for ev in self.events:
                yield ev

        return _gen()


class _FakeOneBotAdapter:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send_action(self, action: Any) -> None:
        self.sent.append(action)


def _sample_group_event() -> MessageEvent:
    return MessageEvent(
        self_id=100,
        message_type=MessageType.GROUP,
        sub_type="normal",
        group_id=12345,
        user_id=555,
        message_id=42,
        message=[TextSegment(text="格兰早")],
        raw_message="格兰早",
        time=1_700_000_000,
        sender=None,
    )


@pytest.fixture
def status_links_on() -> Iterator[None]:
    """Wire the status-link seam ON for the test, reset to OFF on teardown.

    ``configure_status_links`` mutates process-wide globals, so the teardown
    keeps sibling tests (and other modules importing ``service``) hermetic.
    """
    service.configure_status_links(
        public_url="https://bot.example.com",
        enabled=True,
        minter=lambda sk: "TOK",
    )
    yield
    service.configure_status_links()  # defaults: disabled, no url, no minter


_EXPECTED_LINK = "🔗 实时状态: https://bot.example.com/status/TOK"


def _telegram_inbound() -> InboundEvent:
    binding = ChannelBinding(
        channel="telegram", account="999", thread="77", sender="77"
    )
    return InboundEvent(
        channel="telegram", binding=binding, text="go", message_id="1"
    )


async def _noop_edit(_text: str) -> None:
    return None


# ---------------------------------------------------------------------------
# _drive_spinner — the shared Telegram/Discord/Slack/Feishu loop.
# ---------------------------------------------------------------------------


class TestDriveSpinnerEarlyEmit:
    @pytest.mark.asyncio
    async def test_first_spawn_emits_link_once(
        self, status_links_on: None
    ) -> None:
        """Two spawn tool_calls in one turn → callback fires exactly once
        and ``outcome.status_link_emitted`` is set."""
        svc = _ScriptedChatService([
            _Ev(kind="tool_call", tool="subagent_spawn"),
            _Ev(kind="tool_call", tool="subagent_spawn_many"),
            _Ev(kind="token_delta", text="all done"),
            _Ev(kind="done"),
        ])
        spinner = MutableSpinner(_noop_edit)
        emitted: list[str] = []

        async def _cb(line: str) -> None:
            emitted.append(line)

        outcome = await service._drive_spinner(
            spinner,
            svc,
            _telegram_inbound(),
            "m",
            asyncio.Event(),
            request=SimpleNamespace(),
            on_subagent_spawn=_cb,
        )
        assert emitted == [_EXPECTED_LINK]
        assert outcome.status_link_emitted is True

    @pytest.mark.asyncio
    async def test_inline_spawn_variant_also_triggers(
        self, status_links_on: None
    ) -> None:
        svc = _ScriptedChatService([
            _Ev(kind="tool_call", tool="subagent_spawn_inline"),
            _Ev(kind="done"),
        ])
        emitted: list[str] = []

        async def _cb(line: str) -> None:
            emitted.append(line)

        outcome = await service._drive_spinner(
            MutableSpinner(_noop_edit),
            svc,
            _telegram_inbound(),
            "m",
            asyncio.Event(),
            request=SimpleNamespace(),
            on_subagent_spawn=_cb,
        )
        assert emitted == [_EXPECTED_LINK]
        assert outcome.status_link_emitted is True

    @pytest.mark.asyncio
    async def test_no_spawn_no_emit(self, status_links_on: None) -> None:
        """A turn that calls ordinary tools (no sub-agent) never surfaces
        the early link — the end-of-turn footer path handles it instead."""
        svc = _ScriptedChatService([
            _Ev(kind="tool_call", tool="web_search"),
            _Ev(kind="token_delta", text="answer"),
            _Ev(kind="done"),
        ])
        emitted: list[str] = []

        async def _cb(line: str) -> None:
            emitted.append(line)

        outcome = await service._drive_spinner(
            MutableSpinner(_noop_edit),
            svc,
            _telegram_inbound(),
            "m",
            asyncio.Event(),
            request=SimpleNamespace(),
            on_subagent_spawn=_cb,
        )
        assert emitted == []
        assert outcome.status_link_emitted is False

    @pytest.mark.asyncio
    async def test_no_emit_when_link_feature_off(self) -> None:
        """Spawn happens but the status-link feature is unconfigured →
        nothing to send, callback never fires, flag stays False (so the
        end-of-turn footer is also a no-op)."""
        service.configure_status_links()  # explicitly OFF
        svc = _ScriptedChatService([
            _Ev(kind="tool_call", tool="subagent_spawn"),
            _Ev(kind="done"),
        ])
        emitted: list[str] = []

        async def _cb(line: str) -> None:
            emitted.append(line)

        outcome = await service._drive_spinner(
            MutableSpinner(_noop_edit),
            svc,
            _telegram_inbound(),
            "m",
            asyncio.Event(),
            request=SimpleNamespace(),
            on_subagent_spawn=_cb,
        )
        assert emitted == []
        assert outcome.status_link_emitted is False

    @pytest.mark.asyncio
    async def test_callback_failure_never_breaks_stream(
        self, status_links_on: None
    ) -> None:
        """A send failure in the early-emit callback must be swallowed —
        the turn still completes and produces its reply text."""
        svc = _ScriptedChatService([
            _Ev(kind="tool_call", tool="subagent_spawn"),
            _Ev(kind="token_delta", text="survived"),
            _Ev(kind="done"),
        ])
        spinner = MutableSpinner(_noop_edit)

        async def _cb(_line: str) -> None:
            raise RuntimeError("transport exploded")

        outcome = await service._drive_spinner(
            spinner,
            svc,
            _telegram_inbound(),
            "m",
            asyncio.Event(),
            request=SimpleNamespace(),
            on_subagent_spawn=_cb,
        )
        # Not marked emitted (send failed) → end-of-turn footer still
        # appends the link as the fallback.
        assert outcome.status_link_emitted is False
        assert "".join(spinner.text_parts) == "survived"


# ---------------------------------------------------------------------------
# _build_footer_for_outcome — end-of-turn de-dup.
# ---------------------------------------------------------------------------


class TestFooterDeDup:
    def test_footer_skips_link_when_already_emitted(
        self, status_links_on: None
    ) -> None:
        outcome = service._DriveSpinnerOutcome(status_link_emitted=True)
        footer = service._build_footer_for_outcome(
            outcome, service._FooterState(), session_key="sess"
        )
        assert _EXPECTED_LINK not in footer
        # Nothing else to render on an emitter-less turn → empty footer.
        assert footer == ""

    def test_footer_keeps_link_when_not_emitted(
        self, status_links_on: None
    ) -> None:
        outcome = service._DriveSpinnerOutcome(status_link_emitted=False)
        footer = service._build_footer_for_outcome(
            outcome, service._FooterState(), session_key="sess"
        )
        assert footer == _EXPECTED_LINK

    def test_w41_footer_kept_link_dropped_when_emitted(
        self, status_links_on: None
    ) -> None:
        """The cost/elapsed (W4.1) line must still render even when the
        status link was already pushed mid-turn — only the link is deduped."""
        outcome = service._DriveSpinnerOutcome(status_link_emitted=True)
        fs = service._FooterState(
            elapsed_ms=12_000,
            estimated_cost_usd=0.01,
            cost_status="estimated",
            tool_call_count=2,
            populated=True,
        )
        footer = service._build_footer_for_outcome(
            outcome, fs, session_key="sess"
        )
        assert "elapsed:" in footer
        assert _EXPECTED_LINK not in footer


# ---------------------------------------------------------------------------
# handle_one_qq — end-to-end on a summary-style channel.
# ---------------------------------------------------------------------------


def _action_text(action: object) -> str:
    """Flatten a SendGroupMsg/SendPrivateMsg action into its text."""
    out: list[str] = []
    for seg in getattr(action, "message", []) or []:
        text = getattr(seg, "text", None)
        if isinstance(text, str):
            out.append(text)
    return "".join(out)


class TestQqEndToEndEarlyLink:
    @pytest.mark.asyncio
    async def test_early_link_sent_and_not_duplicated(
        self, status_links_on: None
    ) -> None:
        svc = _ScriptedChatService([
            _Ev(kind="tool_call", tool="subagent_spawn"),
            _Ev(kind="token_delta", text="here is the answer"),
            _Ev(kind="done"),
        ])
        ev = _sample_group_event()
        binding = ChannelBinding.qq_group(ev.self_id, ev.group_id or 0, ev.user_id)
        req = RoutedRequest(binding=binding, content="hi")
        adapter = _FakeOneBotAdapter()

        await service.handle_one_qq(
            svc, req, ev, "m", adapter, asyncio.Event()  # type: ignore[arg-type]
        )

        texts = [_action_text(a) for a in adapter.sent]
        link_hits = [t for t in texts if _EXPECTED_LINK in t]
        # Exactly one message carries the link, and it is the standalone
        # early message — NOT appended to the final answer bubble.
        assert len(link_hits) == 1, texts
        assert link_hits[0].strip() == _EXPECTED_LINK
        # The final reply bubble carries the answer without a second link.
        assert any("here is the answer" in t for t in texts)
        answer_bubble = next(t for t in texts if "here is the answer" in t)
        assert _EXPECTED_LINK not in answer_bubble

    @pytest.mark.asyncio
    async def test_no_spawn_link_only_at_end(
        self, status_links_on: None
    ) -> None:
        """Regression guard: a turn with NO sub-agent keeps the existing
        end-of-turn behaviour — the link rides the final bubble, and there
        is no separate early message."""
        svc = _ScriptedChatService([
            _Ev(kind="token_delta", text="just a chat reply"),
            _Ev(kind="done"),
        ])
        ev = _sample_group_event()
        binding = ChannelBinding.qq_group(ev.self_id, ev.group_id or 0, ev.user_id)
        req = RoutedRequest(binding=binding, content="hi")
        adapter = _FakeOneBotAdapter()

        await service.handle_one_qq(
            svc, req, ev, "m", adapter, asyncio.Event()  # type: ignore[arg-type]
        )
        texts = [_action_text(a) for a in adapter.sent]
        # Single bubble: the reply + appended link, no standalone early msg.
        assert len(texts) == 1
        assert "just a chat reply" in texts[0]
        assert _EXPECTED_LINK in texts[0]
