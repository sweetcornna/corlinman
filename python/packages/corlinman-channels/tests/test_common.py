"""Tests for ``corlinman_channels.common`` (shared types).

Exercises the cross-cutting pieces (``InboundEvent``, ``ChannelBinding``
session-key stability, ``Attachment`` shape) so the per-channel test
modules can focus on transport-specific behaviour.
"""

from __future__ import annotations

import pytest
from corlinman_channels.common import (
    Attachment,
    AttachmentKind,
    ChannelBinding,
    ChannelError,
    ConfigError,
    InboundAdapter,
    InboundEvent,
    TransportError,
    UnsupportedError,
    normalize_outbound_text,
)


class TestNormalizeOutboundText:
    """Plain-text channels render no markdown — flatten the scaffolding
    while keeping code blocks and Chinese typography intact."""

    def test_strips_bold_and_italic_emphasis(self) -> None:
        assert normalize_outbound_text("- **id**: `zhang`") == "· id: zhang"
        assert normalize_outbound_text("a *b* c __d__ e") == "a b c d e"
        assert normalize_outbound_text("***x***") == "x"

    def test_strips_headings_and_blockquotes(self) -> None:
        assert normalize_outbound_text("## Title\nbody") == "Title\nbody"
        assert normalize_outbound_text("> quoted") == "quoted"

    def test_bullets_become_clean_middot(self) -> None:
        out = normalize_outbound_text("- one\n- two")
        assert out == "· one\n· two"

    def test_preserves_chinese_full_width_punctuation(self) -> None:
        s = "你好，世界。这是“引号”、顿号；问号？"
        assert normalize_outbound_text(s) == s

    def test_normalizes_ai_tell_latin_punctuation(self) -> None:
        assert normalize_outbound_text("a — b") == "a - b"
        assert normalize_outbound_text("wait…") == "wait..."

    def test_preserves_fenced_code_blocks_verbatim(self) -> None:
        src = "see:\n```py\nx = **1**  # not bold\n```\ndone"
        out = normalize_outbound_text(src)
        assert "x = **1**  # not bold" in out
        assert "```py" in out

    def test_preserves_underscores_in_identifiers_and_paths(self) -> None:
        # Intra-word underscores are NOT markdown emphasis — must survive.
        assert normalize_outbound_text("id: zhang_xuefeng") == "id: zhang_xuefeng"
        assert normalize_outbound_text("my_file.py") == "my_file.py"
        assert (
            normalize_outbound_text("/tmp/foo_bar/baz_qux.txt")
            == "/tmp/foo_bar/baz_qux.txt"
        )
        # Real underscore emphasis at word boundaries still flattens.
        assert normalize_outbound_text("a __b__ c") == "a b c"
        assert normalize_outbound_text("_lead_ word") == "lead word"

    def test_keeps_backticks_around_mentions(self) -> None:
        # Stripping backticks off a mention could turn it into a live ping
        # on render-and-parse channels (Slack/Discord) — keep them.
        assert normalize_outbound_text("`@everyone`") == "`@everyone`"
        assert normalize_outbound_text("`<@U123>`") == "`<@U123>`"
        # Non-mention inline code still unwraps.
        assert normalize_outbound_text("the `value` here") == "the value here"

    def test_idempotent(self) -> None:
        once = normalize_outbound_text("- **a** `b` — c")
        assert normalize_outbound_text(once) == once

    def test_empty_and_plain_passthrough(self) -> None:
        assert normalize_outbound_text("") == ""
        assert normalize_outbound_text("just text") == "just text"


class TestChannelBinding:
    """Builder + session-key stability."""

    def test_session_key_is_deterministic(self) -> None:
        a = ChannelBinding(channel="qq", account="100", thread="200", sender="300")
        b = ChannelBinding(channel="qq", account="100", thread="200", sender="300")
        assert a.session_key() == b.session_key()
        assert len(a.session_key()) == 16

    def test_session_key_differs_per_tuple(self) -> None:
        a = ChannelBinding(channel="qq", account="1", thread="2", sender="3")
        b = ChannelBinding(channel="qq", account="1", thread="2", sender="4")
        assert a.session_key() != b.session_key()

    def test_qq_group_builder(self) -> None:
        b = ChannelBinding.qq_group(100, 12345, 555)
        assert b.channel == "qq"
        assert b.account == "100"
        assert b.thread == "12345"
        assert b.sender == "555"

    def test_qq_private_uses_user_id_as_thread(self) -> None:
        b = ChannelBinding.qq_private(100, 555)
        assert b.thread == b.sender == "555"

    def test_telegram_user_id_defaults_to_chat_id(self) -> None:
        b = ChannelBinding.telegram(bot_id=999, chat_id=42)
        assert b.sender == "42"

    def test_telegram_user_id_overrides_chat_id(self) -> None:
        b = ChannelBinding.telegram(bot_id=999, chat_id=-100, user_id=77)
        assert b.thread == "-100"
        assert b.sender == "77"


class TestAttachment:
    """Attachment is a frozen dataclass so mutation should fail."""

    def test_image_url_attachment_round_trip(self) -> None:
        a = Attachment(
            kind=AttachmentKind.IMAGE,
            url="https://cdn/x.png",
            mime="image/*",
            file_name="x.png",
        )
        assert a.kind == AttachmentKind.IMAGE
        assert a.url == "https://cdn/x.png"
        assert a.data is None

    def test_attachment_is_frozen(self) -> None:
        a = Attachment(kind=AttachmentKind.AUDIO)
        with pytest.raises(Exception):
            a.kind = AttachmentKind.IMAGE  # type: ignore[misc]


class TestInboundEvent:
    """The normalized envelope is a frozen dataclass with sensible defaults."""

    def test_defaults_are_sane(self) -> None:
        binding = ChannelBinding(channel="qq", account="1", thread="2", sender="3")
        ev: InboundEvent[None] = InboundEvent(channel="qq", binding=binding, text="hi")
        assert ev.message_id is None
        assert ev.timestamp == 0
        assert ev.mentioned is False
        assert ev.attachments == []
        assert ev.payload is None
        assert ev.user_id is None

    def test_payload_is_generic(self) -> None:
        binding = ChannelBinding(channel="qq", account="1", thread="2", sender="3")
        ev: InboundEvent[dict] = InboundEvent(
            channel="qq", binding=binding, text="x", payload={"k": "v"}
        )
        assert ev.payload == {"k": "v"}


class TestErrors:
    """Error hierarchy: every concrete error inherits from ``ChannelError``."""

    @pytest.mark.parametrize(
        "cls", [ConfigError, TransportError, UnsupportedError]
    )
    def test_concrete_errors_inherit_from_base(self, cls: type[Exception]) -> None:
        assert issubclass(cls, ChannelError)

    def test_raise_and_catch_via_base(self) -> None:
        with pytest.raises(ChannelError):
            raise ConfigError("bad")


class TestInboundAdapterProtocol:
    """The Protocol is structural — any class with an ``inbound()`` method
    satisfies it without subclassing."""

    def test_protocol_check_succeeds_for_compliant_class(self) -> None:
        class Stub:
            def inbound(self):  # type: ignore[no-untyped-def]
                return iter([])

        assert isinstance(Stub(), InboundAdapter)

    def test_protocol_check_fails_without_inbound(self) -> None:
        class Stub:
            pass

        assert not isinstance(Stub(), InboundAdapter)
