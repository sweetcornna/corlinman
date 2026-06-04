"""Tests for persona injection on the two official channels — QQ Official
(api.sgroup.qq.com) and WeChat Official Account.

Both channels were missing the humanlike persona-injection wiring that the
five other channels (QQ / Telegram / Discord / Slack / Feishu) already
have (root cause R6 / gap M20). These tests pin the fix: with
``humanlike_enabled=True`` + ``persona_id`` + ``persona_store``, the chat
backend must see a leading ``role="system"`` message carrying the persona
body; with the gate off, no system message is injected.

The handlers are driven directly with a scripted chat service + fake
sender — no network round-trip, mirroring ``test_service.py`` /
``test_wechat_official.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_channels.common import ChannelBinding, InboundEvent
from corlinman_channels.service import (
    QqOfficialChannelParams,
    WeChatOfficialChannelParams,
    handle_one_qq_official,
    handle_one_wechat_official,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _Ev:
    def __init__(self, kind: str, text: str = "") -> None:
        self.kind = kind
        self.text = text


class _ScriptedChatService:
    """Streams a scripted list of events + records the request it ran on."""

    def __init__(self, events: list[_Ev]) -> None:
        self.events = events
        self.calls: list[Any] = []

    def run(self, request: Any, cancel: Any) -> Any:
        self.calls.append(request)

        async def _gen() -> Any:
            for ev in self.events:
                yield ev

        return _gen()


class _FakePersonaStore:
    """Returns a canned persona row by id (matches the W7 store surface)."""

    def __init__(self, persona_id: str, system_prompt: str) -> None:
        self._row = SimpleNamespace(
            id=persona_id,
            display_name="Test Persona",
            short_summary="",
            system_prompt=system_prompt,
            is_builtin=False,
        )

    async def get(self, persona_id: str) -> Any:
        return self._row if persona_id == self._row.id else None


class _FakeQqOfficialSender:
    """Records text sends. Only the surface ``handle_one_qq_official``
    exercises for a plain C2C text reply."""

    def __init__(self) -> None:
        self.text_sends: list[tuple[str, str, str | None]] = []
        self._next_id = 0

    async def send_c2c_text(
        self,
        openid: str,
        content: str,
        *,
        msg_id: str | None = None,
        event_id: str | None = None,
    ) -> str:
        self.text_sends.append((openid, content, msg_id))
        self._next_id += 1
        return f"msg_{self._next_id}"


class _FakeWeChatSender:
    """Records customer-service pushes. ``handle_one_wechat_official``
    only needs ``send_text_customer`` for a long reply."""

    def __init__(self) -> None:
        self.customer_sends: list[tuple[str, str]] = []

    async def send_text_customer(self, openid: str, text: str) -> None:
        self.customer_sends.append((openid, text))


def _qq_official_inbound() -> InboundEvent[Any]:
    binding = ChannelBinding(
        channel="qq_official",
        account="app_xyz",
        thread="ou_user_1",
        sender="ou_user_1",
    )
    return InboundEvent(
        channel="qq_official",
        binding=binding,
        text="hi",
        message_id="msg_inbound_1",
        timestamp=0,
        mentioned=True,
        attachments=[],
        payload={
            "id": "msg_inbound_1",
            "content": "hi",
            "_qq_official_event_type": "C2C_MESSAGE_CREATE",
        },
    )


def _wechat_inbound() -> InboundEvent[Any]:
    binding = ChannelBinding(
        channel="wechat_official",
        account="gh_a",
        thread="o_user",
        sender="o_user",
    )
    return InboundEvent(
        channel="wechat_official",
        binding=binding,
        text="ping",
        message_id="m1",
        timestamp=0,
        mentioned=True,
    )


# ---------------------------------------------------------------------------
# QQ Official
# ---------------------------------------------------------------------------


class TestQqOfficialPersonaInjection:
    @pytest.mark.asyncio
    async def test_persona_prepended_when_humanlike_on(self) -> None:
        svc = _ScriptedChatService([
            _Ev("token_delta", "hello"),
            _Ev("done"),
        ])
        sender = _FakeQqOfficialSender()
        store = _FakePersonaStore("grantley", "PERSONA-BODY-MARK\nYou are Grantley.")
        params = QqOfficialChannelParams(
            config={},
            model="m",
            chat_service=svc,
            humanlike_enabled=True,
            persona_id="grantley",
            persona_store=store,
        )
        await handle_one_qq_official(
            svc, _qq_official_inbound(), "m", sender, asyncio.Event(),  # type: ignore[arg-type]
            params=params,
        )
        assert svc.calls, "chat_service.run was never invoked"
        request = svc.calls[0]
        assert request.messages[0].role == "system"
        assert "PERSONA-BODY-MARK" in request.messages[0].content
        assert request.persona_id == "grantley"

    @pytest.mark.asyncio
    async def test_no_injection_when_humanlike_off(self) -> None:
        svc = _ScriptedChatService([
            _Ev("token_delta", "hello"),
            _Ev("done"),
        ])
        sender = _FakeQqOfficialSender()
        store = _FakePersonaStore("grantley", "PERSONA-BODY-MARK")
        params = QqOfficialChannelParams(
            config={},
            model="m",
            chat_service=svc,
            humanlike_enabled=False,  # off
            persona_id="grantley",
            persona_store=store,
        )
        await handle_one_qq_official(
            svc, _qq_official_inbound(), "m", sender, asyncio.Event(),  # type: ignore[arg-type]
            params=params,
        )
        request = svc.calls[0]
        assert "system" not in [m.role for m in request.messages]

    @pytest.mark.asyncio
    async def test_resolver_overrides_static_fields(self) -> None:
        svc = _ScriptedChatService([
            _Ev("token_delta", "ok"),
            _Ev("done"),
        ])
        sender = _FakeQqOfficialSender()
        store = _FakePersonaStore("kitty", "MEOW-PERSONA")
        params = QqOfficialChannelParams(
            config={},
            model="m",
            chat_service=svc,
            humanlike_enabled=False,  # static says off
            persona_id=None,
            persona_store=store,
            humanlike_resolver=lambda: (True, "kitty"),  # live says on
        )
        await handle_one_qq_official(
            svc, _qq_official_inbound(), "m", sender, asyncio.Event(),  # type: ignore[arg-type]
            params=params,
        )
        request = svc.calls[0]
        assert request.messages[0].role == "system"
        assert "MEOW-PERSONA" in request.messages[0].content
        assert request.persona_id == "kitty"

    @pytest.mark.asyncio
    async def test_no_params_is_unchanged(self) -> None:
        """Back-compat: callers that omit ``params`` get no system message
        and the turn still completes."""
        svc = _ScriptedChatService([
            _Ev("token_delta", "hello"),
            _Ev("done"),
        ])
        sender = _FakeQqOfficialSender()
        await handle_one_qq_official(
            svc, _qq_official_inbound(), "m", sender, asyncio.Event(),  # type: ignore[arg-type]
        )
        request = svc.calls[0]
        assert "system" not in [m.role for m in request.messages]
        assert sender.text_sends, "reply should still be sent"


# ---------------------------------------------------------------------------
# WeChat Official
# ---------------------------------------------------------------------------


class TestWeChatOfficialPersonaInjection:
    @pytest.mark.asyncio
    async def test_persona_prepended_when_humanlike_on(self) -> None:
        svc = _ScriptedChatService([
            _Ev("token_delta", "hello there"),
            _Ev("done"),
        ])
        sender = _FakeWeChatSender()
        store = _FakePersonaStore("grantley", "PERSONA-BODY-MARK\nYou are Grantley.")
        params = WeChatOfficialChannelParams(
            config={},
            model="m",
            chat_service=svc,
            humanlike_enabled=True,
            persona_id="grantley",
            persona_store=store,
        )
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        await handle_one_wechat_official(
            svc, _wechat_inbound(), "m", sender, asyncio.Event(),  # type: ignore[arg-type]
            passive_future=fut,
            params=params,
        )
        assert svc.calls, "chat_service.run was never invoked"
        request = svc.calls[0]
        assert request.messages[0].role == "system"
        assert "PERSONA-BODY-MARK" in request.messages[0].content
        assert request.persona_id == "grantley"

    @pytest.mark.asyncio
    async def test_no_injection_when_humanlike_off(self) -> None:
        svc = _ScriptedChatService([
            _Ev("token_delta", "hello there"),
            _Ev("done"),
        ])
        sender = _FakeWeChatSender()
        store = _FakePersonaStore("grantley", "PERSONA-BODY-MARK")
        params = WeChatOfficialChannelParams(
            config={},
            model="m",
            chat_service=svc,
            humanlike_enabled=False,  # off
            persona_id="grantley",
            persona_store=store,
        )
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        await handle_one_wechat_official(
            svc, _wechat_inbound(), "m", sender, asyncio.Event(),  # type: ignore[arg-type]
            passive_future=fut,
            params=params,
        )
        request = svc.calls[0]
        assert "system" not in [m.role for m in request.messages]

    @pytest.mark.asyncio
    async def test_resolver_overrides_static_fields(self) -> None:
        svc = _ScriptedChatService([
            _Ev("token_delta", "ok"),
            _Ev("done"),
        ])
        sender = _FakeWeChatSender()
        store = _FakePersonaStore("kitty", "MEOW-PERSONA")
        params = WeChatOfficialChannelParams(
            config={},
            model="m",
            chat_service=svc,
            humanlike_enabled=False,  # static says off
            persona_id=None,
            persona_store=store,
            humanlike_resolver=lambda: (True, "kitty"),  # live says on
        )
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        await handle_one_wechat_official(
            svc, _wechat_inbound(), "m", sender, asyncio.Event(),  # type: ignore[arg-type]
            passive_future=fut,
            params=params,
        )
        request = svc.calls[0]
        assert request.messages[0].role == "system"
        assert "MEOW-PERSONA" in request.messages[0].content
        assert request.persona_id == "kitty"

    @pytest.mark.asyncio
    async def test_no_params_is_unchanged(self) -> None:
        """Back-compat: callers that omit ``params`` get no system message
        and the turn still resolves the passive future."""
        svc = _ScriptedChatService([
            _Ev("token_delta", "hi there"),
            _Ev("done"),
        ])
        sender = _FakeWeChatSender()
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        await handle_one_wechat_official(
            svc, _wechat_inbound(), "m", sender, asyncio.Event(),  # type: ignore[arg-type]
            passive_future=fut,
        )
        request = svc.calls[0]
        assert "system" not in [m.role for m in request.messages]
        assert fut.done()
