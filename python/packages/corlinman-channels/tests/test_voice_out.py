"""Outbound voice: classification, transcoding and per-channel routing.

The contract these pin down is "a voice note should arrive as a voice
note, and when it cannot, it must still arrive". Every path here has a
fallback, so the tests assert the fallback as hard as the happy path:

* Telegram must transcode mp3 → OGG/Opus (its ``sendVoice`` renders a
  waveform for nothing else) and fall back to ``send_document`` when
  ffmpeg is missing;
* WeChat must actually call its long-dead ``send_voice_customer``;
* QQ Official must refuse a non-SILK clip rather than fail an upload the
  CDN would reject;
* Slack/Discord/Feishu must label audio truthfully even though they only
  have a generic file upload.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_channels import service
from corlinman_channels.voice_out import (
    AUDIO_SUFFIXES,
    VOICE_NOTE_SPECS,
    ffmpeg_available,
    is_audio,
    prepare_voice_note,
    voice_status_line,
)

_needs_ffmpeg = pytest.mark.skipif(
    not ffmpeg_available(), reason="ffmpeg not installed"
)


def _make_tone(dst: Path, *, freq: int = 440, seconds: int = 1) -> Path:
    """Render a real mp3 with ffmpeg so transcode tests are genuine.

    Deliberately synchronous: spawning through ``asyncio.run`` here would
    bind the child's transport to a loop that is closed before the test's
    own loop runs, and awaiting it later raises "attached to a different
    loop".
    """
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
            "-c:a", "libmp3lame", "-b:a", "64k", str(dst),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return dst


@pytest.fixture()
def mp3(tmp_path: Path) -> Path:
    """A tiny real mp3 produced by ffmpeg, so transcodes are genuine."""
    if not ffmpeg_available():
        pytest.skip("ffmpeg not installed")
    out = _make_tone(tmp_path / "clip.mp3")
    assert out.is_file() and out.stat().st_size > 0
    return out


def _tool_call(path: Path, caption: str | None = None) -> Any:
    import json

    args: dict[str, Any] = {"path": str(path)}
    if caption:
        args["caption"] = caption
    return SimpleNamespace(
        tool="send_attachment", args_json=json.dumps(args).encode(), id="tc-1"
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["a.mp3", "a.ogg", "a.opus", "a.wav", "a.amr", "a.silk", "a.m4a"]
)
def test_audio_containers_are_recognised(name: str) -> None:
    assert is_audio(Path(name))


@pytest.mark.parametrize("name", ["a.pdf", "a.png", "a.txt", "a.zip"])
def test_non_audio_is_not_misrouted(name: str) -> None:
    assert not is_audio(Path(name))


def test_silk_and_amr_are_covered_despite_having_no_registered_mime() -> None:
    # mimetypes cannot resolve these, so the extension set is load-bearing.
    import mimetypes

    assert mimetypes.guess_type("x.silk")[0] is None
    assert ".silk" in AUDIO_SUFFIXES
    assert is_audio(Path("x.silk"))


def test_mime_override_wins_for_unknown_extension() -> None:
    assert is_audio(Path("recording.bin"), "audio/mpeg")


def test_status_line_distinguishes_voice_note_from_file() -> None:
    assert "语音" in voice_status_line("a.mp3", native=True)
    assert "音频文件" in voice_status_line("a.mp3", native=False)


# ---------------------------------------------------------------------------
# Transcoding
# ---------------------------------------------------------------------------


@_needs_ffmpeg
async def test_telegram_transcodes_mp3_to_opus(mp3: Path) -> None:
    out = await prepare_voice_note(mp3, "telegram")
    assert out is not None
    assert out.suffix == ".ogg"
    assert out != mp3
    assert out.stat().st_size > 0


@_needs_ffmpeg
async def test_native_container_is_passed_through_untouched(mp3: Path) -> None:
    # NapCat does its own SILK conversion, so mp3 is native for QQ.
    assert await prepare_voice_note(mp3, "qq") == mp3


@_needs_ffmpeg
async def test_transcode_result_is_cached(mp3: Path) -> None:
    first = await prepare_voice_note(mp3, "telegram")
    second = await prepare_voice_note(mp3, "telegram")
    assert first == second


@_needs_ffmpeg
async def test_cache_key_tracks_content_not_just_filename(
    mp3: Path, tmp_path: Path
) -> None:
    """A regenerated clip reusing a filename must not serve a stale ogg."""
    first = await prepare_voice_note(mp3, "telegram")
    assert first is not None
    original = first.read_bytes()

    _make_tone(mp3, freq=880, seconds=3)

    second = await prepare_voice_note(mp3, "telegram")
    assert second is not None
    assert second != first
    assert second.read_bytes() != original


async def test_missing_ffmpeg_degrades_to_file_send(
    mp3: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import corlinman_channels.voice_out as vo

    monkeypatch.setattr(vo, "ffmpeg_available", lambda: False)
    assert await prepare_voice_note(mp3, "telegram") is None


async def test_qq_official_refuses_non_silk(mp3: Path) -> None:
    # No general encoder produces SILK; sending anything else would be
    # rejected by Tencent's CDN, so we must not try.
    assert VOICE_NOTE_SPECS["qq_official"].transcode_supported is False
    assert await prepare_voice_note(mp3, "qq_official") is None


async def test_qq_official_accepts_a_real_silk_clip(tmp_path: Path) -> None:
    clip = tmp_path / "v.silk"
    clip.write_bytes(b"#!SILK_V3fakepayload")
    assert await prepare_voice_note(clip, "qq_official") == clip


async def test_qq_official_rejects_amr(tmp_path: Path) -> None:
    """Tencent's voice slot is SILK-only — .amr must not be waved through.

    Listing it as "native" would turn a clean "skipped, wrong format"
    notice into an upload the CDN rejects.
    """
    clip = tmp_path / "v.amr"
    clip.write_bytes(b"#!AMR\n" + b"\x00" * 64)
    assert await prepare_voice_note(clip, "qq_official") is None


async def test_oversized_clip_is_rejected_for_wechat(tmp_path: Path) -> None:
    big = tmp_path / "big.mp3"
    big.write_bytes(b"\x00" * (3 * 1024 * 1024))  # cap is 2 MB
    assert await prepare_voice_note(big, "wechat_official") is None


async def test_unknown_channel_has_no_voice_note_path(mp3: Path) -> None:
    assert await prepare_voice_note(mp3, "slack") is None


async def test_failed_transcode_returns_none(tmp_path: Path) -> None:
    if not ffmpeg_available():
        pytest.skip("ffmpeg not installed")
    junk = tmp_path / "notaudio.mp3"
    junk.write_bytes(b"this is definitely not an mp3")
    assert await prepare_voice_note(junk, "telegram") is None


# ---------------------------------------------------------------------------
# Channel routing
# ---------------------------------------------------------------------------


@_needs_ffmpeg
async def test_telegram_sends_audio_as_a_voice_note(mp3: Path) -> None:
    calls: dict[str, Any] = {}

    class _Sender:
        async def send_voice(self, chat_id, path, caption=None):  # type: ignore[no-untyped-def]
            calls["voice"] = (chat_id, Path(path), caption)
            return 1

        async def send_document(self, *a, **k):  # type: ignore[no-untyped-def]
            calls["document"] = (a, k)
            return 1

    status = await service._telegram_send_attachment(
        _Sender(), 42, None, _tool_call(mp3)
    )
    assert "document" not in calls
    assert calls["voice"][1].suffix == ".ogg"
    assert "语音" in status


async def test_telegram_falls_back_to_document_without_ffmpeg(
    mp3: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import corlinman_channels.voice_out as vo

    monkeypatch.setattr(vo, "ffmpeg_available", lambda: False)
    calls: dict[str, Any] = {}

    class _Sender:
        async def send_voice(self, *a, **k):  # type: ignore[no-untyped-def]
            calls["voice"] = True
            return 1

        async def send_document(self, chat_id, path, caption=None, filename=None, mime=""):  # type: ignore[no-untyped-def]
            calls["document"] = Path(path)
            return 1

    status = await service._telegram_send_attachment(
        _Sender(), 42, None, _tool_call(mp3)
    )
    assert "voice" not in calls
    assert calls["document"] == mp3
    # Still labelled as audio, so the user knows what arrived.
    assert "音频" in status


@_needs_ffmpeg
async def test_wechat_uploads_and_sends_a_voice_message(mp3: Path) -> None:
    """The previously-dead send_voice_customer path is now reached."""
    calls: dict[str, Any] = {}

    class _Sender:
        async def upload_temp_media(self, media_type, path):  # type: ignore[no-untyped-def]
            calls["upload"] = (media_type, Path(path))
            return "MEDIA-1"

        async def send_voice_customer(self, openid, media_id):  # type: ignore[no-untyped-def]
            calls["voice"] = (openid, media_id)

        async def send_text_customer(self, openid, content):  # type: ignore[no-untyped-def]
            calls.setdefault("text", []).append(content)

    status = await service._wechat_send_attachment(_Sender(), "openid-9", _tool_call(mp3))
    assert calls["upload"][0] == "voice"
    assert calls["voice"] == ("openid-9", "MEDIA-1")
    assert "语音" in status


async def test_wechat_reports_a_clip_it_cannot_deliver(tmp_path: Path) -> None:
    big = tmp_path / "big.mp3"
    big.write_bytes(b"\x00" * (3 * 1024 * 1024))

    class _Sender:
        async def upload_temp_media(self, *a, **k):  # type: ignore[no-untyped-def]
            raise AssertionError("must not upload an oversized clip")

        async def send_voice_customer(self, *a, **k):  # type: ignore[no-untyped-def]
            raise AssertionError("must not send")

    status = await service._wechat_send_attachment(_Sender(), "o", _tool_call(big))
    assert "跳过" in status


async def test_wechat_has_no_generic_file_message(tmp_path: Path) -> None:
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"%PDF-1.4")

    class _Sender:
        async def upload_temp_media(self, *a, **k):  # type: ignore[no-untyped-def]
            raise AssertionError("no file message exists")

    status = await service._wechat_send_attachment(_Sender(), "o", _tool_call(doc))
    assert "不支持文件直发" in status


async def test_slack_labels_audio_as_audio(mp3: Path) -> None:
    class _Sender:
        async def upload_file(self, *a, **k):  # type: ignore[no-untyped-def]
            return "F1"

    status = await service._slack_send_attachment(_Sender(), "C1", None, _tool_call(mp3))
    assert "音频" in status


async def test_slack_still_labels_documents_as_files(tmp_path: Path) -> None:
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"%PDF")

    class _Sender:
        async def upload_file(self, *a, **k):  # type: ignore[no-untyped-def]
            return "F1"

    status = await service._slack_send_attachment(_Sender(), "C1", None, _tool_call(doc))
    assert "已发送文件" in status
