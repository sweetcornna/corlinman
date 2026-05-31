"""Gap-fill (lane-channels) — per-adapter inbound attachment parse.

Fixtures exercising :func:`extract_attachments` / sender-name / reply-to
on each platform's raw wire payload, plus the Telegram ``Message`` media
descriptors (sticker/video/audio/animation) and the
``tg-file-id:`` resolver.
"""

from __future__ import annotations

import json

from corlinman_channels.common import AttachmentKind
from corlinman_channels.telegram_media import attachment_file_id


# ---------------------------------------------------------------------------
# Telegram Message.to_attachments + body_text + sender name
# ---------------------------------------------------------------------------


def test_telegram_photo_picks_largest_and_uses_caption() -> None:
    from corlinman_channels.telegram import Message

    msg = Message.model_validate(
        {
            "message_id": 1,
            "chat": {"id": 5, "type": "private"},
            "date": 100,
            "from": {"id": 9, "first_name": "Alice", "username": "al"},
            "caption": "look at this",
            "photo": [
                {"file_id": "P_small", "file_size": 100},
                {"file_id": "P_big", "file_size": 9999},
            ],
        }
    )
    atts = msg.to_attachments()
    assert len(atts) == 1
    assert atts[0].kind == AttachmentKind.IMAGE
    assert atts[0].url == "tg-file-id:P_big"
    assert msg.body_text() == "look at this"  # caption surfaces as text
    assert msg.sender_display_name() == "Alice"


def test_telegram_sticker_descriptor() -> None:
    from corlinman_channels.telegram import Message

    msg = Message.model_validate(
        {
            "message_id": 2,
            "chat": {"id": 5, "type": "private"},
            "date": 1,
            "sticker": {"file_id": "S1", "emoji": "🎉", "set_name": "Party"},
        }
    )
    atts = msg.to_attachments()
    assert len(atts) == 1
    assert atts[0].mime == "image/webp"


def test_telegram_all_media_kinds() -> None:
    from corlinman_channels.telegram import Message

    msg = Message.model_validate(
        {
            "message_id": 3,
            "chat": {"id": 5, "type": "private"},
            "date": 1,
            "voice": {"file_id": "V1"},
            "audio": {"file_id": "AU1"},
            "video": {"file_id": "VID1"},
            "animation": {"file_id": "AN1"},
            "document": {"file_id": "D1", "file_name": "x.pdf", "mime_type": "application/pdf"},
        }
    )
    kinds = sorted(str(a.kind) for a in msg.to_attachments())
    assert kinds == ["audio", "audio", "document", "video", "video"]


def test_telegram_reply_to_text_via_body_text() -> None:
    from corlinman_channels.telegram import Message

    msg = Message.model_validate(
        {
            "message_id": 4,
            "chat": {"id": 5, "type": "private"},
            "date": 1,
            "text": "+1",
            "reply_to_message": {
                "message_id": 3,
                "chat": {"id": 5, "type": "private"},
                "date": 1,
                "caption": "a captioned photo",
            },
        }
    )
    assert msg.reply_to_message is not None
    assert msg.reply_to_message.body_text() == "a captioned photo"


def test_telegram_file_id_resolver() -> None:
    from corlinman_channels.common import Attachment

    a = Attachment(kind=AttachmentKind.IMAGE, url="tg-file-id:ABC123")
    assert attachment_file_id(a) == "ABC123"
    b = Attachment(kind=AttachmentKind.IMAGE, url="https://cdn/x.png")
    assert attachment_file_id(b) is None


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------


def test_discord_extract_attachments_and_attribution() -> None:
    from corlinman_channels.discord import (
        extract_attachments,
        reply_to_text,
        sender_display_name,
    )

    msg = {
        "attachments": [
            {"url": "https://cdn/x.png", "content_type": "image/png", "filename": "x.png"},
            {"url": "https://cdn/y.pdf", "filename": "y.pdf"},
        ],
        "member": {"nick": "Nicky"},
        "author": {"global_name": "Glob", "username": "u"},
        "referenced_message": {"content": "the parent"},
    }
    atts = extract_attachments(msg)
    assert [a.kind for a in atts] == [AttachmentKind.IMAGE, AttachmentKind.DOCUMENT]
    assert sender_display_name(msg) == "Nicky"
    assert reply_to_text(msg, "bot") == "the parent"


def test_discord_sticker_items() -> None:
    from corlinman_channels.discord import extract_attachments, sticker_hint

    msg = {"sticker_items": [{"id": "555", "name": "wow"}]}
    atts = extract_attachments(msg)
    assert len(atts) == 1
    assert atts[0].kind == AttachmentKind.IMAGE
    assert sticker_hint(msg) is not None


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------


def test_slack_extract_files_and_name() -> None:
    from corlinman_channels.slack import extract_attachments, sender_display_name

    event = {
        "files": [
            {"url_private": "https://s/a.jpg", "mimetype": "image/jpeg", "name": "a.jpg"},
            {"url_private": "https://s/b.bin", "mimetype": "application/zip", "name": "b.zip"},
        ],
        "user_profile": {"display_name": "Disp", "real_name": "Real Name"},
    }
    atts = extract_attachments(event)
    assert [a.kind for a in atts] == [AttachmentKind.IMAGE, AttachmentKind.DOCUMENT]
    assert sender_display_name(event) == "Disp"


def test_slack_prefers_download_url() -> None:
    from corlinman_channels.slack import extract_attachments

    event = {
        "files": [
            {
                "url_private": "https://s/priv",
                "url_private_download": "https://s/dl",
                "mimetype": "image/png",
            }
        ]
    }
    assert extract_attachments(event)[0].url == "https://s/dl"


# ---------------------------------------------------------------------------
# Feishu
# ---------------------------------------------------------------------------


def test_feishu_image_resource_token() -> None:
    from corlinman_channels.feishu import extract_attachments

    msg = {
        "message_type": "image",
        "message_id": "m1",
        "content": json.dumps({"image_key": "img_xyz"}),
    }
    atts = extract_attachments(msg)
    assert len(atts) == 1
    assert atts[0].kind == AttachmentKind.IMAGE
    assert atts[0].url == "feishu-resource:m1:img_xyz"


def test_feishu_audio_and_file() -> None:
    from corlinman_channels.feishu import extract_attachments

    audio = {
        "message_type": "audio",
        "message_id": "m2",
        "content": json.dumps({"file_key": "fk"}),
    }
    assert extract_attachments(audio)[0].kind == AttachmentKind.AUDIO
    doc = {
        "message_type": "file",
        "message_id": "m3",
        "content": json.dumps({"file_key": "fk2", "file_name": "report.pdf"}),
    }
    a = extract_attachments(doc)[0]
    assert a.kind == AttachmentKind.DOCUMENT
    assert a.file_name == "report.pdf"


def test_feishu_text_message_has_no_attachments() -> None:
    from corlinman_channels.feishu import extract_attachments

    msg = {
        "message_type": "text",
        "message_id": "m4",
        "content": json.dumps({"text": "hi"}),
    }
    assert extract_attachments(msg) == []


# ---------------------------------------------------------------------------
# QQ official
# ---------------------------------------------------------------------------


def test_qq_official_attachments_normalize_url() -> None:
    from corlinman_channels.qq_official import extract_attachments, sender_display_name

    payload = {
        "attachments": [
            {"url": "example.com/i.jpg", "content_type": "image/jpeg", "filename": "i.jpg"},
            {"url": "//cdn/v.mp4", "content_type": "video/mp4"},
        ],
        "author": {"username": "QQUser"},
    }
    atts = extract_attachments(payload)
    assert atts[0].url == "https://example.com/i.jpg"
    assert atts[0].kind == AttachmentKind.IMAGE
    assert atts[1].url == "https://cdn/v.mp4"
    assert atts[1].kind == AttachmentKind.VIDEO
    assert sender_display_name(payload) == "QQUser"


def test_qq_official_member_nick_preferred() -> None:
    from corlinman_channels.qq_official import sender_display_name

    payload = {"member": {"nick": "Nick"}, "author": {"username": "raw"}}
    assert sender_display_name(payload) == "Nick"
