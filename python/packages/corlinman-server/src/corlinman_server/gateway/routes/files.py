"""``POST /v1/files`` + ``GET /v1/files/{file_id}`` — web-chat file store.

New gateway infrastructure for the enterprise-parity chat界面
(``docs/PLAN_CHAT_PERFECT.md`` §4 decision 1 — Wave 2). It unblocks
two downstream waves:

* **P1 (user attachments)** — the composer uploads a picked / dragged
  file, gets back a stable ``{file_id, url, …}``, and embeds the
  ``url`` as an OpenAI content-part so the assistant can see it and the
  history can render it after a refresh.
* **assistant media / attachment download** — tool products
  (``image_generate`` output, ``send_attachment`` blobs) register here
  and surface a browser-fetchable ``/v1/files/{id}`` URL.

This module is the storage primitive only; the parts-conversion and
journal-persistence layers land in the later waves (W3/W4). It is
deliberately self-contained — it owns no boot wiring — so it can ship
as an independent backend slice that does not collide with the W1
stream-contract work in :mod:`~corlinman_server.gateway.routes.chat`.

Storage layout (sidecar JSON metadata + filesystem blob)
--------------------------------------------------------
Files live under ``<data_dir>/files/``. Each upload writes two
files keyed by the same opaque ``file_id``::

    <data_dir>/files/<file_id>.blob   # raw bytes, exactly as received
    <data_dir>/files/<file_id>.json   # {name, mime, size, created_at_ms}

The sidecar-JSON shape mirrors the rest of the gateway's
``<data_dir>``-local persistence (``status_epochs.json``,
``public_origin``, the OAuth token blobs) rather than standing up a
second sqlite store: there is no boot-wired singleton to hang a
connection off (the route resolves the data dir lazily per request,
exactly like :func:`status._data_dir`), and a per-file sidecar keeps
the write path crash-safe (blob first, then metadata) without a
schema migration. ``created_at_ms`` is recorded for the eventual
retention sweep but is not consulted on the read path.

Auth
----
Both endpoints sit under the ``/v1/`` prefix, so the existing
:class:`~corlinman_server.gateway.middleware.auth.ApiKeyAuthMiddleware`
already gates them behind a tenant bearer key. The in-app chat UI
authenticates with the admin-session cookie (no API key), so
``/v1/files`` is added to
:data:`~corlinman_server.gateway.middleware.auth.ADMIN_SESSION_BRIDGE_PREFIXES`
alongside ``/v1/chat`` — the same bridge, extended, never weakened.
This module therefore needs **no** per-route auth code: by the time a
handler runs the request is already authenticated.

See :func:`router` for the FastAPI ``APIRouter`` factory.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse

_log = logging.getLogger(__name__)

__all__ = ["load_stored_file", "router"]


# ─── Caps + id format ────────────────────────────────────────────────


#: Hard cap on a single uploaded file. 25 MiB comfortably covers the
#: high-resolution screenshots / short documents a chat user pastes
#: while keeping a single request from pinning memory (the upload is
#: read fully into RAM before persistence). Override for an operator
#: that needs bigger attachments with ``CORLINMAN_FILES_MAX_BYTES``.
DEFAULT_MAX_BYTES: int = 25 * 1024 * 1024

#: Env override for the per-file cap. Mirrors the persona asset store's
#: ``CORLINMAN_PERSONA_MAX_ASSET_BYTES`` knob.
_MAX_BYTES_ENV: str = "CORLINMAN_FILES_MAX_BYTES"

#: ``file_id`` is 26 lowercase hex chars (same shape as the persona
#: asset store's :func:`asset_store._ulid`). The strict ``[0-9a-f]``
#: class is also the path-traversal guard: a value matching this regex
#: structurally cannot contain ``/``, ``\\``, ``.`` or ``..`` so it can
#: never escape ``<data_dir>/files/`` when joined onto the base dir.
_FILE_ID_RE: re.Pattern[str] = re.compile(r"^[0-9a-f]{26}$")

#: MIME types served ``inline`` rather than as a forced download. Raster
#: images are safe to render directly in an ``<img>`` / lightbox;
#: everything else is sent ``attachment`` so a browser never executes an
#: uploaded blob (e.g. an HTML payload) in the gateway's origin.
_INLINE_MIME_PREFIXES: tuple[str, ...] = ("image/",)

#: Exceptions to the inline rule: SVG is ``image/*`` but is a script
#: container (inline ``<script>``, event handlers) — rendering one
#: inline from the gateway origin is stored XSS with an admin cookie in
#: scope. Always force-download these.
_FORCE_ATTACHMENT_MIMES: frozenset[str] = frozenset({"image/svg+xml"})

#: Fallback MIME when the client sends none / an empty content type. The
#: generic binary type makes the serve path default to ``attachment``.
_DEFAULT_MIME: str = "application/octet-stream"


# ─── Helpers ─────────────────────────────────────────────────────────


def _now_ms() -> int:
    """Wall-clock millis since the UNIX epoch."""
    return int(time.time() * 1000)


def _new_file_id() -> str:
    """Fresh opaque file id — 26 lowercase hex chars.

    Same shape as the persona asset store's ``_ulid`` (uuid4 hex
    truncated to 26): not a real ULID, but lex-sortable-enough and,
    crucially, matching :data:`_FILE_ID_RE` so the value is safe to
    interpolate into a filesystem path without further sanitisation.
    """
    return uuid.uuid4().hex[:26]


def _max_bytes() -> int:
    """Per-file byte cap, allowing an operator env override.

    Falls back to :data:`DEFAULT_MAX_BYTES` when the env var is unset or
    unparseable (same defensive parse the persona caps use)."""
    raw = os.environ.get(_MAX_BYTES_ENV)
    if not raw:
        return DEFAULT_MAX_BYTES
    try:
        val = int(raw)
    except ValueError:
        return DEFAULT_MAX_BYTES
    return val if val > 0 else DEFAULT_MAX_BYTES


def _data_dir() -> Path | None:
    """Resolve the gateway data dir, or ``None`` when none exists.

    Mirrors :func:`status._data_dir` verbatim so every ``<data_dir>``
    consumer agrees on one resolution order: the ``CORLINMAN_DATA_DIR``
    env override first, then ``~/.corlinman`` iff it already exists. The
    upload path creates ``<data_dir>/files/`` on demand; the read path
    treats a missing dir as a 404 (the file genuinely isn't there)."""
    raw = os.environ.get("CORLINMAN_DATA_DIR")
    if raw:
        return Path(raw)
    home = Path.home() / ".corlinman"
    return home if home.exists() else None


def _files_dir() -> Path | None:
    """``<data_dir>/files`` — the blob + sidecar root. ``None`` if no
    data dir is resolvable (degraded boot)."""
    base = _data_dir()
    return None if base is None else base / "files"


def _blob_path(files_dir: Path, file_id: str) -> Path:
    """On-disk path to a file's raw bytes."""
    return files_dir / f"{file_id}.blob"


def _meta_path(files_dir: Path, file_id: str) -> Path:
    """On-disk path to a file's sidecar JSON metadata."""
    return files_dir / f"{file_id}.json"


def _read_meta(path: Path) -> dict[str, Any] | None:
    """Load a sidecar JSON metadata file, or ``None`` on any error.

    A corrupt / partially-written sidecar reads as ``None`` (treated by
    the caller as 404) rather than 500ing the serve path — a re-upload
    heals it."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _normalise_mime(raw: str | None) -> str:
    """Strip charset / parameter suffixes off an uploaded content type.

    Multipart clients can send ``image/jpeg; charset=binary`` (the same
    quirk the persona upload route guards). We keep only the bare
    ``type/subtype`` and fall back to the generic binary type when the
    client sent nothing usable."""
    mime = (raw or "").split(";", 1)[0].strip().lower()
    return mime or _DEFAULT_MIME


def _is_inline(mime: str) -> bool:
    """Whether ``mime`` may be served ``inline`` (raster images) vs forced
    as an ``attachment`` download (everything else, incl. SVG)."""
    if mime in _FORCE_ATTACHMENT_MIMES:
        return False
    return mime.startswith(_INLINE_MIME_PREFIXES)


def _content_disposition(mime: str, file_name: str) -> str:
    """Build the ``Content-Disposition`` header value.

    Images render ``inline`` (chat bubble / lightbox); every other type
    is ``attachment`` so the browser downloads rather than executes it.
    The filename is emitted with both the plain ``filename=`` token and
    the RFC 5987 ``filename*=UTF-8''…`` form so non-ASCII names (CJK
    screenshots) survive the round-trip."""
    disposition = "inline" if _is_inline(mime) else "attachment"
    # Plain token: keep only filesystem-safe ASCII so a quote / control
    # char in the name can't break out of the header value. The
    # ``filename*`` form below carries the faithful UTF-8 name.
    ascii_name = re.sub(r'[^A-Za-z0-9._-]', "_", file_name) or "file"
    quoted_utf8 = _rfc5987_quote(file_name)
    return (
        f"{disposition}; filename=\"{ascii_name}\"; "
        f"filename*=UTF-8''{quoted_utf8}"
    )


def _rfc5987_quote(value: str) -> str:
    """Percent-encode ``value`` per RFC 5987 for ``filename*=UTF-8''…``.

    Only the RFC's ``attr-char`` set is left literal; everything else
    (spaces, CJK, quotes) is percent-encoded from its UTF-8 bytes."""
    attr_chars = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
        "!#$&+-.^_`|~"
    )
    out: list[str] = []
    for byte in value.encode("utf-8"):
        ch = chr(byte)
        if ch in attr_chars:
            out.append(ch)
        else:
            out.append(f"%{byte:02X}")
    return "".join(out)


def load_stored_file(file_id: str) -> tuple[bytes, str, str] | None:
    """Load a stored upload: ``(bytes, mime, name)``, or ``None``.

    Shared with :mod:`corlinman_server.gateway.routes.chat` so chat
    requests that reference an upload as ``/v1/files/{id}`` (or a bare
    ``file_id``) can inline the actual bytes for the model provider —
    providers cannot fetch gateway-private URLs. The same strict-hex id
    validation as the serve route applies; any miss is ``None``.
    """
    if not _FILE_ID_RE.match(file_id):
        return None
    files_dir = _files_dir()
    if files_dir is None:
        return None
    meta = _read_meta(_meta_path(files_dir, file_id))
    blob_path = _blob_path(files_dir, file_id)
    if meta is None or not blob_path.is_file():
        return None
    try:
        blob = blob_path.read_bytes()
    except OSError:
        return None
    mime = _normalise_mime(meta.get("mime") if isinstance(meta, dict) else None)
    name = str(meta.get("name") or f"{file_id}.bin")
    return blob, mime, name


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    """Error body in the gateway's ``{"error": {...}}`` envelope shape
    (matches :func:`chat._error_response`)."""
    return JSONResponse(
        {"error": {"code": code, "message": message}},
        status_code=status_code,
    )


# ─── Router ──────────────────────────────────────────────────────────


def router() -> APIRouter:
    """Build the ``/v1/files`` sub-router.

    Stateless: the route resolves its storage dir lazily from
    ``<data_dir>`` per request (no boot wiring / ``GatewayState`` slot),
    so it is always safe to mount. Auth is handled upstream by
    :class:`ApiKeyAuthMiddleware` (bearer key) + the admin-session
    bridge (in-app chat cookie) — see the module docstring.
    """
    api = APIRouter(tags=["files"])

    @api.post(
        "/v1/files",
        response_model=None,
        summary="Upload one file for the web chat (multipart/form-data)",
    )
    async def upload_file(
        file: Annotated[UploadFile, File()],
    ) -> JSONResponse:
        files_dir = _files_dir()
        if files_dir is None:
            # No resolvable data dir (degraded boot) — nowhere to persist.
            _log.warning("files upload rejected: no data dir resolvable")
            return _error(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "storage_unavailable",
                "file storage is not configured",
            )

        body = await file.read()
        if not body:
            # Reject empty uploads — a zero-byte file carries no content
            # and would render as a broken attachment downstream.
            return _error(
                status.HTTP_400_BAD_REQUEST,
                "empty_file",
                "uploaded file is empty",
            )
        cap = _max_bytes()
        if len(body) > cap:
            return _error(
                status.HTTP_413_CONTENT_TOO_LARGE,
                "file_too_large",
                f"file is {len(body)} bytes; cap is {cap}",
            )

        file_id = _new_file_id()
        mime = _normalise_mime(file.content_type)
        # Cap the stored name length the same way the persona upload does
        # so a hostile multipart part-name can't bloat the sidecar.
        file_name = (file.filename or f"{file_id}.bin")[:255]
        size = len(body)

        files_dir.mkdir(parents=True, exist_ok=True)
        blob = _blob_path(files_dir, file_id)
        meta = _meta_path(files_dir, file_id)
        # Blob FIRST so a sidecar never points at missing bytes (same
        # write ordering the persona asset store uses).
        try:
            blob.write_bytes(body)
            meta.write_text(
                json.dumps(
                    {
                        "name": file_name,
                        "mime": mime,
                        "size": size,
                        "created_at_ms": _now_ms(),
                    }
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            _log.warning("files upload write failed: %s", exc)
            # Best-effort cleanup so a half-written blob doesn't linger.
            blob.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
            return _error(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                "write_failed",
                "failed to persist uploaded file",
            )

        return JSONResponse(
            {
                "file_id": file_id,
                "url": f"/v1/files/{file_id}",
                "name": file_name,
                "mime": mime,
                "size": size,
            },
            status_code=status.HTTP_201_CREATED,
        )

    @api.get(
        "/v1/files/{file_id}",
        response_model=None,
        summary="Serve one uploaded file (image inline, else attachment)",
    )
    async def serve_file(file_id: str) -> FileResponse:
        # Validate the id BEFORE touching the filesystem: the strict hex
        # regex is the path-traversal guard (a value with ``/`` / ``..``
        # can't match), so a malformed id is a flat 404 — never a probe
        # into the data dir.
        if not _FILE_ID_RE.match(file_id):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "file_not_found", "id": file_id},
            )

        files_dir = _files_dir()
        if files_dir is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "file_not_found", "id": file_id},
            )

        meta = _read_meta(_meta_path(files_dir, file_id))
        blob = _blob_path(files_dir, file_id)
        if meta is None or not blob.is_file():
            # Unknown id, corrupt sidecar, or a sidecar that outlived its
            # blob (manual ``rm``) — all 404, all healed by a re-upload.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "file_not_found", "id": file_id},
            )

        mime = _normalise_mime(meta.get("mime") if isinstance(meta, dict) else None)
        file_name = str(meta.get("name") or f"{file_id}.bin")
        return FileResponse(
            blob,
            media_type=mime,
            headers={
                "Content-Disposition": _content_disposition(mime, file_name),
                # Uploaded content is immutable per id, so a private
                # long cache is safe (private: it may be tenant content).
                "Cache-Control": "private, max-age=86400, immutable",
            },
        )

    return api
