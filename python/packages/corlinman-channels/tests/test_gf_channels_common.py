"""Gap-fill (lane-channels) — common.py helpers.

Covers the MSG_BREAK split, the inbound attribution prefix, the sticker
vision-description placeholder, and the album/media-group merge-debounce
buffer.
"""

from __future__ import annotations

from corlinman_channels.common import (
    AlbumDebouncer,
    Attachment,
    AttachmentKind,
    ChannelBinding,
    InboundEvent,
    format_attribution_prefix,
    split_on_msg_break,
    sticker_placeholder,
)


# ---------------------------------------------------------------------------
# MSG_BREAK split (channels-msg-break-leak)
# ---------------------------------------------------------------------------


def test_split_on_msg_break_two_bubbles() -> None:
    assert split_on_msg_break("hello[MSG_BREAK]world") == ["hello", "world"]


def test_split_on_msg_break_strips_and_drops_empty() -> None:
    assert split_on_msg_break("  a  [MSG_BREAK]  [MSG_BREAK] b ") == ["a", "b"]


def test_split_on_msg_break_no_marker_returns_single() -> None:
    assert split_on_msg_break("just one") == ["just one"]


def test_split_on_msg_break_all_empty_returns_original() -> None:
    # Pure markers / whitespace fall back to the original text so a stray
    # marker never produces a zero-bubble (silently dropped) reply.
    assert split_on_msg_break("[MSG_BREAK]") == ["[MSG_BREAK]"]


# ---------------------------------------------------------------------------
# Sticker placeholder (channels-inbound-multimodal-dropped)
# ---------------------------------------------------------------------------


def test_sticker_placeholder_emoji_only() -> None:
    assert sticker_placeholder("😂") == "[sticker 😂]"


def test_sticker_placeholder_emoji_and_set() -> None:
    assert sticker_placeholder("😂", "Cats") == '[sticker 😂 from "Cats"]'


def test_sticker_placeholder_bare() -> None:
    assert sticker_placeholder() == "[sticker]"


# ---------------------------------------------------------------------------
# Attribution prefix
# ---------------------------------------------------------------------------


def test_attribution_sender_only() -> None:
    assert format_attribution_prefix(sender_name="Alice", reply_to_text=None) == "[Alice]"


def test_attribution_empty_when_nothing() -> None:
    assert format_attribution_prefix(sender_name=None, reply_to_text="  ") == ""


def test_attribution_sender_and_reply() -> None:
    out = format_attribution_prefix(sender_name="Bob", reply_to_text="hi there")
    assert out == '[Bob 回复 "hi there"]'


def test_attribution_reply_only() -> None:
    out = format_attribution_prefix(sender_name=None, reply_to_text="parent")
    assert out == '[回复 "parent"]'


def test_attribution_collapses_and_truncates_long_quote() -> None:
    long = "x" * 400
    out = format_attribution_prefix(
        sender_name=None, reply_to_text=long, max_quote_chars=50
    )
    assert out.endswith('…"]')
    # Truncated to the cap (plus the bracket/quote scaffolding).
    assert len(out) < 70


def test_attribution_collapses_multiline_quote() -> None:
    out = format_attribution_prefix(sender_name=None, reply_to_text="a\n\nb   c")
    assert out == '[回复 "a b c"]'


# ---------------------------------------------------------------------------
# Album / media-group debounce (channels-no-album-debounce)
# ---------------------------------------------------------------------------


def _binding() -> ChannelBinding:
    return ChannelBinding("telegram", "bot", "chat", "user")


def _ev(
    mid: int,
    *,
    group: str | None = None,
    text: str = "",
    n_att: int = 0,
) -> InboundEvent:
    atts = [
        Attachment(kind=AttachmentKind.IMAGE, url=f"u{mid}-{i}") for i in range(n_att)
    ]
    return InboundEvent(
        channel="telegram",
        binding=_binding(),
        text=text,
        message_id=str(mid),
        attachments=atts,
        media_group_id=group,
    )


def test_album_standalone_passes_through() -> None:
    clk = {"t": 0.0}
    deb = AlbumDebouncer(1.5, clock=lambda: clk["t"])
    out = deb.feed(_ev(1, text="hello"))
    assert len(out) == 1
    assert out[0].message_id == "1"


def test_album_items_buffer_until_window_lapses() -> None:
    clk = {"t": 0.0}
    deb = AlbumDebouncer(1.5, clock=lambda: clk["t"])
    # Three album members arrive within the window — none emit yet.
    assert deb.feed(_ev(2, group="g", text="caption", n_att=1)) == []
    clk["t"] = 0.5
    assert deb.feed(_ev(3, group="g", n_att=1)) == []
    clk["t"] = 0.9
    assert deb.feed(_ev(4, group="g", n_att=1)) == []
    # Window not yet lapsed.
    assert deb.flush_ready() == []
    # Advance past the window — the merged album flushes.
    clk["t"] = 3.0
    ready = deb.flush_ready()
    assert len(ready) == 1
    merged = ready[0]
    assert len(merged.attachments) == 3
    # First member's caption survives; attribution fields carry forward.
    assert merged.text == "caption"
    assert merged.message_id == "2"


def test_album_flush_when_new_unrelated_item_arrives_after_window() -> None:
    clk = {"t": 0.0}
    deb = AlbumDebouncer(1.0, clock=lambda: clk["t"])
    deb.feed(_ev(10, group="g1", n_att=1))
    clk["t"] = 2.0
    # A later standalone event triggers the lapsed album to flush first.
    out = deb.feed(_ev(11, text="next"))
    ids = [e.message_id for e in out]
    assert "10" in ids  # flushed album
    assert "11" in ids  # the standalone


def test_album_flush_all_drains_on_shutdown() -> None:
    deb = AlbumDebouncer(99.0)  # window so long flush_ready never fires
    deb.feed(_ev(20, group="g", n_att=2))
    deb.feed(_ev(21, group="g", n_att=1))
    drained = deb.flush_all()
    assert len(drained) == 1
    assert len(drained[0].attachments) == 3
    assert deb.flush_all() == []  # idempotent
