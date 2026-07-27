"""W3-3 channel-side approval flow (QQ text menu + shared plumbing).

Covers the acceptance path for channels: an ``awaiting_approval`` event
mid-turn posts a prompt into the conversation; the INITIATOR's reply is
intercepted before router dispatch and fed to the gateway approval broker;
non-initiator replies are ignored; an unanswered prompt produces an
explicit user-visible timeout notice (never a silent failure).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from corlinman_channels import service
from corlinman_channels.common import ChannelBinding
from corlinman_channels.onebot import MessageEvent, MessageType, TextSegment
from corlinman_channels.router import RoutedRequest


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
    args_preview_json: str = ""
    reason: str = ""


class _ScriptedChatService:
    """Yields the scripted events; parks after an ``awaiting_approval``
    until ``gate`` is set (mirrors the real agent stream, which stays
    open while the ask is pending — the handler's ``finally`` cleanup
    must not run before the decision)."""

    def __init__(self, events: list[_Ev], gate: asyncio.Event | None = None) -> None:
        self.events = events
        self.gate = gate

    def run(self, request: Any, cancel: Any) -> Any:
        async def _gen() -> Any:
            for ev in self.events:
                yield ev
                if ev.kind == "awaiting_approval" and self.gate is not None:
                    await self.gate.wait()

        return _gen()


class _FakeOneBotAdapter:
    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def send_action(self, action: Any) -> None:
        self.sent.append(action)


@dataclass
class _BrokerRecorder:
    calls: list[dict[str, Any]] = field(default_factory=list)
    result: bool = True
    gate: asyncio.Event | None = None

    async def __call__(
        self,
        call_id: str,
        *,
        approved: bool,
        scope: str,
        deny_message: str = "",
        session_key: str = "",
    ) -> bool:
        self.calls.append(
            {
                "call_id": call_id,
                "approved": approved,
                "scope": scope,
                "session_key": session_key,
                "deny_message": deny_message,
            }
        )
        if self.gate is not None:
            self.gate.set()
        return self.result


def _group_event(*, user_id: int = 555, text: str = "y") -> MessageEvent:
    return MessageEvent(
        self_id=100,
        message_type=MessageType.GROUP,
        sub_type="normal",
        group_id=12345,
        user_id=user_id,
        message_id=42,
        message=[TextSegment(text=text)],
        raw_message=text,
        time=1_700_000_000,
        sender=None,
    )


def _awaiting_ev(call_id: str = "call-appr-1") -> _Ev:
    return _Ev(
        kind="awaiting_approval",
        call_id=call_id,
        tool="run_shell",
        args_preview_json='{"command": "rm -rf /tmp/x"}',
        reason="permission rule requires approval",
    )


def _action_text(action: object) -> str:
    out: list[str] = []
    for seg in getattr(action, "message", []) or []:
        text = getattr(seg, "text", None)
        if isinstance(text, str):
            out.append(text)
    return "".join(out)


@pytest.fixture(autouse=True)
def _clean_registry() -> Any:
    service._PENDING_CHANNEL_APPROVALS.clear()
    yield
    for bucket in list(service._PENDING_CHANNEL_APPROVALS.values()):
        for entry in bucket.values():
            if entry.timeout_task is not None:
                entry.timeout_task.cancel()
    service._PENDING_CHANNEL_APPROVALS.clear()


# ---------------------------------------------------------------------------
# reply parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("y", (True, "once")),
        ("YES", (True, "once")),
        ("1", (True, "once")),
        ("批准", (True, "once")),
        ("s", (True, "session")),
        ("2", (True, "session")),
        ("a", (True, "always")),
        ("3", (True, "always")),
        ("n", (False, "once")),
        ("0", (False, "once")),
        ("拒绝", (False, "once")),
        ("hello there", None),
        ("", None),
    ],
)
def test_parse_approval_reply(text: str, expected: Any) -> None:
    assert service._parse_approval_reply(text) == expected


# ---------------------------------------------------------------------------
# QQ end-to-end: awaiting event → prompt → initiator decision → broker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qq_awaiting_event_posts_prompt_and_initiator_approves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()
    broker = _BrokerRecorder(gate=gate)
    monkeypatch.setattr(service, "_approval_decide_via_broker", broker)

    svc = _ScriptedChatService(
        [
            _awaiting_ev(),
            _Ev(kind="token_delta", text="done thinking"),
            _Ev(kind="done"),
        ],
        gate,
    )
    ev = _group_event()
    binding = ChannelBinding.qq_group(ev.self_id, ev.group_id or 0, ev.user_id)
    req = RoutedRequest(binding=binding, content="hi")
    adapter = _FakeOneBotAdapter()

    turn = asyncio.create_task(
        service.handle_one_qq(
            svc, req, ev, "m", adapter, asyncio.Event()  # type: ignore[arg-type]
        )
    )
    # The prompt menu must land while the turn is still streaming.
    for _ in range(50):
        if adapter.sent:
            break
        await asyncio.sleep(0.01)
    prompt = _action_text(adapter.sent[0])
    assert "run_shell" in prompt
    assert "rm -rf /tmp/x" in prompt
    assert "y/1" in prompt

    # Initiator answers "s" (approve + session grant) — consumed, broker hit.
    reply = _group_event(user_id=555, text="s")
    consumed = await service._qq_try_handle_approval_reply(adapter, reply, "default")
    assert consumed is True
    assert len(broker.calls) == 1
    call = broker.calls[0]
    assert call["call_id"] == "call-appr-1"
    assert call["approved"] is True
    assert call["scope"] == "session"
    assert call["deny_message"] == ""
    # Review fix: the decision is session-scoped — the registration
    # captured the initiator's binding session key and the broker decide
    # must present it (an unscoped decide would read as expired).
    assert call["session_key"], "decide must carry the stream's session key"
    # The confirmation message went back into the conversation.
    assert any("已批准" in _action_text(a) for a in adapter.sent[1:])
    await turn


@pytest.mark.asyncio
async def test_qq_non_initiator_reply_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()
    broker = _BrokerRecorder(gate=gate)
    monkeypatch.setattr(service, "_approval_decide_via_broker", broker)

    svc = _ScriptedChatService([_awaiting_ev(), _Ev(kind="done")], gate)
    ev = _group_event(user_id=555)
    binding = ChannelBinding.qq_group(ev.self_id, ev.group_id or 0, ev.user_id)
    req = RoutedRequest(binding=binding, content="hi")
    adapter = _FakeOneBotAdapter()

    turn = asyncio.create_task(
        service.handle_one_qq(
            svc, req, ev, "m", adapter, asyncio.Event()  # type: ignore[arg-type]
        )
    )
    for _ in range(50):
        if adapter.sent:
            break
        await asyncio.sleep(0.01)

    # Someone ELSE says "y" — not consumed, broker never touched, the
    # prompt stays pending for the real initiator.
    intruder = _group_event(user_id=999, text="y")
    consumed = await service._qq_try_handle_approval_reply(adapter, intruder, "default")
    assert consumed is False
    assert broker.calls == []
    assert service._PENDING_CHANNEL_APPROVALS  # still pending

    # The initiator's decision still works afterwards.
    reply = _group_event(user_id=555, text="n")
    assert await service._qq_try_handle_approval_reply(adapter, reply, "default")
    assert broker.calls[-1]["approved"] is False
    assert broker.calls[-1]["deny_message"]
    await turn


@pytest.mark.asyncio
async def test_qq_expired_decision_gets_expiry_notice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()
    broker = _BrokerRecorder(result=False, gate=gate)  # broker: unknown call_id
    monkeypatch.setattr(service, "_approval_decide_via_broker", broker)

    svc = _ScriptedChatService([_awaiting_ev(), _Ev(kind="done")], gate)
    ev = _group_event()
    binding = ChannelBinding.qq_group(ev.self_id, ev.group_id or 0, ev.user_id)
    req = RoutedRequest(binding=binding, content="hi")
    adapter = _FakeOneBotAdapter()
    turn = asyncio.create_task(
        service.handle_one_qq(
            svc, req, ev, "m", adapter, asyncio.Event()  # type: ignore[arg-type]
        )
    )
    for _ in range(50):
        if adapter.sent:
            break
        await asyncio.sleep(0.01)

    reply = _group_event(user_id=555, text="y")
    assert await service._qq_try_handle_approval_reply(adapter, reply, "default")
    assert any("已失效" in _action_text(a) for a in adapter.sent[1:])
    await turn


@pytest.mark.asyncio
async def test_timeout_notice_is_user_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Acceptance 7: an unanswered prompt produces an explicit expiry
    message in the conversation, not a silent failure."""
    monkeypatch.setattr(service, "_APPROVAL_TIMEOUT_S", 1.2)
    monkeypatch.setattr(service, "_APPROVAL_NOTICE_MARGIN_S", 0.2)

    notices: list[str] = []

    async def _notify() -> None:
        notices.append("timeout")

    entry = service._register_channel_approval(
        "qq:default:g1", _awaiting_ev(), "555", _notify
    )
    assert entry.timeout_task is not None
    await asyncio.wait_for(entry.timeout_task, timeout=5.0)
    assert notices == ["timeout"]
    # Entry cleared → a late reply reports expiry rather than deciding.
    assert "qq:default:g1" not in service._PENDING_CHANNEL_APPROVALS


# ---------------------------------------------------------------------------
# request capability flag
# ---------------------------------------------------------------------------


def test_qq_request_declares_approval_capable() -> None:
    ev = _group_event()
    binding = ChannelBinding.qq_group(ev.self_id, ev.group_id or 0, ev.user_id)
    req = RoutedRequest(binding=binding, content="hi")
    request = service._build_internal_request(req, ev, "m")
    assert request.approval_capable is True


def test_text_channel_request_defaults_fail_closed() -> None:
    """Discord/Slack/Feishu have no approval reply loop yet — their
    requests must NOT declare the capability."""
    from corlinman_channels.common import InboundEvent

    binding = ChannelBinding(
        channel="discord", account="a", thread="t", sender="s"
    )
    inbound = InboundEvent(
        channel="discord",
        binding=binding,
        text="hi",
    )
    request = service._build_text_channel_request(inbound, "m")
    assert request.approval_capable is False
    telegram_request = service._build_text_channel_request(
        inbound, "m", approval_capable=True
    )
    assert telegram_request.approval_capable is True


# ─── W3-3 review fixes ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_approvals_match_by_initiator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two users' approvals pend in ONE group — each initiator's reply
    answers their OWN prompt (plain FIFO let the oldest entry absorb
    every reply, so the second initiator could never decide)."""
    broker = _BrokerRecorder()
    monkeypatch.setattr(service, "_approval_decide_via_broker", broker)
    conv = "qq:default:g12345"
    service._register_channel_approval(
        conv, _awaiting_ev("call-A"), "111", None, session_key="sess-a"
    )
    service._register_channel_approval(
        conv, _awaiting_ev("call-B"), "222", None, session_key="sess-b"
    )

    # The SECOND initiator answers first — their own entry is decided.
    out = await service._try_handle_channel_approval_reply(conv, "222", "y")
    assert out is not None and "已批准" in out
    assert [c["call_id"] for c in broker.calls] == ["call-B"]
    assert broker.calls[0]["session_key"] == "sess-b"

    # The first initiator's entry is untouched and still decidable.
    out = await service._try_handle_channel_approval_reply(conv, "111", "n")
    assert out is not None and "已拒绝" in out
    assert [c["call_id"] for c in broker.calls] == ["call-B", "call-A"]


@pytest.mark.asyncio
async def test_bounded_spawn_releases_permit_on_park() -> None:
    """A turn parked on approval frees its concurrency permit — a
    saturated semaphore must not block the dispatch loop from reading
    the decision replies (deadlock finding, W3-3 review)."""
    sem = asyncio.Semaphore(1)
    pending: set[asyncio.Task[None]] = set()
    parked = asyncio.Event()
    unpark = asyncio.Event()

    async def _turn() -> None:
        service._release_turn_permit_early()
        parked.set()
        await unpark.wait()

    await service._bounded_spawn(sem, pending, _turn)
    await asyncio.wait_for(parked.wait(), timeout=2)
    # Permit already back while the turn is still parked…
    await asyncio.wait_for(sem.acquire(), timeout=2)
    sem.release()
    unpark.set()
    for t in list(pending):
        await t
    # …and turn completion does not double-release.
    await asyncio.wait_for(sem.acquire(), timeout=2)
    assert sem.locked()


def test_gate_exempt_predicate_matches_interceptor() -> None:
    """channel_approval_awaits_reply: pending + parseable + initiator."""
    conv = "telegram:777"
    service._register_channel_approval(
        conv, _awaiting_ev("call-tg"), "42", None, session_key="s"
    )
    assert service.channel_approval_awaits_reply(conv, "42", "y")
    assert not service.channel_approval_awaits_reply(conv, "43", "y")  # not initiator
    assert not service.channel_approval_awaits_reply(conv, "42", "hello")  # not a decision
    assert not service.channel_approval_awaits_reply("telegram:888", "42", "y")  # no pending
