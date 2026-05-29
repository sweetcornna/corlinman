"""Async dispatchers for the ``persona.*`` builtin tools.

Wire shape matches the established ``dispatch_<tool>(args_json=..., ...) -> str``
contract used by :mod:`corlinman_agent.web` and the subagent fan-out
family. Every dispatcher:

* takes ``args_json`` (raw bytes from ``ToolCallEvent``) plus the in-
  process persona / asset stores (passed through by the agent servicer);
* returns a JSON-encoded result string the reasoning loop feeds back as
  ``ToolResult.content``;
* NEVER raises — every failure path folds into a
  ``{"ok": false, "error": "code", "message": "..."}`` envelope so the
  model's next reasoning round has something coherent to read.

Cross-package note
------------------
``PersonaStore`` + ``PersonaAssetStore`` live in the corlinman-server
package; this module is in corlinman-agent and intentionally does NOT
import them at module scope. Stores are passed in via keyword args
(typed ``Any``) so the persona tools stay decoupled — exactly the same
pattern the subagent ``BlackboardStore`` dispatcher uses for the
gateway-owned sqlite handle.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)


__all__ = [
    "dispatch_persona_attach_asset_from_url",
    "dispatch_persona_create",
    "dispatch_persona_delete",
    "dispatch_persona_get",
    "dispatch_persona_list",
    "dispatch_persona_list_assets",
    "dispatch_persona_update",
]


#: Soft cap on the system_prompt body returned by ``persona_get``. Long
#: bodies still get streamed back through the model context if the agent
#: insists, but the default read path returns a trimmed view + the
#: ``…truncated`` marker so a 10k-char persona body doesn't blow the
#: model's context window on every tool call.
_GET_BODY_CLIP_CHARS: int = 2000

#: Hard cap on the byte size of an image fetched by
#: ``persona_attach_asset_from_url``. Matches the PLAN's "10 MiB" wording
#: which is intentionally higher than the asset store's 8 MiB per-asset
#: cap — we want the asset store to be the one to reject the upload (with
#: its specific ``AssetTooLarge`` envelope) rather than swallowing the
#: rejection silently in the download layer.
_MAX_DOWNLOAD_BYTES: int = 10 * 1024 * 1024

#: HTTP fetch timeout for ``persona_attach_asset_from_url``. Generous —
#: persona refs are often hosted on slow CDNs / Discord attachment URLs
#: and a sub-10s timeout was tripping legitimate uploads in early
#: testing.
_DOWNLOAD_TIMEOUT_SECS: float = 30.0


#: MIME allowlist — kept inline here so a misuse round can return the
#: friendlier ``unsupported_mime`` envelope BEFORE we round-trip the
#: bytes into the asset store (which would also reject but with a
#: slightly noisier message).
_ALLOWED_DOWNLOAD_MIMES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode(args_json: bytes | str) -> dict[str, Any]:
    """Decode ``args_json`` into a dict. Always returns a dict — invalid
    JSON / non-object payloads collapse to ``{}`` so downstream key
    lookups behave consistently."""
    raw: str
    if isinstance(args_json, (bytes, bytearray)):
        try:
            raw = bytes(args_json).decode("utf-8")
        except UnicodeDecodeError:
            return {}
    else:
        raw = args_json or ""
    try:
        obj = json.loads(raw or "{}")
    except (ValueError, json.JSONDecodeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _err(code: str, message: str) -> str:
    """Render a failure envelope in the canonical persona-tool shape."""
    return json.dumps(
        {"ok": False, "error": code, "message": message},
        ensure_ascii=False,
    )


def _ok(payload: dict[str, Any]) -> str:
    """Render a success envelope. ``payload`` is merged into
    ``{"ok": true, **payload}`` so callers can write
    ``return _ok({"persona": ...})`` directly."""
    body: dict[str, Any] = {"ok": True}
    body.update(payload)
    return json.dumps(body, ensure_ascii=False)


def _persona_summary(persona: Any) -> dict[str, Any]:
    """Project a Persona row into the short summary dict used by
    ``persona_list``."""
    return {
        "id": getattr(persona, "id", ""),
        "display_name": getattr(persona, "display_name", ""),
        "short_summary": getattr(persona, "short_summary", "") or "",
        "is_builtin": bool(getattr(persona, "is_builtin", False)),
    }


def _persona_full(persona: Any, *, clip_body: bool = True) -> dict[str, Any]:
    """Project a Persona row into the full dict returned by
    ``persona_get``. ``system_prompt`` is clipped to
    ``_GET_BODY_CLIP_CHARS`` with a ``…truncated`` suffix when
    ``clip_body`` is True (default) and the body exceeds the cap."""
    body = getattr(persona, "system_prompt", "") or ""
    body_truncated = False
    if clip_body and len(body) > _GET_BODY_CLIP_CHARS:
        body = body[:_GET_BODY_CLIP_CHARS] + "…truncated"
        body_truncated = True
    return {
        "id": getattr(persona, "id", ""),
        "display_name": getattr(persona, "display_name", ""),
        "short_summary": getattr(persona, "short_summary", "") or "",
        "system_prompt": body,
        "system_prompt_truncated": body_truncated,
        "is_builtin": bool(getattr(persona, "is_builtin", False)),
        "created_at_ms": int(getattr(persona, "created_at_ms", 0) or 0),
        "updated_at_ms": int(getattr(persona, "updated_at_ms", 0) or 0),
    }


def _asset_summary(record: Any) -> dict[str, Any]:
    """Project an AssetRecord into the short summary dict used by
    ``persona_list_assets`` and the success envelope of
    ``persona_attach_asset_from_url``."""
    return {
        "id": getattr(record, "id", ""),
        "persona_id": getattr(record, "persona_id", ""),
        "kind": getattr(record, "kind", ""),
        "label": getattr(record, "label", ""),
        "file_name": getattr(record, "file_name", ""),
        "mime": getattr(record, "mime", ""),
        "size_bytes": int(getattr(record, "size_bytes", 0) or 0),
        "sha256": getattr(record, "sha256", ""),
        "created_at_ms": int(getattr(record, "created_at_ms", 0) or 0),
    }


def _store_required(store: Any, kind: str) -> str | None:
    """Return a JSON error envelope if ``store`` is not wired, else
    ``None``. Centralises the 503-shaped diagnostic so the model gets
    the same wording across every persona tool when the gateway booted
    without a persona store."""
    if store is None:
        return _err(
            f"{kind}_unavailable",
            f"{kind} is not wired in this deployment",
        )
    return None


# ---------------------------------------------------------------------------
# Read-only dispatchers
# ---------------------------------------------------------------------------


async def dispatch_persona_list(
    *, args_json: bytes | str, persona_store: Any, asset_store: Any = None
) -> str:
    """``persona_list`` — return every persona as a summary list.

    ``asset_store`` is accepted but ignored so the agent_servicer can
    pass both stores uniformly to every persona dispatcher.
    """
    del args_json  # no args
    if (err := _store_required(persona_store, "persona_store")) is not None:
        return err
    try:
        rows = await persona_store.list()
    except Exception as exc:  # noqa: BLE001 - dispatcher must never raise
        logger.exception("persona_list.failed")
        return _err("persona_list_failed", str(exc))
    return _ok({"personas": [_persona_summary(r) for r in rows]})


async def dispatch_persona_get(
    *, args_json: bytes | str, persona_store: Any, asset_store: Any = None
) -> str:
    """``persona_get`` — return the full row (system_prompt clipped)."""
    del asset_store
    if (err := _store_required(persona_store, "persona_store")) is not None:
        return err
    args = _decode(args_json)
    pid = (args.get("id") or "").strip() if isinstance(args.get("id"), str) else ""
    if not pid:
        return _err("invalid_args", "missing or empty 'id' field")
    try:
        row = await persona_store.get(pid)
    except Exception as exc:  # noqa: BLE001
        logger.exception("persona_get.failed", persona_id=pid)
        return _err("persona_get_failed", str(exc))
    if row is None:
        return _err("persona_not_found", f"no persona with id {pid!r}")
    return _ok({"persona": _persona_full(row)})


async def dispatch_persona_list_assets(
    *, args_json: bytes | str, persona_store: Any, asset_store: Any
) -> str:
    """``persona_list_assets`` — list one persona's emoji + ref assets."""
    if (err := _store_required(persona_store, "persona_store")) is not None:
        return err
    if (err := _store_required(asset_store, "persona_asset_store")) is not None:
        return err
    args = _decode(args_json)
    pid = (args.get("id") or "").strip() if isinstance(args.get("id"), str) else ""
    if not pid:
        return _err("invalid_args", "missing or empty 'id' field")
    kind_raw = args.get("kind")
    kind: str | None
    if kind_raw is None:
        kind = None
    elif isinstance(kind_raw, str) and kind_raw in ("emoji", "reference"):
        kind = kind_raw
    else:
        return _err(
            "invalid_args",
            "'kind' must be 'emoji' or 'reference' when provided",
        )
    # 404-fast on missing persona so the model doesn't think an empty
    # bucket means "no assets yet" when in reality the slug is a typo.
    try:
        row = await persona_store.get(pid)
    except Exception as exc:  # noqa: BLE001
        logger.exception("persona_list_assets.persona_lookup_failed", persona_id=pid)
        return _err("persona_get_failed", str(exc))
    if row is None:
        return _err("persona_not_found", f"no persona with id {pid!r}")
    try:
        assets = await asset_store.list(pid, kind=kind) if kind is not None \
            else await asset_store.list(pid)
    except Exception as exc:  # noqa: BLE001
        logger.exception("persona_list_assets.failed", persona_id=pid)
        return _err("persona_list_assets_failed", str(exc))
    return _ok({"assets": [_asset_summary(a) for a in assets]})


# ---------------------------------------------------------------------------
# Mutation dispatchers
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


async def dispatch_persona_create(
    *, args_json: bytes | str, persona_store: Any, asset_store: Any = None
) -> str:
    """``persona_create`` — insert a new persona row."""
    del asset_store
    if (err := _store_required(persona_store, "persona_store")) is not None:
        return err
    # Lazy-import so the corlinman-agent module stays decoupled from
    # the corlinman-server package — same pattern the subagent
    # tool_wrapper uses for its server-side observability hooks.
    try:
        from corlinman_server.persona import (  # noqa: PLC0415
            Persona,
            PersonaError,
            PersonaExists,
            PersonaProtected,
        )
    except ImportError as exc:
        logger.warning("persona_create.import_failed", error=str(exc))
        return _err("persona_store_unavailable", str(exc))

    args = _decode(args_json)
    raw_id = args.get("id")
    pid = raw_id.strip() if isinstance(raw_id, str) else ""
    raw_display = args.get("display_name")
    display = raw_display.strip() if isinstance(raw_display, str) else ""
    raw_summary = args.get("short_summary")
    summary = raw_summary.strip() if isinstance(raw_summary, str) else ""
    raw_prompt = args.get("system_prompt")
    prompt = raw_prompt if isinstance(raw_prompt, str) else ""
    if not pid or not display or not prompt:
        return _err(
            "invalid_args",
            "id, display_name and system_prompt are required",
        )

    now = _now_ms()
    candidate = Persona(
        id=pid,
        display_name=display,
        short_summary=summary,
        system_prompt=prompt,
        is_builtin=False,
        created_at_ms=now,
        updated_at_ms=now,
    )
    try:
        created = await persona_store.create(candidate)
    except PersonaExists:
        return _err(
            "persona_exists",
            f"persona with id {pid!r} already exists",
        )
    except PersonaProtected as exc:
        return _err("persona_protected", str(exc))
    except PersonaError as exc:
        return _err("persona_create_failed", str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("persona_create.failed", persona_id=pid)
        return _err("persona_create_failed", str(exc))
    return _ok({"persona": _persona_full(created, clip_body=False)})


async def dispatch_persona_update(
    *, args_json: bytes | str, persona_store: Any, asset_store: Any = None
) -> str:
    """``persona_update`` — patch an existing persona row."""
    del asset_store
    if (err := _store_required(persona_store, "persona_store")) is not None:
        return err
    try:
        from corlinman_server.persona import (  # noqa: PLC0415
            PersonaError,
            PersonaProtected,
        )
    except ImportError as exc:
        logger.warning("persona_update.import_failed", error=str(exc))
        return _err("persona_store_unavailable", str(exc))

    args = _decode(args_json)
    pid = (args.get("id") or "").strip() if isinstance(args.get("id"), str) else ""
    if not pid:
        return _err("invalid_args", "missing or empty 'id' field")

    def _opt_str(key: str) -> str | None:
        val = args.get(key)
        if val is None:
            return None
        if not isinstance(val, str):
            return None
        return val

    display = _opt_str("display_name")
    summary = _opt_str("short_summary")
    prompt = _opt_str("system_prompt")
    if display is None and summary is None and prompt is None:
        return _err(
            "invalid_args",
            "at least one of display_name, short_summary, system_prompt "
            "must be provided",
        )

    try:
        updated = await persona_store.update(
            pid,
            display_name=display,
            short_summary=summary,
            system_prompt=prompt,
        )
    except PersonaProtected as exc:
        return _err("persona_protected", str(exc))
    except PersonaError as exc:
        # The store raises bare PersonaError on missing row — matches
        # the admin route's 404 path.
        return _err("persona_not_found", str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("persona_update.failed", persona_id=pid)
        return _err("persona_update_failed", str(exc))
    return _ok({"persona": _persona_full(updated, clip_body=False)})


async def dispatch_persona_delete(
    *, args_json: bytes | str, persona_store: Any, asset_store: Any = None
) -> str:
    """``persona_delete`` — remove one persona + its assets."""
    if (err := _store_required(persona_store, "persona_store")) is not None:
        return err
    try:
        from corlinman_server.persona import (  # noqa: PLC0415
            PersonaProtected,
        )
    except ImportError as exc:
        logger.warning("persona_delete.import_failed", error=str(exc))
        return _err("persona_store_unavailable", str(exc))

    args = _decode(args_json)
    pid = (args.get("id") or "").strip() if isinstance(args.get("id"), str) else ""
    if not pid:
        return _err("invalid_args", "missing or empty 'id' field")

    # Best-effort asset cleanup BEFORE row removal so a delete that
    # half-succeeds (asset store down) still nukes the persona row and
    # operators can re-run the cleanup manually. Matches the
    # admin-route /admin/personas DELETE behaviour.
    if asset_store is not None:
        try:
            await asset_store.delete_all(pid)
        except Exception:  # noqa: BLE001 — never block the row delete
            logger.warning("persona_delete.asset_cleanup_failed", persona_id=pid)

    try:
        removed = await persona_store.delete(pid)
    except PersonaProtected as exc:
        return _err("persona_protected", str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("persona_delete.failed", persona_id=pid)
        return _err("persona_delete_failed", str(exc))
    return _ok({"removed": bool(removed), "id": pid})


# ---------------------------------------------------------------------------
# Attach-from-url dispatcher
# ---------------------------------------------------------------------------


async def dispatch_persona_attach_asset_from_url(
    *,
    args_json: bytes | str,
    persona_store: Any,
    asset_store: Any,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """``persona_attach_asset_from_url`` — fetch + store one asset.

    The optional ``transport`` arg is a unit-test seam — production
    callers leave it ``None`` so :mod:`httpx` opens its standard
    network transport.
    """
    if (err := _store_required(persona_store, "persona_store")) is not None:
        return err
    if (err := _store_required(asset_store, "persona_asset_store")) is not None:
        return err
    try:
        from corlinman_server.persona import (  # noqa: PLC0415
            AssetMimeRejected,
            AssetQuotaExceeded,
            AssetTooLarge,
        )
    except ImportError as exc:
        logger.warning(
            "persona_attach_asset_from_url.import_failed", error=str(exc)
        )
        return _err("persona_store_unavailable", str(exc))

    args = _decode(args_json)
    raw_pid = args.get("persona_id")
    pid = raw_pid.strip() if isinstance(raw_pid, str) else ""
    kind = args.get("kind")
    raw_label = args.get("label")
    label = raw_label.strip() if isinstance(raw_label, str) else ""
    raw_url = args.get("url")
    url = raw_url.strip() if isinstance(raw_url, str) else ""
    file_name_raw = args.get("file_name")
    file_name = (
        file_name_raw.strip()
        if isinstance(file_name_raw, str) and file_name_raw.strip()
        else None
    )
    if not pid or not label or not url or kind not in ("emoji", "reference"):
        return _err(
            "invalid_args",
            "persona_id, kind (emoji|reference), label and url are required",
        )
    if not (url.startswith("http://") or url.startswith("https://")):
        return _err(
            "invalid_args",
            "'url' must be an absolute http(s) URL",
        )

    # 404-fast on missing persona so the download isn't wasted.
    try:
        row = await persona_store.get(pid)
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "persona.attach_asset.persona_lookup_failed", persona_id=pid
        )
        return _err("persona_get_failed", str(exc))
    if row is None:
        return _err("persona_not_found", f"no persona with id {pid!r}")

    if file_name is None:
        parsed_path = urllib.parse.urlparse(url).path
        candidate_name = parsed_path.rsplit("/", 1)[-1] if parsed_path else ""
        file_name = candidate_name or f"{label}.bin"

    # Stream the body so an oversized response is bounded — same shape
    # web_fetch uses for its body cap.
    try:
        client_kwargs: dict[str, Any] = {
            "timeout": _DOWNLOAD_TIMEOUT_SECS,
            "follow_redirects": True,
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        async with httpx.AsyncClient(**client_kwargs) as client:
            async with client.stream("GET", url) as response:
                if response.status_code >= 400:
                    return _err(
                        "download_failed",
                        f"http_status: server returned {response.status_code}",
                    )
                content_type = (
                    response.headers.get("content-type") or ""
                ).split(";", 1)[0].strip().lower()
                if (
                    content_type
                    and content_type not in _ALLOWED_DOWNLOAD_MIMES
                ):
                    return _err(
                        "unsupported_mime",
                        f"received {content_type!r}; allowed: "
                        f"{', '.join(sorted(_ALLOWED_DOWNLOAD_MIMES))}",
                    )
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > _MAX_DOWNLOAD_BYTES:
                        return _err(
                            "download_too_large",
                            f"download exceeded {_MAX_DOWNLOAD_BYTES} bytes",
                        )
                body_bytes = b"".join(chunks)
    except httpx.TimeoutException as exc:
        return _err("download_timeout", str(exc))
    except httpx.HTTPError as exc:
        return _err("download_failed", str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("persona.attach_asset.download_unexpected", url=url)
        return _err("download_failed", str(exc))

    # If the response carried no content-type header we fall through
    # to the asset store's MIME validator — it'll reject if the bytes
    # aren't one of the four allowed shapes.
    mime = content_type or "application/octet-stream"

    try:
        record = await asset_store.put(
            pid,
            kind,
            label,
            bytes_=body_bytes,
            mime=mime,
            file_name=file_name,
        )
    except AssetMimeRejected as exc:
        return _err("unsupported_mime", str(exc))
    except AssetTooLarge as exc:
        return _err("asset_too_large", str(exc))
    except AssetQuotaExceeded as exc:
        return _err("quota_exceeded", str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "persona.attach_asset.put_failed",
            persona_id=pid,
            kind=kind,
            label=label,
        )
        return _err("asset_store_failed", str(exc))
    return _ok({"asset": _asset_summary(record)})
