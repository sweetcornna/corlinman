"""Shared internals for the builtin web tools.

Kept private (leading underscore) — the public surface is the
``dispatch_*`` callables in :mod:`.fetch` / :mod:`.search` /
:mod:`.calculator`. This module holds the bits all three need: a
lenient ``args_json`` decoder mirroring the blackboard tool's, a
dependency-free HTML → readable-text extractor, and the SSRF
guard (:func:`is_safe_host`).
"""

from __future__ import annotations

import html
import ipaddress
import os
import re
import socket
from typing import Any

import httpx
import structlog

logger = structlog.get_logger(__name__)

#: Default User-Agent. Some endpoints (notably DuckDuckGo) reject the
#: stock ``python-httpx`` UA, so we present as a desktop browser.
DEFAULT_USER_AGENT: str = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

#: Wall-clock ceiling for any single outbound request, seconds.
DEFAULT_TIMEOUT_SECONDS: float = 12.0


class WebArgsInvalidError(Exception):
    """Raised by the per-tool arg parsers; the dispatcher catches it and
    folds the message into an ``{"error": "args_invalid: ..."}`` envelope.
    Same shape as the subagent / blackboard ``_ArgsInvalidError`` so the
    model sees a uniform failure surface across all builtin tools."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def decode_args(args_json: bytes | str) -> dict[str, Any]:
    """Decode a tool call's raw ``args_json`` into a dict.

    Accepts the ``ToolCallEvent.args_json`` bytes (utf-8 OpenAI
    ``function.arguments`` string) or an already-decoded string. Mirrors
    :func:`corlinman_agent.subagent.blackboard._decode`.
    """
    if isinstance(args_json, (bytes, bytearray)):
        try:
            decoded = bytes(args_json).decode("utf-8")
        except UnicodeDecodeError as exc:  # pragma: no cover - defensive
            raise WebArgsInvalidError(f"args_json not utf-8: {exc}") from exc
    else:
        decoded = args_json
    import json

    try:
        raw = json.loads(decoded) if decoded.strip() else {}
    except json.JSONDecodeError as exc:
        raise WebArgsInvalidError(f"args_json not JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise WebArgsInvalidError(
            f"args_json must be a JSON object, got {type(raw).__name__}"
        )
    return raw


# ---------------------------------------------------------------------------
# HTML → text
# ---------------------------------------------------------------------------

#: Block-level tags whose boundaries should become newlines so the
#: extracted text keeps a sane paragraph structure.
_BLOCK_TAGS = (
    "p",
    "div",
    "br",
    "li",
    "tr",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "section",
    "article",
    "header",
    "footer",
    "blockquote",
)

_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript|template|svg|head)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_BLOCK_RE = re.compile(
    r"</?(?:" + "|".join(_BLOCK_TAGS) + r")\b[^>]*>",
    re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RUN_RE = re.compile(r"[ \t]+")
_BLANKLINE_RUN_RE = re.compile(r"\n\s*\n\s*")
_TITLE_RE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def extract_title(markup: str) -> str | None:
    """Pull the ``<title>`` text out of an HTML document, if present."""
    match = _TITLE_RE.search(markup)
    if match is None:
        return None
    title = html.unescape(_TAG_RE.sub("", match.group(1))).strip()
    return title or None


def html_to_text(markup: str) -> str:
    """Strip HTML markup down to readable plain text.

    Dependency-free on purpose — ``corlinman-agent`` should not grow a
    BeautifulSoup / lxml dependency for a builtin tool. The heuristic:

    1. drop ``<script>`` / ``<style>`` / ``<head>`` / comment blocks
       wholesale (their text is never reader content);
    2. turn block-level tag boundaries into newlines so paragraphs
       survive;
    3. strip every remaining tag;
    4. unescape HTML entities and collapse whitespace runs.

    Good enough for feeding a page's prose to an LLM; it is explicitly
    *not* a layout-faithful renderer.
    """
    text = _SCRIPT_STYLE_RE.sub(" ", markup)
    text = _COMMENT_RE.sub(" ", text)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _WS_RUN_RE.sub(" ", text)
    text = _BLANKLINE_RUN_RE.sub("\n\n", text)
    # Trim trailing spaces left on each line.
    text = "\n".join(line.strip() for line in text.splitlines())
    return text.strip()


def looks_like_html(content_type: str | None, body: str) -> bool:
    """Best-effort: should ``body`` be run through :func:`html_to_text`?"""
    if content_type and "html" in content_type.lower():
        return True
    if content_type and any(
        kind in content_type.lower()
        for kind in ("json", "xml", "text/plain", "csv", "javascript")
    ):
        return False
    # No / generic content-type — sniff for a tag.
    head = body.lstrip()[:512].lower()
    return head.startswith("<!doctype html") or "<html" in head or "<body" in head


def make_client(
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    follow_redirects: bool = False,
) -> httpx.AsyncClient:
    """Construct the outbound :class:`httpx.AsyncClient`.

    ``transport`` is the test seam — production passes ``None`` (real
    network), unit tests inject an :class:`httpx.MockTransport`.

    ``follow_redirects`` defaults to **False** so SSRF callers can
    re-validate each hop manually via :func:`is_safe_host`. The
    deprecated implicit redirect-following remains available for
    callers that have no security exposure (e.g. the search backend
    talking to a fixed endpoint).
    """
    return httpx.AsyncClient(
        transport=transport,
        timeout=timeout,
        follow_redirects=follow_redirects,
        headers={"User-Agent": DEFAULT_USER_AGENT},
    )


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------

#: IPv4 cloud-metadata service address. Always denied regardless of how
#: ``ipaddress`` classifies it — AWS / GCP / Azure / DigitalOcean all
#: serve credentials here and the LLM should never reach it.
_METADATA_V4 = ipaddress.ip_address("169.254.169.254")
#: IPv6 cloud-metadata service address (GCP).
_METADATA_V6 = ipaddress.ip_address("fd00:ec2::254")


class WebFetchUnsafeHostError(Exception):
    """Raised when an outbound URL resolves to a private / loopback /
    link-local / reserved / multicast IP, or to a known cloud
    metadata endpoint.

    The dispatcher catches this and folds the message into an
    ``{"error": "unsafe_host: ..."}`` envelope. Production runs of the
    agent are routinely deployed alongside internal services on the
    same network; an LLM with web_fetch can otherwise be coerced (via
    prompt injection in a fetched page or via a doctored search hit)
    into probing those services or exfiltrating cloud credentials
    through the metadata endpoint.
    """


def _allow_private_override() -> bool:
    """Honour ``CORLINMAN_WEB_FETCH_ALLOW_PRIVATE=1`` for development.

    Default off. The variable is read at every check so test fixtures
    can flip it on/off via :meth:`monkeypatch.setenv`.
    """
    return os.environ.get("CORLINMAN_WEB_FETCH_ALLOW_PRIVATE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _ip_is_unsafe(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Return a short reason string when ``addr`` should be blocked.

    ``None`` means the address is safe to dial. Reasons:

    * ``"metadata"`` — cloud metadata service (highest priority; the
      classification of 169.254.x is technically link-local but we
      want a clearer error and we want the rule to survive future
      changes to the stdlib classification).
    * ``"loopback"`` — 127.0.0.0/8 or ::1.
    * ``"private"`` — RFC 1918 / RFC 4193 / etc.
    * ``"link_local"`` — 169.254.0.0/16 (excluding metadata, already
      handled above) or fe80::/10.
    * ``"reserved"`` — IANA-reserved ranges.
    * ``"multicast"`` — 224.0.0.0/4 / ff00::/8.
    * ``"unspecified"`` — 0.0.0.0 / ::.
    """
    if addr == _METADATA_V4 or addr == _METADATA_V6:
        return "metadata"
    if addr.is_loopback:
        return "loopback"
    if addr.is_link_local:
        return "link_local"
    if addr.is_private:
        return "private"
    if addr.is_multicast:
        return "multicast"
    if addr.is_unspecified:
        return "unspecified"
    if addr.is_reserved:
        return "reserved"
    return None


def is_safe_host(url: str) -> list[str]:
    """Validate that ``url`` is safe to dial and return the validated IP(s).

    Raises :class:`WebFetchUnsafeHostError` when:

    * the scheme is anything but ``http`` / ``https``;
    * the host parses literally as an unsafe IP;
    * the host resolves (via :func:`socket.getaddrinfo`) to ANY unsafe
      IP — a single unsafe resolved address fails the whole check, so
      DNS pinning attacks ("evil.example.com → 10.0.0.5") cannot
      bypass the guard.

    On success returns the list of validated IP-literal strings the host
    resolved to (a single-element list for a literal-IP URL). Callers
    that dial the host MUST pin the connection to one of these exact IPs
    (see :func:`pin_transport`) so no second, unvalidated DNS lookup can
    race the guard's — a DNS-rebind attacker who answers the guard with a
    public IP and the connect with an internal one is otherwise able to
    bypass the check entirely (SEC-012). Callers that only need the
    boolean verdict (e.g. filtering result URLs) may ignore the return.

    Override: setting ``CORLINMAN_WEB_FETCH_ALLOW_PRIVATE=1`` skips the
    IP-classification check (scheme + metadata are still enforced) so
    a developer running the agent against a local fixture can still
    use ``http://127.0.0.1:8080`` without flipping security off
    globally. Production deployments must never set this.
    """
    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise WebFetchUnsafeHostError(
            f"scheme {scheme!r} not allowed (only http/https)"
        )
    host = parsed.hostname
    if not host:
        raise WebFetchUnsafeHostError("missing host")

    allow_private = _allow_private_override()

    # If host parses as a literal IP, classify it directly. ``getaddrinfo``
    # would also do this but parsing first lets us reject without DNS.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        # Metadata is always blocked, even with the dev override.
        if literal == _METADATA_V4 or literal == _METADATA_V6:
            raise WebFetchUnsafeHostError(
                f"cloud metadata endpoint {host} is always denied"
            )
        if allow_private:
            return [str(literal)]
        reason = _ip_is_unsafe(literal)
        if reason is not None:
            raise WebFetchUnsafeHostError(
                f"host {host} resolves to {reason} address {literal}"
            )
        return [str(literal)]

    # DNS path: resolve every address record and check each one. A single
    # unsafe address denies the whole request — there is no point dialing
    # a hostname where any DNS round-robin entry leaks internal traffic.
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise WebFetchUnsafeHostError(f"dns resolution failed: {exc}") from exc

    validated: list[str] = []
    seen: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        # sockaddr[0] is the address string for both AF_INET
        # (str, int) and AF_INET6 (str, int, int, int) tuples; the
        # stub types it as ``str | int`` so narrow before use.
        ip_str = str(sockaddr[0])
        # Strip IPv6 zone-id suffix if present.
        ip_str = ip_str.split("%", 1)[0]
        if ip_str in seen:
            continue
        seen.add(ip_str)
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            # If we can't classify it, refuse on the safe side.
            raise WebFetchUnsafeHostError(
                f"host {host} resolved to unclassifiable address {ip_str!r}"
            ) from None
        # Metadata is always blocked regardless of override.
        if addr == _METADATA_V4 or addr == _METADATA_V6:
            raise WebFetchUnsafeHostError(
                f"host {host} resolves to cloud metadata endpoint {addr}"
            )
        if allow_private:
            validated.append(str(addr))
            continue
        reason = _ip_is_unsafe(addr)
        if reason is not None:
            raise WebFetchUnsafeHostError(
                f"host {host} resolves to {reason} address {addr}"
            )
        validated.append(str(addr))
    if not validated:
        raise WebFetchUnsafeHostError(f"host {host} resolved to no addresses")
    return validated


# ---------------------------------------------------------------------------
# DNS-rebind pin (SEC-012)
# ---------------------------------------------------------------------------


def _bracket_if_ipv6(ip: str) -> str:
    """Wrap a bare IPv6 literal in ``[...]`` for use in a ``Host`` header /
    authority. IPv4 and already-bracketed values pass through unchanged."""
    if ":" in ip and not ip.startswith("["):
        return f"[{ip}]"
    return ip


class PinnedTransport(httpx.AsyncBaseTransport):
    """Wrap an inner transport and dial a *pinned* IP, never re-resolving.

    The SSRF guard (:func:`is_safe_host`) resolves and classifies a host's
    IPs but the connection layer would otherwise resolve the hostname a
    *second* time — a DNS-rebind attacker answers the guard's lookup with a
    public IP and the connect's lookup with an internal one, bypassing the
    guard (SEC-012). This transport closes that TOCTOU window: it rewrites
    each outgoing request to dial ``pinned_ip`` directly (so the socket
    opens to the validated address, with no further DNS) while preserving:

    * the original ``Host`` header (virtual-host routing); and
    * the TLS ``sni_hostname`` / ``server_hostname`` (so HTTPS cert
      validation still happens against the *hostname*, not the IP).

    The pin is per-request and keyed on the request's own host, so it is
    safe to reuse across redirect hops: each hop is independently
    re-validated and re-pinned by the caller before the request is built.
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        *,
        expected_host: str,
        pinned_ip: str,
    ) -> None:
        self._inner = inner
        # Lower-cased hostname the pin applies to; other hosts (should not
        # occur, but be conservative) pass through unmodified.
        self._expected_host = expected_host.lower()
        self._pinned_ip = pinned_ip

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        original_host = request.url.host
        if original_host.lower() != self._expected_host:
            # Not the host we validated — do not pin (defensive; the caller
            # builds a fresh transport per hop so this should not happen).
            return await self._inner.handle_async_request(request)

        # Preserve the authority for the Host header + TLS SNI before we
        # rewrite the URL host to the pinned IP.
        is_default_port = request.url.port is None
        port = request.url.port
        host_authority = _bracket_if_ipv6(original_host)
        if not is_default_port:
            host_authority = f"{host_authority}:{port}"

        request.url = request.url.copy_with(host=self._pinned_ip)
        request.headers["Host"] = host_authority
        # TLS validates the cert against the original hostname, not the IP.
        request.extensions = {
            **request.extensions,
            "sni_hostname": original_host,
        }
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


def pin_transport(
    inner: httpx.AsyncBaseTransport | None,
    *,
    host: str,
    pinned_ip: str,
) -> httpx.AsyncBaseTransport:
    """Build a :class:`PinnedTransport` that dials ``pinned_ip`` for ``host``.

    ``inner`` is the underlying transport — ``None`` in production (we
    construct the default :class:`httpx.AsyncHTTPTransport`), or an
    injected :class:`httpx.MockTransport` in tests.
    """
    base = inner if inner is not None else httpx.AsyncHTTPTransport()
    return PinnedTransport(base, expected_host=host, pinned_ip=pinned_ip)


def _url_host(url: str) -> str | None:
    """Best-effort hostname extraction for pinning decisions."""
    import urllib.parse

    return urllib.parse.urlparse(url).hostname
