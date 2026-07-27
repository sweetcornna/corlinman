"""Outbound voice notes — one classifier, one transcoder, seven channels.

``text_to_speech`` writes an mp3 into the workspace and the model hands
that path to ``send_attachment``. What happens next used to be decided
independently in each channel's ``_*_send_attachment`` helper, with
``mimetypes.guess_type`` duplicated in three places — so an mp3 arrived
as a *native voice note* on QQ, as a *document* on Telegram (whose
``sendVoice`` only accepts OGG/Opus), and not at all on WeChat.

This module centralises the two decisions that were scattered:

1. **Is this audio?** :func:`is_audio` — extension set plus MIME sniff,
   because the workspace writer knows the container but a user-supplied
   path may not have a registered MIME type.
2. **Can this channel send it as a voice note, and in what container?**
   :data:`VOICE_NOTE_SPECS` per channel, with :func:`prepare_voice_note`
   transcoding through ffmpeg when the source container is wrong.

Everything degrades: no ffmpeg, an unknown codec, an oversized clip, or a
channel with no voice API all fall back to sending the file as a normal
attachment. A voice message that arrives as a playable file is a much
better outcome than one that does not arrive.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import mimetypes
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

__all__ = [
    "AUDIO_SUFFIXES",
    "VOICE_NOTE_SPECS",
    "VoiceNoteSpec",
    "ffmpeg_available",
    "is_audio",
    "prepare_voice_note",
    "voice_status_line",
]


#: Containers we treat as audio regardless of what ``mimetypes`` thinks.
#: ``.silk`` and ``.amr`` are here because they are the native voice
#: containers on QQ and WeChat and Python does not map either.
AUDIO_SUFFIXES: frozenset[str] = frozenset(
    {
        ".aac",
        ".amr",
        ".flac",
        ".m4a",
        ".mp3",
        ".oga",
        ".ogg",
        ".opus",
        ".pcm",
        ".silk",
        ".wav",
        ".weba",
    }
)


def is_audio(path: Path, mime: str | None = None) -> bool:
    """True when ``path`` should be delivered as audio.

    Checks the sniffed MIME first, then the extension set — a
    workspace-generated ``.silk`` has no registered MIME type but is
    unambiguously audio.
    """
    resolved = mime or mimetypes.guess_type(path.name)[0] or ""
    if resolved.startswith("audio/"):
        return True
    return path.suffix.lower() in AUDIO_SUFFIXES


@dataclass(frozen=True, slots=True)
class VoiceNoteSpec:
    """What one channel's native voice-note API demands."""

    channel: str
    #: Containers the channel accepts as-is.
    native_suffixes: frozenset[str]
    #: Extension to transcode to when the source is not native.
    target_ext: str
    #: ffmpeg output arguments (input/output paths are added around them).
    ffmpeg_args: tuple[str, ...]
    #: Hard ceiling the upstream API enforces; 0 means "no known limit".
    max_bytes: int = 0
    #: ``True`` when no general-purpose encoder can produce the required
    #: container, so a non-native file can only be sent as a plain file.
    transcode_supported: bool = True


VOICE_NOTE_SPECS: dict[str, VoiceNoteSpec] = {
    # Telegram sendVoice renders a waveform bubble only for OGG/Opus;
    # anything else must go through sendAudio/sendDocument.
    "telegram": VoiceNoteSpec(
        channel="telegram",
        native_suffixes=frozenset({".ogg", ".oga", ".opus"}),
        target_ext=".ogg",
        ffmpeg_args=("-vn", "-c:a", "libopus", "-b:a", "32k", "-ar", "48000", "-ac", "1"),
        max_bytes=50 * 1024 * 1024,
    ),
    # NapCat/OneBot transcodes to SILK itself, so hand it anything common.
    "qq": VoiceNoteSpec(
        channel="qq",
        native_suffixes=frozenset(AUDIO_SUFFIXES),
        target_ext=".mp3",
        ffmpeg_args=("-vn", "-c:a", "libmp3lame", "-b:a", "64k", "-ar", "24000", "-ac", "1"),
    ),
    # WeChat customer-service voice: AMR or MP3, <=60s, <=2MB.
    "wechat_official": VoiceNoteSpec(
        channel="wechat_official",
        native_suffixes=frozenset({".amr", ".mp3"}),
        target_ext=".mp3",
        ffmpeg_args=(
            "-vn", "-c:a", "libmp3lame", "-b:a", "32k",
            "-ar", "16000", "-ac", "1", "-t", "60",
        ),
        max_bytes=2 * 1024 * 1024,
    ),
    # QQ official rich-media voice requires SILK, which no general
    # encoder produces — native voice only when the clip already is one.
    "qq_official": VoiceNoteSpec(
        channel="qq_official",
        native_suffixes=frozenset({".silk", ".amr"}),
        target_ext=".silk",
        ffmpeg_args=(),
        transcode_supported=False,
    ),
}


def ffmpeg_available() -> bool:
    """``True`` when an ffmpeg binary is on PATH."""
    return shutil.which("ffmpeg") is not None


def _cache_path(src: Path, target_ext: str, channel: str) -> Path:
    """Deterministic scratch path for a transcode.

    Keyed by absolute path + mtime + size so a regenerated clip that
    reuses a filename does not serve a stale conversion.
    """
    try:
        stat = src.stat()
        stamp = f"{src.resolve()}:{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        stamp = str(src)
    digest = hashlib.sha256(f"{channel}:{stamp}".encode()).hexdigest()[:24]
    root = Path(tempfile.gettempdir()) / "corlinman-voice"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{digest}{target_ext}"


async def _run_ffmpeg(src: Path, dst: Path, args: tuple[str, ...]) -> bool:
    """Transcode ``src`` → ``dst``. Returns success; never raises."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
    cmd.extend(args)
    cmd.append(str(dst))
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
    except (OSError, ValueError) as exc:
        _log.warning("voice transcode spawn failed: %s", exc)
        return False
    if proc.returncode != 0:
        _log.warning(
            "voice transcode failed rc=%s err=%s",
            proc.returncode,
            (stderr or b"").decode("utf-8", "replace")[:300],
        )
        with contextlib.suppress(OSError):
            dst.unlink(missing_ok=True)
        return False
    return dst.is_file() and dst.stat().st_size > 0


async def prepare_voice_note(path: Path, channel: str) -> Path | None:
    """Return a path the channel can send as a **native voice note**.

    ``None`` means "send it as a regular file instead" — the caller must
    always have that fallback. Reasons for ``None``: the channel has no
    voice API, the container cannot be produced (SILK), ffmpeg is absent,
    the transcode failed, or the result exceeds the channel's size cap.
    """
    spec = VOICE_NOTE_SPECS.get(channel)
    if spec is None:
        return None

    suffix = path.suffix.lower()
    if suffix in spec.native_suffixes:
        if spec.max_bytes:
            try:
                if path.stat().st_size > spec.max_bytes:
                    _log.info(
                        "voice note too large for %s: %s bytes > %s",
                        channel,
                        path.stat().st_size,
                        spec.max_bytes,
                    )
                    return None
            except OSError:
                return None
        return path

    if not spec.transcode_supported or not spec.ffmpeg_args:
        return None
    if not ffmpeg_available():
        _log.info(
            "voice note for %s needs %s but ffmpeg is unavailable; sending as file",
            channel,
            spec.target_ext,
        )
        return None

    dst = _cache_path(path, spec.target_ext, channel)
    if not (dst.is_file() and dst.stat().st_size > 0):
        if not await _run_ffmpeg(path, dst, spec.ffmpeg_args):
            return None
    if spec.max_bytes and dst.stat().st_size > spec.max_bytes:
        _log.info("transcoded voice note still too large for %s", channel)
        return None
    return dst


def voice_status_line(display: str, *, native: bool) -> str:
    """Status text rendered into the channel's progress placeholder."""
    if native:
        return f"🎙️ 已发送语音: {display}"
    return f"🎵 已发送音频文件: {display}"
