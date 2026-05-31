"""Outbound Telegram Bot API: ``sendMessage`` / ``sendPhoto`` / ``sendVoice``.

Python port of ``rust/.../telegram/send.rs``. The Rust crate hand-rolls
the multipart boundary to avoid pulling in a multipart-encoder
dependency; we do the same here so the dep graph stays minimal
(httpx is already a dependency for the long-poll adapter).

Why not ``httpx`` multipart? httpx's ``files=`` parameter requires
either a file path or a ``BufferedIOBase``; building the body
ourselves and POSTing raw bytes parallels the Rust shape exactly and
keeps the wire format deterministic for tests that snapshot the
multipart payload.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from corlinman_channels.common import split_on_msg_break

__all__ = [
    "MAX_UPLOAD_BYTES",
    "PhotoSource",
    "SendError",
    "TelegramSender",
    "build_multipart",
]


# ---------------------------------------------------------------------------
# Upload guards
# ---------------------------------------------------------------------------


#: Hard cap on the size of local-file uploads (``sendDocument`` /
#: ``sendPhoto`` / ``sendVoice``). Telegram's documented bot-API limit
#: is 50 MiB for documents; we leave a safety headroom so the multipart
#: envelope + a few-percent-overhead chunked transfer never trips the
#: server-side ceiling. A runaway agent that wrote a 10 GiB file would
#: otherwise OOM the gateway via ``Path.read_bytes()``.
MAX_UPLOAD_BYTES: int = 45 * 1024 * 1024


def _check_upload_size(path: Path) -> None:
    """Raise :class:`SendIoError` if ``path`` exceeds :data:`MAX_UPLOAD_BYTES`.

    Called before ``Path.read_bytes()`` so we never materialise a multi-GB
    file into RAM. Streaming-multipart can land later; the size guard is
    the minimum needed to keep the gateway memory-stable.
    """
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SendIoError(str(exc)) from exc
    if size > MAX_UPLOAD_BYTES:
        raise SendIoError(
            f"file too large: {size} > {MAX_UPLOAD_BYTES} (path={path.name})"
        )


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SendError(Exception):
    """Base error for outbound calls. Mirrors Rust ``SendError`` enum."""


class SendApiError(SendError):
    """Telegram API rejected the request (``ok: false``)."""


class SendHttpError(SendError):
    """Network / HTTP failure."""


class SendIoError(SendError):
    """File I/O failed while reading the multipart payload."""


SendError.Api = SendApiError  # type: ignore[attr-defined]
SendError.Http = SendHttpError  # type: ignore[attr-defined]
SendError.Io = SendIoError  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Source variants
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _PhotoUrl:
    url: str


@dataclass(slots=True)
class _PhotoPath:
    path: Path


class PhotoSource:
    """Photo source variants. Mirrors Rust ``PhotoSource``::

        PhotoSource.Url("https://...")   # Telegram fetches it server-side
        PhotoSource.Path(Path("/tmp/x.jpg"))  # multipart upload
    """

    Url = _PhotoUrl
    Path = _PhotoPath


PhotoSourceT = _PhotoUrl | _PhotoPath


# ---------------------------------------------------------------------------
# Sender
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Multipart:
    body: bytes
    boundary: str


def build_multipart(
    chat_id: int,
    file_field: str,
    filename: str,
    bytes_: bytes,
    caption: str | None,
    content_type: str,
) -> _Multipart:
    """Assemble a minimal ``multipart/form-data`` body.

    Layout (matches Rust ``build_multipart``)::

        --BOUNDARY\\r\\n
        Content-Disposition: form-data; name="chat_id"\\r\\n\\r\\n
        12345\\r\\n
        --BOUNDARY\\r\\n
        Content-Disposition: form-data; name="photo"; filename="..."\\r\\n
        Content-Type: image/jpeg\\r\\n\\r\\n
        <bytes>\\r\\n
        --BOUNDARY--\\r\\n
    """
    boundary = f"corlinman-tg-{secrets.token_hex(16)}"
    body = bytearray()
    dash = b"--"
    crlf = b"\r\n"

    # chat_id text part
    body.extend(dash)
    body.extend(boundary.encode())
    body.extend(crlf)
    body.extend(b'Content-Disposition: form-data; name="chat_id"')
    body.extend(crlf)
    body.extend(crlf)
    body.extend(str(chat_id).encode())
    body.extend(crlf)

    # caption text part (optional)
    if caption is not None:
        body.extend(dash)
        body.extend(boundary.encode())
        body.extend(crlf)
        body.extend(b'Content-Disposition: form-data; name="caption"')
        body.extend(crlf)
        body.extend(crlf)
        body.extend(caption.encode())
        body.extend(crlf)

    # file part
    body.extend(dash)
    body.extend(boundary.encode())
    body.extend(crlf)
    header = (
        f'Content-Disposition: form-data; name="{file_field}"; '
        f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'
    )
    body.extend(header.encode())
    body.extend(bytes_)
    body.extend(crlf)

    # closing boundary
    body.extend(dash)
    body.extend(boundary.encode())
    body.extend(dash)
    body.extend(crlf)

    return _Multipart(body=bytes(body), boundary=boundary)


class TelegramSender:
    """Thin client over the bot HTTPS surface, scoped to the outbound path.

    Mirrors Rust ``TelegramSender``. Construct once per bot token and
    reuse — the underlying :class:`httpx.AsyncClient` connection pool
    is the actual cost.
    """

    __slots__ = ("_edit_rate_limit_until", "base", "client", "token")

    def __init__(
        self,
        client: httpx.AsyncClient,
        token: str,
        base: str = "https://api.telegram.org",
    ) -> None:
        self.client = client
        self.token = token
        self.base = base
        # Shared back-off budget for the two "decorative" endpoints
        # (``editMessageText`` + ``sendChatAction``). Telegram returns
        # HTTP 429 with ``parameters.retry_after`` when the bot is being
        # too chatty; further calls during the window deepen the ban,
        # so we silently skip them until the deadline passes.
        self._edit_rate_limit_until: float = 0.0

    def _endpoint(self, method: str) -> str:
        return f"{self.base}/bot{self.token}/{method}"

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        inline_keyboard: list[list[dict[str, str]]] | None = None,
    ) -> int:
        """POST ``/sendMessage``. Returns the Telegram ``message_id``.

        When ``inline_keyboard`` is provided we attach a
        ``reply_markup.inline_keyboard`` payload so the user sees
        clickable buttons under the message. The shape mirrors the
        Telegram bot API verbatim — each row is a list of
        ``{"text": "...", "callback_data": "..."}`` dicts. The agent's
        ``ask_user`` tool plumbs through here so a question with canned
        options becomes a button grid (see ``handle_one_telegram``).

        ``callback_data`` is hard-capped by Telegram at 64 bytes UTF-8;
        the caller is responsible for the cap. ``text`` on the button
        face is the user-visible label (no Telegram-side cap beyond the
        4096-char message body).

        Text containing ``[MSG_BREAK]`` markers is split into multiple
        bubbles sent sequentially; the last message id is returned.
        """
        bubbles = split_on_msg_break(text)
        last_id = 0
        for i, bubble in enumerate(bubbles):
            body: dict[str, object] = {"chat_id": chat_id, "text": bubble}
            # Only thread reply_to on the first bubble so the chain reads naturally.
            if reply_to_message_id is not None and i == 0:
                body["reply_to_message_id"] = reply_to_message_id
            # Inline keyboard attaches only to the last bubble.
            if inline_keyboard and i == len(bubbles) - 1:
                body["reply_markup"] = {"inline_keyboard": inline_keyboard}
            try:
                resp = await self.client.post(self._endpoint("sendMessage"), json=body)
            except httpx.HTTPError as exc:
                raise SendHttpError(str(exc)) from exc
            last_id = await _parse_envelope(resp)
        return last_id

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
    ) -> None:
        """POST ``/answerCallbackQuery``. Best-effort.

        Telegram requires the bot to acknowledge an inline-button press;
        otherwise the client shows a spinner on the button forever.
        ``text`` (optional) pops a toast on the user's screen. Failures
        are logged-and-swallowed because the actual inbound flow is
        already driven by the callback's payload — the ack is purely
        decorative.
        """
        payload: dict[str, object] = {"callback_query_id": callback_query_id}
        if text is not None:
            payload["text"] = text
        try:
            resp = await self.client.post(
                self._endpoint("answerCallbackQuery"), json=payload
            )
            # The endpoint returns ok:true on success; we don't care about
            # the response body. Non-2xx is best-effort logged via the
            # rate-limit hook so an over-eager call site can't break the
            # adapter loop.
            if resp.status_code == 429:
                self._note_retry_after(resp)
        except httpx.HTTPError:
            return

    async def send_photo(
        self,
        chat_id: int,
        source: PhotoSourceT,
        caption: str | None = None,
    ) -> int:
        """POST ``/sendPhoto``. URL source uses the simple JSON form;
        local-path source uses multipart upload."""
        if isinstance(source, _PhotoUrl):
            body: dict[str, object] = {"chat_id": chat_id, "photo": source.url}
            if caption is not None:
                body["caption"] = caption
            try:
                resp = await self.client.post(self._endpoint("sendPhoto"), json=body)
            except httpx.HTTPError as exc:
                raise SendHttpError(str(exc)) from exc
            return await _parse_envelope(resp)
        # PhotoSource.Path
        path = source.path
        _check_upload_size(path)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise SendIoError(str(exc)) from exc
        filename = path.name or "photo.bin"
        mp = build_multipart(chat_id, "photo", filename, content, caption, "image/jpeg")
        return await self._post_multipart("sendPhoto", mp)

    async def send_voice(
        self,
        chat_id: int,
        path: Path,
        caption: str | None = None,
    ) -> int:
        """POST ``/sendVoice`` from a local OGG path.

        Raises :class:`SendIoError` when ``path`` exceeds
        :data:`MAX_UPLOAD_BYTES` — protects the gateway from a runaway
        agent that wrote a multi-GB file into the media dir.
        """
        _check_upload_size(path)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise SendIoError(str(exc)) from exc
        filename = path.name or "voice.ogg"
        mp = build_multipart(chat_id, "voice", filename, content, caption, "audio/ogg")
        return await self._post_multipart("sendVoice", mp)

    async def send_document(
        self,
        chat_id: int,
        path: Path,
        caption: str | None = None,
        filename: str | None = None,
        mime: str = "application/octet-stream",
    ) -> int:
        """POST ``/sendDocument`` from a local file path.

        Used by the ``send_attachment`` agent tool — supports any file
        type (HTML, PDF, code, etc.). ``filename`` overrides the
        on-disk basename for the user-visible display.

        Raises :class:`SendIoError` when ``path`` exceeds
        :data:`MAX_UPLOAD_BYTES` — the channel handler folds the error
        into a friendly status line rather than crashing the turn.
        """
        _check_upload_size(path)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise SendIoError(str(exc)) from exc
        name = filename or path.name or "file.bin"
        mp = build_multipart(chat_id, "document", name, content, caption, mime)
        return await self._post_multipart("sendDocument", mp)

    async def send_chat_action(
        self, chat_id: int, action: str = "typing"
    ) -> None:
        """POST ``/sendChatAction``. Shows "Bot is typing…" in the
        Telegram client. The indicator auto-clears after ~5s, so callers
        re-fire periodically while a turn is in flight.

        Best-effort: a failure here never blocks the reply path. We log
        and swallow transport / API errors instead of raising.
        """
        if time.time() < self._edit_rate_limit_until:
            return
        body = {"chat_id": chat_id, "action": action}
        try:
            resp = await self.client.post(
                self._endpoint("sendChatAction"), json=body
            )
            if resp.status_code == 429:
                self._note_retry_after(resp)
                return
            if resp.status_code >= 400:
                # Don't raise — the indicator is decorative.
                return
        except httpx.HTTPError:
            return

    async def edit_message_text(
        self, chat_id: int, message_id: int, text: str
    ) -> None:
        """POST ``/editMessageText``. Mutates an earlier message in place
        — used as the "mutable spinner line" while tool calls land.

        Best-effort: Telegram rejects edits that produce identical text
        (``message is not modified``); we treat any non-2xx as a no-op
        so a status renderer that re-fires the same content never breaks
        the turn. HTTP 429 updates a shared back-off so subsequent edits
        / chat-actions silently skip until the window expires.
        """
        if time.time() < self._edit_rate_limit_until:
            return
        body = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        try:
            resp = await self.client.post(
                self._endpoint("editMessageText"), json=body
            )
        except httpx.HTTPError:
            return
        if resp.status_code == 429:
            self._note_retry_after(resp)

    def _note_retry_after(self, resp: httpx.Response) -> None:
        """Extend the shared 429 back-off using ``parameters.retry_after``.

        Falls back to a one-second penalty when the body can't be parsed
        — Telegram always sets the field on a real rate-limit response,
        but the parse is best-effort so a malformed reply never raises.
        """
        retry_after: float = 1.0
        try:
            env = resp.json()
            if isinstance(env, dict):
                params = env.get("parameters")
                if isinstance(params, dict):
                    ra = params.get("retry_after")
                    if isinstance(ra, (int, float)):
                        retry_after = float(ra)
        except Exception:  # noqa: BLE001
            pass
        self._edit_rate_limit_until = time.time() + retry_after

    async def _post_multipart(self, method: str, mp: _Multipart) -> int:
        try:
            resp = await self.client.post(
                self._endpoint(method),
                content=mp.body,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={mp.boundary}"
                },
            )
        except httpx.HTTPError as exc:
            raise SendHttpError(str(exc)) from exc
        return await _parse_envelope(resp)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _parse_envelope(resp: httpx.Response) -> int:
    """Lift the Telegram envelope ``{ok, result: {message_id}}``.

    Returns the ``message_id``; raises :class:`SendError` subclasses
    on transport / API failures. Mirrors Rust ``parse_envelope``.
    """
    text = resp.text
    if resp.status_code >= 400:
        raise SendHttpError(f"{resp.status_code}: {text}")
    try:
        env = resp.json()
    except ValueError as exc:
        raise SendHttpError(str(exc)) from exc
    if not isinstance(env, dict):
        raise SendApiError("response was not a JSON object")
    if not env.get("ok"):
        raise SendApiError(env.get("description") or "")
    result = env.get("result")
    if not isinstance(result, dict) or "message_id" not in result:
        raise SendApiError("response missing result.message_id")
    return int(result["message_id"])
