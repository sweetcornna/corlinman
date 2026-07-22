"""``qzone_publish`` — drive the QQ空间 ``emotion_cgi_publish_v6`` flow.

Port of ``hermes-agent/tools/qzone_tool.py`` adapted to the corlinman
shape:

* Async :class:`httpx.AsyncClient` instead of blocking ``urllib.request``.
* OneBot credentials sourced via :class:`OneBotClient` rather than the
  old shared ``_onebot_call`` helper.
* ``generate`` argument can be a nested ``image_with_refs`` args dict;
  the dispatcher calls :func:`dispatch_image_with_refs` first and
  prepends the generated path to ``images``.
* Workspace path resolution mirrors ``send_attachment``: relative
  paths resolve against ``<DATA_DIR>/workspace`` so callers can pass
  the same path they used with ``write_file`` / ``image_with_refs``.

QZone wire format
-----------------
QQ has no official open API for publishing 说说 — this drives the
reverse-engineered web endpoints. ``richval`` is one comma-delimited
segment per image, segments joined by TAB. If Tencent changes the
wire format, :func:`_build_richval` is the single place to fix.

Return envelope
---------------
Success: ``{"ok": true, "tid": "...", "qzone_url": "https://...",
"uin": "...", "images": <count>, "generated": <bool>}``.

Failure: ``{"ok": false, "error": "<code>", "message": "...",
"qzone_url": null}``. The dispatcher never raises — every failure
path folds into this shape so the model gets one clean string.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
import structlog
from corlinman_content_policy import (
    TencentPolicyConfig,
    classifier_failure_decision,
    moderate_media,
    moderate_text,
)

from corlinman_agent.onebot import OneBotClient, OneBotError

logger = structlog.get_logger(__name__)


__all__ = [
    "QZONE_PUBLISH_TOOL",
    "QZoneError",
    "dispatch_qzone_publish",
    "qzone_publish_tool_schema",
]


#: Wire-stable tool name. Imported by the agent servicer's
#: ``BUILTIN_TOOLS`` set + the ``_dispatch_builtin`` switch.
QZONE_PUBLISH_TOOL: str = "qzone_publish"


# QZone web endpoints — fixed constants, never built from user input.
_QZONE_PUBLISH_URL: str = (
    "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com"
    "/cgi-bin/emotion_cgi_publish_v6"
)
_QZONE_UPLOAD_URL: str = (
    "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
)

#: QZone cookie domain to request from OneBot — NapCat / Lagrange return
#: the *.qq.com cookie jar (uin / skey / p_skey / ...) for this domain.
_QZONE_COOKIE_DOMAIN: str = "user.qzone.qq.com"

#: Desktop UA — QZone serves a different (mobile) flow to mobile UAs.
_DESKTOP_UA: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

#: QZone 说说 limits.
_IMAGE_EXTS: frozenset[str] = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})
_MAX_IMAGES: int = 9
_MAX_IMAGE_BYTES: int = 20 * 1024 * 1024  # 20 MiB

#: Per-request timeouts.
_QZONE_TIMEOUT: float = 20.0
_QZONE_UPLOAD_TIMEOUT: float = 60.0


class QZoneError(RuntimeError):
    """Raised on any failure of the QZone primitives.

    The dispatcher catches and folds these into the JSON envelope so
    the model gets a clean error code + human message.
    """


# ---------------------------------------------------------------------------
# Tool schema
# ---------------------------------------------------------------------------


def qzone_publish_tool_schema() -> dict[str, Any]:
    """OpenAI-shaped tool descriptor for ``qzone_publish``."""
    return {
        "type": "function",
        "function": {
            "name": QZONE_PUBLISH_TOOL,
            "description": (
                "Publish a 说说 (status update) to the bound QQ "
                "account's QQ空间 (QZone). Supports text, attached "
                "local image paths, and/or an AI-generated image via "
                "the `image_with_refs` tool (pass its args under "
                "`generate`). The QQ login state is borrowed from the "
                "running NapCat instance — no QQ password is needed. "
                "Returns `{tid, qzone_url}` on success. Note: drives "
                "unofficial QZone web endpoints, so it can fail if the "
                "login state is stale or Tencent risk-control fires."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": (
                            "The 说说 body text. May be empty when "
                            "'images' or 'generate' is provided; "
                            "otherwise required."
                        ),
                    },
                    "images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional list of image file paths to "
                            "attach (max 9). Relative paths resolve "
                            "against the agent workspace — pass the "
                            "SAME path you used with `write_file` / "
                            "`image_with_refs`."
                        ),
                    },
                    "generate": {
                        "type": "object",
                        "description": (
                            "Optional `image_with_refs` args "
                            "({prompt, characters, aspect_ratio?, "
                            "persona_id?}). When set, the tool runs "
                            "`image_with_refs` first and prepends the "
                            "generated image to `images`."
                        ),
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
        },
    }


# ---------------------------------------------------------------------------
# Envelope + arg helpers
# ---------------------------------------------------------------------------


def _err(error: str, message: str, **extra: Any) -> str:
    """Render a failure envelope in the canonical shape.

    ``error`` is the wire-stable failure *code* (e.g. ``"invalid_args"``
    / ``"qzone_rejected"``). The parameter is named ``error`` (not
    ``code``) so callers can attach a numeric ``code=...`` field via
    ``**extra`` without clobbering the positional name.
    """
    payload: dict[str, Any] = {"ok": False, "error": error, "message": message}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _decode(args_json: bytes | str) -> dict[str, Any]:
    """Decode the ``ToolCallEvent.args_json`` payload to a dict."""
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


def _resolve_workspace_root() -> Path:
    """Resolve the agent workspace root used by ``send_attachment``.

    Kept in lockstep with
    :func:`corlinman_channels._status._agent_workspace_root` so a path
    returned by ``image_with_refs`` / ``write_file`` lands on the same
    filesystem location both lookups walk.
    """
    env_ws = os.environ.get("CORLINMAN_AGENT_WORKSPACE")
    if env_ws:
        root = Path(env_ws)
    else:
        data_dir = os.environ.get("CORLINMAN_DATA_DIR")
        base = Path(data_dir) if data_dir else Path.home() / ".corlinman"
        root = base / "workspace"
    return root.resolve()


def _resolve_image_path(path_str: str) -> Path | None:
    """Resolve an image path the same way ``send_attachment`` does.

    Order: absolute existing > workspace + relative > workspace +
    basename. Returns ``None`` when no candidate exists.
    """
    if not path_str or not path_str.strip():
        return None
    workspace = _resolve_workspace_root()
    raw = Path(path_str)
    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
        candidates.append(workspace / raw.name)
    else:
        candidates.append((workspace / raw).resolve())
        if raw.name and raw.name != path_str:
            candidates.append(workspace / raw.name)
    for cand in candidates:
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None


def _read_image_file(path: Path) -> tuple[bytes, str]:
    """Read a local image file, returning ``(bytes, basename)``.

    Raises :class:`QZoneError` with a human-readable reason for any
    problem so the dispatcher can fail fast before touching the network.
    """
    ext = path.suffix.lower()
    if ext not in _IMAGE_EXTS:
        raise QZoneError(
            f"unsupported image type {ext!r} (allowed: "
            f"{sorted(_IMAGE_EXTS)})"
        )
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise QZoneError(f"could not stat image: {exc}") from exc
    if size == 0:
        raise QZoneError("file is empty")
    if size > _MAX_IMAGE_BYTES:
        raise QZoneError(
            f"image too large ({size} bytes; max {_MAX_IMAGE_BYTES})"
        )
    try:
        return path.read_bytes(), path.name
    except OSError as exc:
        raise QZoneError(f"could not read image: {exc}") from exc


# ---------------------------------------------------------------------------
# QZone primitives (pure, unit-testable)
# ---------------------------------------------------------------------------


def _compute_gtk(p_skey: str) -> int:
    """Compute the QZone ``g_tk`` CSRF token from the ``p_skey`` cookie.

    The long-standing QZone DJB-style hash. Identical to the hermes
    implementation — verified bit-for-bit against the live endpoint.
    """
    h = 5381
    for ch in p_skey:
        h += (h << 5) + ord(ch)
    return h & 0x7FFFFFFF


def _extract_cookie_value(cookie_str: str, key: str) -> str | None:
    """Return the value of ``key`` from a ``k=v; k2=v2`` cookie string."""
    for part in cookie_str.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name == key:
            return value
    return None


def _as_text(raw: bytes | str) -> str:
    """Decode a response body to text."""
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode("utf-8", errors="replace").strip()
    return (raw or "").strip()


def _extract_json_object(raw: bytes | str) -> dict[str, Any] | None:
    """Locate and parse the first ``{...}`` JSON object in ``raw``.

    QZone wraps payloads in JSONP-style shims (``_Callback({...})``,
    ``frameElement.callback({...})``). Finds the JSON regardless.
    """
    text = _as_text(raw)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _extract_pic_info(data: dict[str, Any]) -> dict[str, Any]:
    """Pull the fields needed for ``richval`` out of an upload response.

    QZone has shipped several response shapes; values are fetched
    leniently with fallbacks so a missing optional field degrades
    gracefully rather than raising.
    """
    return {
        "albumid": data.get("albumid", ""),
        "lloc": data.get("lloc") or data.get("photoid", ""),
        "sloc": data.get("sloc") or data.get("photoid", ""),
        "type": data.get("type", 0),
        "width": data.get("width", 0),
        "height": data.get("height", 0),
        "url": data.get("url") or data.get("pre", ""),
    }


def _build_richval(pic_infos: list[dict[str, Any]]) -> str:
    """Build the ``richval`` string for an image 说说.

    Reverse-engineered wire format: one comma-delimited segment per
    image, segments joined by a TAB. If Tencent changes the format,
    this is the single place to fix.
    """
    segments = []
    for pic in pic_infos:
        segments.append(
            ",{albumid},{lloc},{sloc},{type},{height},{width},,{height},{width}".format(
                albumid=pic.get("albumid", ""),
                lloc=pic.get("lloc", ""),
                sloc=pic.get("sloc", ""),
                type=pic.get("type", 0),
                height=pic.get("height", 0),
                width=pic.get("width", 0),
            )
        )
    return "\t".join(segments)


def _build_publish_form(
    text: str, uin: str, pic_infos: list[dict[str, Any]] | None = None
) -> dict[str, str]:
    """Build the form body for the QZone emotion_publish endpoint."""
    form = {
        "syn_tweet_verson": "1",
        "paramstr": "1",
        "pic_template": "",
        "richtype": "",
        "richval": "",
        "special_url": "",
        "subrichtype": "",
        "who": "1",
        "con": text,
        "feedversion": "1",
        "ver": "1",
        "ugc_right": "1",
        "to_sign": "0",
        "hostuin": str(uin),
        "code_version": "1",
        "format": "json",
        "qzreferrer": f"https://user.qzone.qq.com/{uin}",
    }
    if pic_infos:
        form["richtype"] = "1"
        form["richval"] = _build_richval(pic_infos)
    return form


def _build_upload_form(
    image_b64: str,
    filename: str,
    uin: str,
    skey: str,
    p_skey: str,
    gtk: int,
) -> dict[str, str]:
    """Build the form body for the QZone cgi_upload_image endpoint."""
    return {
        "filename": filename,
        "uploadtype": "1",
        "albumtype": "7",
        "exttype": "0",
        "refer": "shuoshuo",
        "output_type": "json",
        "charset": "utf-8",
        "output_charset": "utf-8",
        "upload_hd": "1",
        "hd_width": "2048",
        "hd_height": "10000",
        "hd_quality": "96",
        "backUrls": (
            "http://upbak.photo.qzone.qq.com/cgi-bin/upload/cgi_upload_image,"
            "http://119.147.64.75/cgi-bin/upload/cgi_upload_image"
        ),
        "url": f"{_QZONE_UPLOAD_URL}?g_tk={gtk}",
        "base64": "1",
        "zzpaneluin": str(uin),
        "p_uin": str(uin),
        "uin": str(uin),
        "skey": skey,
        "p_skey": p_skey,
        "qzonetoken": "",
        "picfile": image_b64,
    }


def _parse_upload_response(raw: bytes | str) -> dict[str, Any]:
    """Parse the QZone cgi_upload_image response.

    Returns ``{"ok": True, "pic": {...}}`` on success or
    ``{"ok": False, "error": ...}`` otherwise. The response is wrapped
    in a ``frameElement.callback(...)`` JSONP shim.
    """
    obj = _extract_json_object(raw)
    if obj is None:
        return {"ok": False, "error": "unparseable upload response"}
    ret = obj.get("ret")
    if ret != 0:
        return {"ok": False, "code": ret, "error": f"ret={ret}"}
    data = obj.get("data") or {}
    return {"ok": True, "pic": _extract_pic_info(data)}


def _parse_publish_response(raw: bytes | str) -> dict[str, Any]:
    """Parse the QZone emotion_publish response.

    Returns ``{"ok": True, "tid": ...}`` on success or
    ``{"ok": False, "error": ..., "code": ...}`` otherwise. QZone wraps
    the JSON body in a ``_Callback(...)`` shim in some flows.

    ``emotion_cgi_publish_v6`` reports success two ways: ``{"ret":0,
    "tid":...}`` (classic) and ``{"code":0,"tid":...}`` (newer — verified
    against live NapCat). Either zero status, with a non-error
    ``subcode``, counts as success.
    """
    obj = _extract_json_object(raw)
    if obj is None:
        return {"ok": False, "error": "unparseable QZone response"}

    ret = obj.get("ret")
    code = obj.get("code")
    subcode = obj.get("subcode", 0)
    status = ret if ret is not None else code
    if status == 0 and subcode in (0, None):
        return {
            "ok": True,
            "tid": obj.get("tid") or obj.get("t1_tid"),
            "raw": obj,
        }
    err = f"ret={ret}, code={code}, subcode={subcode}"
    return {"ok": False, "code": status, "error": err}


# ---------------------------------------------------------------------------
# QZone HTTP requests (async)
# ---------------------------------------------------------------------------


async def _qzone_post(
    client: httpx.AsyncClient,
    url: str,
    form: dict[str, str],
    cookie: str,
    uin: str,
    timeout: float,
) -> bytes:
    """POST a form-urlencoded body to a QZone endpoint and return the body."""
    headers = {
        "Cookie": cookie,
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": f"https://user.qzone.qq.com/{uin}",
        "User-Agent": _DESKTOP_UA,
    }
    try:
        response = await client.post(
            url, data=form, headers=headers, timeout=timeout
        )
    except httpx.TimeoutException as exc:
        raise QZoneError(f"QZone request timed out: {exc}") from exc
    except httpx.HTTPError as exc:
        raise QZoneError(f"QZone request transport error: {exc}") from exc
    if response.status_code >= 400:
        raise QZoneError(f"QZone HTTP {response.status_code}")
    return response.content


async def _upload_one_image(
    client: httpx.AsyncClient,
    image_bytes: bytes,
    filename: str,
    uin: str,
    skey: str,
    p_skey: str,
    gtk: int,
    cookie: str,
) -> dict[str, Any]:
    """Upload one image to QZone and return its parsed pic info.

    Raises :class:`QZoneError` if QZone rejects the upload.
    """
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    form = _build_upload_form(image_b64, filename, uin, skey, p_skey, gtk)
    url = f"{_QZONE_UPLOAD_URL}?g_tk={gtk}"
    raw = await _qzone_post(client, url, form, cookie, uin, _QZONE_UPLOAD_TIMEOUT)
    result = _parse_upload_response(raw)
    if not result.get("ok"):
        raise QZoneError(str(result.get("error", "unknown upload error")))
    pic = result["pic"]
    # _parse_upload_response sets "pic" to a dict on the ok path, but the
    # value is typed Any (parsed from untyped JSON) — narrow before return.
    return pic if isinstance(pic, dict) else {}


async def _qzone_publish_post(
    client: httpx.AsyncClient,
    form: dict[str, str],
    gtk: int,
    cookie: str,
    uin: str,
) -> bytes:
    """POST a 说说 to QZone and return the raw response body."""
    url = f"{_QZONE_PUBLISH_URL}?g_tk={gtk}"
    return await _qzone_post(client, url, form, cookie, uin, _QZONE_TIMEOUT)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _policy_config(resolver: Any | None) -> TencentPolicyConfig:
    try:
        # Every agent entrypoint is protected by default. Only an injected
        # resolver returning the literal boolean false may opt out.
        enabled = True if resolver is None else resolver()
    except Exception:
        enabled = True
    return TencentPolicyConfig(enabled=enabled is not False)


def _policy_error(decision: Any) -> str:
    return _err(
        "content_policy_blocked",
        "Tencent content policy blocked this QZone operation.",
        category_codes=list(decision.category_codes),
        rule_ids=list(decision.rule_ids),
        ruleset_version=decision.ruleset_version,
    )


async def _prepare_effect(
    store: Any | None,
    context: dict[str, str] | None,
    *,
    effect_kind: str,
    effect_target: str,
) -> tuple[Any | None, str | None]:
    if store is None and not context:
        return None, None
    if store is None or not context:
        return None, "scheduler_effect_store_unavailable"
    required = ("source_system", "source_job_id", "occurrence_key")
    if any(not context.get(key) for key in required):
        return None, "scheduler_effect_context_invalid"
    try:
        return (
            await store.prepare_effect(
                source_system=context["source_system"],
                source_job_id=context["source_job_id"],
                occurrence_key=context["occurrence_key"],
                effect_kind=effect_kind,
                effect_target=effect_target,
            ),
            None,
        )
    except Exception:
        return None, "scheduler_effect_reservation_blocked"


async def _complete_effect(
    store: Any,
    effect: Any,
    *,
    state: str,
    receipt: object = None,
    error_code: str | None = None,
) -> bool:
    try:
        await store.complete_effect(
            effect.id,
            state=state,
            receipt=receipt,
            error_code=error_code,
        )
    except Exception:
        return False
    return True


def _qzone_url(uin: str, tid: str | None) -> str | None:
    """Build the user-facing 说说 permalink. Returns ``None`` when the
    tid is missing — the JSON envelope keeps the field but null so the
    model knows the post landed but the URL couldn't be resolved."""
    if not tid:
        return None
    return f"https://user.qzone.qq.com/{uin}/mood/{tid}"


async def dispatch_qzone_publish(
    *,
    args_json: bytes | str,
    onebot_client: OneBotClient | None = None,
    onebot_client_factory: Any | None = None,
    image_with_refs_dispatcher: Any | None = None,
    image_with_refs_kwargs: dict[str, Any] | None = None,
    http_transport: httpx.BaseTransport | None = None,
    policy_resolver: Any | None = None,
    execution_mode: str = "live",
    scheduler_store: Any | None = None,
    effect_context: dict[str, str] | None = None,
) -> str:
    """Dispatch one ``qzone_publish`` tool call into a JSON envelope.

    Parameters
    ----------
    args_json
        Raw ``ToolCallEvent.args_json`` bytes.
    onebot_client
        Pre-constructed :class:`OneBotClient` — pass for unit tests
        or when the caller wants to share one client across many
        dispatches. When ``None`` the dispatcher constructs one via
        ``onebot_client_factory`` (or env vars).
    onebot_client_factory
        Optional zero-arg callable that returns a fresh
        :class:`OneBotClient`. Used by the agent servicer to pull
        ``ws_url`` from the channel config without leaking that config
        into this module's import graph.
    image_with_refs_dispatcher
        Optional callable matching
        :func:`corlinman_agent.image.dispatch_image_with_refs`'s
        signature. Used when ``args["generate"]`` is set. Injected as a
        kw arg so this module doesn't take a hard import dependency on
        the image module — the servicer wires it in.
    image_with_refs_kwargs
        Static kwargs to pass alongside the per-call ``generate`` args
        (e.g. ``{"provider": ..., "persona_store": ..., "asset_store": ...,
        "bound_persona_id": ...}``). Required when
        ``image_with_refs_dispatcher`` is set.
    http_transport
        Optional :mod:`httpx` test seam for the QZone HTTP calls. The
        OneBot client gets its own transport (passed at construction).

    Returns
    -------
    str
        JSON envelope. Success: ``{"ok": true, "tid": "...",
        "qzone_url": "...", "uin": "...", "images": N, "generated":
        true/false}``. Failure: ``{"ok": false, "error": "<code>",
        "message": "..."}``. Never raises.
    """
    args = _decode(args_json)

    # Normalize text + images + generate args.
    text_raw = args.get("text")
    text = (text_raw.strip() if isinstance(text_raw, str) else "")

    images_raw = args.get("images") or []
    if isinstance(images_raw, str):
        images_list: list[str] = [images_raw]
    elif isinstance(images_raw, list):
        images_list = [str(p) for p in images_raw if isinstance(p, (str, bytes))]
    else:
        return _err(
            "invalid_args",
            "'images' must be a list of file paths",
        )

    generate = args.get("generate")
    if generate is not None and not isinstance(generate, dict):
        return _err(
            "invalid_args",
            "'generate' must be an image_with_refs args object (dict)",
        )

    if not text and not images_list and not generate:
        return _err(
            "invalid_args",
            "qzone_publish requires 'text', 'images', or 'generate'",
        )

    cfg = _policy_config(policy_resolver)
    try:
        text_decision = moderate_text(text, cfg).decision
        if not text_decision.allowed:
            return _policy_error(text_decision)
        prompt = generate.get("prompt") if isinstance(generate, dict) else ""
        prompt_decision = moderate_text(str(prompt or ""), cfg).decision
        if not prompt_decision.allowed:
            return _policy_error(prompt_decision)
        media_requested = bool(images_list or generate)
        if media_requested:
            media_decision = moderate_media(config=cfg)
            if not media_decision.allowed:
                if text:
                    # Protection keeps the scheduled post useful while refusing
                    # to generate, read, authenticate, or upload unclassified media.
                    images_list = []
                    generate = None
                else:
                    return _policy_error(media_decision)
    except Exception:
        return _policy_error(classifier_failure_decision(text))

    if execution_mode == "shadow":
        return json.dumps(
            {
                "ok": True,
                "shadow": True,
                "effect": "qzone_publish",
                "text_chars": len(text),
                "media_suppressed": bool(images_raw or args.get("generate")),
            },
            ensure_ascii=False,
        )

    # Step 1: optionally call image_with_refs and prepend its output.
    generated_path: str | None = None
    if generate is not None:
        if image_with_refs_dispatcher is None:
            return _err(
                "image_with_refs_unavailable",
                "qzone_publish received a 'generate' arg but no "
                "image_with_refs dispatcher was wired",
            )
        extra_kwargs = dict(image_with_refs_kwargs or {})
        try:
            raw_result = await image_with_refs_dispatcher(
                args_json=json.dumps(generate).encode("utf-8"),
                **extra_kwargs,
            )
        except Exception as exc:
            logger.exception("qzone_publish.image_with_refs_failed")
            return _err(
                "image_with_refs_failed",
                f"image generation failed: {exc}",
            )
        try:
            result_obj = json.loads(raw_result)
        except (ValueError, TypeError) as exc:
            return _err(
                "image_with_refs_failed",
                f"image_with_refs returned non-JSON: {exc}",
            )
        if not isinstance(result_obj, dict) or not result_obj.get("ok"):
            inner_err = (
                result_obj.get("error")
                if isinstance(result_obj, dict)
                else "unknown"
            )
            inner_msg = (
                result_obj.get("message")
                if isinstance(result_obj, dict)
                else ""
            )
            return _err(
                "image_with_refs_failed",
                f"image generation failed ({inner_err}): {inner_msg}",
            )
        generated_path = result_obj.get("path")
        if not isinstance(generated_path, str) or not generated_path.strip():
            return _err(
                "image_with_refs_failed",
                "image_with_refs returned no path",
            )
        # Prepend so the generated image appears first in the 说说 grid.
        images_list = [generated_path, *images_list]

    if len(images_list) > _MAX_IMAGES:
        return _err(
            "too_many_images",
            f"QZone说说 supports at most {_MAX_IMAGES} images "
            f"(received {len(images_list)})",
        )

    # Step 2: resolve + read all image files up front so a bad path
    # fails before any network call.
    image_payloads: list[tuple[bytes, str]] = []
    for raw_path in images_list:
        resolved = _resolve_image_path(raw_path)
        if resolved is None:
            return _err(
                "image_not_found",
                f"image not found: {raw_path}",
            )
        try:
            image_payloads.append(_read_image_file(resolved))
        except QZoneError as exc:
            return _err(
                "image_read_failed",
                f"image {raw_path!r}: {exc}",
            )

    # Step 3: pull QQ login state from OneBot.
    own_client = False
    if onebot_client is None:
        if onebot_client_factory is not None:
            try:
                onebot_client = onebot_client_factory()
            except Exception as exc:
                return _err(
                    "onebot_unavailable",
                    f"could not construct OneBot client: {exc}",
                )
        else:
            try:
                onebot_client = OneBotClient()
            except OneBotError as exc:
                return _err("onebot_unavailable", str(exc))
        own_client = True

    try:
        try:
            login_info = await onebot_client.fetch_login_info()
            cookie = await onebot_client.fetch_cookies(_QZONE_COOKIE_DOMAIN)
        except OneBotError as exc:
            return _err("onebot_failed", str(exc))
        except Exception as exc:
            logger.exception("qzone_publish.onebot_unexpected")
            return _err("onebot_failed", f"OneBot call failed: {exc}")
    finally:
        if own_client:
            # Best-effort cleanup — a transient aclose failure must not
            # mask the (potentially successful) login/cookie fetch above.
            with contextlib.suppress(Exception):
                await onebot_client.aclose()

    uin = str(login_info.get("qq") or login_info.get("user_id") or "").strip()
    if not uin:
        return _err(
            "onebot_failed",
            "OneBot login info missing user_id / qq",
        )

    p_skey = _extract_cookie_value(cookie, "p_skey")
    if not p_skey:
        return _err(
            "qzone_cookie_stale",
            "p_skey not found in OneBot cookies — the QQ login may be "
            "stale or NapCat hasn't fetched the QZone cookie jar yet",
        )
    skey = _extract_cookie_value(cookie, "skey") or ""
    gtk = _compute_gtk(p_skey)

    effect, effect_error = await _prepare_effect(
        scheduler_store,
        effect_context,
        effect_kind="qzone.publish",
        effect_target=f"account:{uin}",
    )
    if effect_error is not None:
        return _err(effect_error, "QZone publish effect could not be reserved.")

    # Step 4: upload images + publish 说说.
    client_kwargs: dict[str, Any] = {
        "timeout": _QZONE_UPLOAD_TIMEOUT,
    }
    if http_transport is not None:
        client_kwargs["transport"] = http_transport

    async with httpx.AsyncClient(**client_kwargs) as client:
        pic_infos: list[dict[str, Any]] = []
        for image_bytes, filename in image_payloads:
            try:
                pic_info = await _upload_one_image(
                    client,
                    image_bytes,
                    filename,
                    uin,
                    skey,
                    p_skey,
                    gtk,
                    cookie,
                )
            except QZoneError as exc:
                if effect is not None:
                    await _complete_effect(
                        scheduler_store,
                        effect,
                        state="failed",
                        error_code="image_upload_failed",
                    )
                return _err(
                    "image_upload_failed",
                    f"upload failed for {filename!r}: {exc}",
                )
            except Exception as exc:
                logger.exception(
                    "qzone_publish.upload_unexpected", filename=filename
                )
                if effect is not None:
                    await _complete_effect(
                        scheduler_store,
                        effect,
                        state="failed",
                        error_code="image_upload_failed",
                    )
                return _err(
                    "image_upload_failed",
                    f"upload failed for {filename!r}: {exc}",
                )
            pic_infos.append(pic_info)

        form = _build_publish_form(text, uin, pic_infos)
        try:
            raw = await _qzone_publish_post(client, form, gtk, cookie, uin)
        except QZoneError as exc:
            if effect is not None:
                await _complete_effect(
                    scheduler_store,
                    effect,
                    state="unknown",
                    error_code="qzone_publish_transport_failed",
                )
            return _err("qzone_publish_failed", str(exc))
        except Exception as exc:
            logger.exception("qzone_publish.publish_unexpected")
            if effect is not None:
                await _complete_effect(
                    scheduler_store,
                    effect,
                    state="unknown",
                    error_code="qzone_publish_transport_failed",
                )
            return _err("qzone_publish_failed", str(exc))

    parsed = _parse_publish_response(raw)
    if not parsed.get("ok"):
        if effect is not None:
            await _complete_effect(
                scheduler_store,
                effect,
                state="failed",
                error_code="qzone_rejected",
            )
        return _err(
            "qzone_rejected",
            f"QZone rejected the post: {parsed.get('error')}",
            code=parsed.get("code"),
        )

    tid = parsed.get("tid")
    tid_str = str(tid) if tid is not None else None
    qzone_url = _qzone_url(uin, tid_str)
    if effect is not None and not await _complete_effect(
        scheduler_store,
        effect,
        state="sent",
        receipt={"tid": tid_str, "qzone_url": qzone_url, "uin": uin},
    ):
        return _err(
            "scheduler_effect_receipt_unknown",
            "QZone publish may be public but its durable receipt was not confirmed.",
            tid=tid_str,
            qzone_url=qzone_url,
        )
    return json.dumps(
        {
            "ok": True,
            "tid": tid_str,
            "qzone_url": qzone_url,
            "uin": uin,
            "images": len(pic_infos),
            "generated": bool(generated_path),
        },
        ensure_ascii=False,
    )
