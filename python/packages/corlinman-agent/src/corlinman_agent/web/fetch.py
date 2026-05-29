"""``web_fetch`` builtin tool — fetch a URL, return readable text.

Given a URL, this fetches the page with :mod:`httpx` and returns the
extracted prose (HTML stripped) capped at a configurable byte budget so
a single fetch can never blow the model's context window.

Wire contract (identical to the subagent / blackboard tools):

* :data:`WEB_FETCH_TOOL` — the wire-stable tool name.
* :func:`web_fetch_tool_schema` — the OpenAI tool descriptor a parent
  drops into ``ChatStart.tools``.
* :func:`dispatch_web_fetch` — async dispatcher, ``args_json -> str``,
  never raises.

Success envelope::

    {"url": "...", "final_url": "...", "status": 200,
     "title": "...", "content_type": "text/html",
     "text": "...", "truncated": false, "bytes": 1234}

Failure envelope::

    {"url": "...", "error": "timeout: ..."}
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import structlog

from corlinman_agent.web._common import (
    WebArgsInvalidError,
    WebFetchUnsafeHostError,
    _url_host,
    decode_args,
    extract_title,
    html_to_text,
    is_safe_host,
    looks_like_html,
    make_client,
    pin_transport,
)

logger = structlog.get_logger(__name__)

#: Wire-stable tool name. Imported by the gateway dispatcher's
#: ``BUILTIN_TOOLS`` set and any agent card that exposes the tool.
WEB_FETCH_TOOL: str = "web_fetch"

#: Hard ceiling on the *extracted text* returned to the model, chars.
#: ~12k chars ≈ 3k tokens — generous for a single page, bounded enough
#: that the reasoning loop's context stays sane.
DEFAULT_MAX_CHARS: int = 12_000

#: Hard ceiling on the raw response body we will buffer, bytes. A
#: response larger than this is truncated mid-stream and flagged — we
#: never load an unbounded body into memory.
MAX_BODY_BYTES: int = 4_000_000

#: Hard ceiling on the redirect chain length. We re-validate each hop
#: through :func:`is_safe_host` before dialing it, so a malicious server
#: cannot bounce us into an internal address; 5 hops is far above what
#: any well-behaved public site needs.
MAX_REDIRECTS: int = 5


def web_fetch_tool_schema() -> dict[str, Any]:
    """OpenAI-shaped tool descriptor for ``web_fetch``."""
    return {
        "type": "function",
        "function": {
            "name": WEB_FETCH_TOOL,
            "description": (
                "Fetch a web page (or plain-text/JSON resource) by URL "
                "and return its readable text content with HTML stripped. "
                "Use this to read documentation, articles, or API output "
                "the user references. The result is capped in length; "
                "request a specific page rather than a site root when "
                "possible."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": (
                            "Absolute http(s) URL to fetch."
                        ),
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": (
                            "Optional cap on returned text length "
                            f"(default {DEFAULT_MAX_CHARS}, "
                            f"max {DEFAULT_MAX_CHARS})."
                        ),
                    },
                },
                "required": ["url"],
                "additionalProperties": False,
            },
        },
    }


def _parse_args(args_json: bytes | str) -> tuple[str, int]:
    raw = decode_args(args_json)
    url = raw.get("url")
    if not isinstance(url, str) or not url.strip():
        raise WebArgsInvalidError("missing or empty 'url' field")
    url = url.strip()
    scheme = url.split("://", 1)[0].lower() if "://" in url else ""
    if scheme not in ("http", "https"):
        raise WebArgsInvalidError(
            "'url' must be an absolute http(s) URL"
        )
    max_chars = raw.get("max_chars", DEFAULT_MAX_CHARS)
    if not isinstance(max_chars, int) or isinstance(max_chars, bool):
        raise WebArgsInvalidError("'max_chars' must be an integer")
    if max_chars <= 0:
        raise WebArgsInvalidError("'max_chars' must be positive")
    max_chars = min(max_chars, DEFAULT_MAX_CHARS)
    return url, max_chars


async def dispatch_web_fetch(
    *,
    args_json: bytes | str,
    transport: httpx.BaseTransport | None = None,
) -> str:
    """Translate one ``web_fetch`` tool call into a JSON envelope.

    Parameters
    ----------
    args_json
        Raw ``ToolCallEvent.args_json`` bytes (or decoded string).
    transport
        Test seam — an :class:`httpx.MockTransport` in unit tests,
        ``None`` in production (real network).

    Returns
    -------
    str
        JSON string for ``ToolResult.content``. Always returns; never
        raises — every failure path becomes ``{"error": "..."}``.
    """
    try:
        url, max_chars = _parse_args(args_json)
    except WebArgsInvalidError as exc:
        return json.dumps({"error": f"args_invalid: {exc.message}"})

    # SSRF guard: refuse to dial private / loopback / link-local /
    # multicast / metadata addresses BEFORE we open the client. This
    # catches both literal-IP URLs (http://10.0.0.1) and hostnames whose
    # DNS resolves to internal addresses. The guard returns the validated
    # IP(s); we PIN the connection to one of them so the socket cannot be
    # re-resolved to a different (internal) address between the check and
    # the connect (DNS-rebind TOCTOU, SEC-012). We re-validate AND re-pin
    # on every redirect hop below so a public site that 302s to
    # http://169.254.169.254 cannot smuggle credentials out of the cloud
    # metadata service.
    try:
        validated_ips = is_safe_host(url)
    except WebFetchUnsafeHostError as exc:
        logger.warning("web_fetch.unsafe_host", url=url, reason=str(exc))
        return json.dumps({"url": url, "error": f"unsafe_host: {exc}"})

    try:
        current_url = url
        pinned_ip = validated_ips[0]
        redirects = 0
        while True:
            host = _url_host(current_url) or ""
            # One pinned client per hop: the socket dials the validated IP
            # for this hop's host while Host header + TLS SNI stay the
            # hostname (HTTPS cert validation + vhost routing preserved).
            hop_transport = pin_transport(
                transport, host=host, pinned_ip=pinned_ip
            )
            async with make_client(
                transport=hop_transport, follow_redirects=False
            ) as client:
                async with client.stream("GET", current_url) as response:
                    # Report the LOGICAL url for this hop, not ``response.url``
                    # — the pin rewrites the request's URL host to the dialed
                    # IP, so ``response.url`` would leak the pinned IP into the
                    # envelope instead of the hostname the caller requested.
                    final_url = current_url
                    status = response.status_code
                    # Manual redirect handling — re-validate + re-pin each hop.
                    if status in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            # Redirect status with no Location header —
                            # treat as a normal response (no body left
                            # to read, so the envelope just records the
                            # status).
                            content_type = response.headers.get("content-type")
                            body_bytes = b""
                            oversized = False
                            total = 0
                            break
                        # Resolve relative redirects against the current URL.
                        next_url = str(httpx.URL(current_url).join(location))
                        if redirects >= MAX_REDIRECTS:
                            logger.warning(
                                "web_fetch.too_many_redirects",
                                url=url,
                                current=current_url,
                                hops=redirects,
                            )
                            return json.dumps(
                                {
                                    "url": url,
                                    "final_url": final_url,
                                    "status": status,
                                    "error": (
                                        f"too_many_redirects: "
                                        f"exceeded {MAX_REDIRECTS}"
                                    ),
                                }
                            )
                        try:
                            next_ips = is_safe_host(next_url)
                        except WebFetchUnsafeHostError as exc:
                            logger.warning(
                                "web_fetch.unsafe_redirect",
                                url=url,
                                target=next_url,
                                reason=str(exc),
                            )
                            return json.dumps(
                                {
                                    "url": url,
                                    "final_url": final_url,
                                    "status": status,
                                    "error": f"unsafe_redirect: {exc}",
                                }
                            )
                        redirects += 1
                        current_url = next_url
                        pinned_ip = next_ips[0]
                        continue
                    content_type = response.headers.get("content-type")
                    # Stream the body so an oversized response is bounded.
                    chunks: list[bytes] = []
                    total = 0
                    oversized = False
                    async for chunk in response.aiter_bytes():
                        chunks.append(chunk)
                        total += len(chunk)
                        if total > MAX_BODY_BYTES:
                            oversized = True
                            break
                    body_bytes = b"".join(chunks)

                    if response.status_code >= 400:
                        logger.info(
                            "web_fetch.non_200",
                            url=url,
                            status=response.status_code,
                        )
                        return json.dumps(
                            {
                                "url": url,
                                "final_url": final_url,
                                "status": response.status_code,
                                "error": (
                                    f"http_status: server returned "
                                    f"{response.status_code}"
                                ),
                            }
                        )
                    break

        raw_text = body_bytes.decode("utf-8", errors="replace")
        if looks_like_html(content_type, raw_text):
            title = extract_title(raw_text)
            text = html_to_text(raw_text)
        else:
            title = None
            text = raw_text.strip()

        truncated = oversized or len(text) > max_chars
        if len(text) > max_chars:
            text = text[:max_chars]

        return json.dumps(
            {
                "url": url,
                "final_url": final_url,
                "status": status,
                "title": title,
                "content_type": content_type,
                "text": text,
                "truncated": truncated,
                "bytes": total,
            }
        )
    except httpx.TimeoutException as exc:
        logger.info("web_fetch.timeout", url=url, error=str(exc))
        return json.dumps({"url": url, "error": f"timeout: {exc}"})
    except httpx.HTTPError as exc:
        logger.info("web_fetch.http_error", url=url, error=str(exc))
        return json.dumps({"url": url, "error": f"fetch_failed: {exc}"})
    except Exception as exc:  # noqa: BLE001 - dispatcher must never raise
        logger.exception("web_fetch.unexpected", url=url)
        return json.dumps({"url": url, "error": f"fetch_failed: {exc}"})
