"""``web_search`` builtin tool — query the web, return result snippets.

The default backend is **keyless**: the DuckDuckGo HTML endpoint
(``https://html.duckduckgo.com/html/``), so the tool works out of the
box on a fresh install with no API key configured. If an operator wires
up a key-based provider (e.g. via ``CORLINMAN_WEB_SEARCH_*`` env vars),
that takes precedence.

Backend selection (highest precedence first):

1. ``CORLINMAN_WEB_SEARCH_BACKEND`` env var — explicit override
   (``ddg`` or ``serpapi``).
2. ``CORLINMAN_WEB_SEARCH_API_KEY`` present → SerpApi backend.
3. otherwise → keyless DuckDuckGo HTML backend.

Wire contract (identical to the other builtin tools):

* :data:`WEB_SEARCH_TOOL` — the wire-stable tool name.
* :func:`web_search_tool_schema` — the OpenAI tool descriptor.
* :func:`dispatch_web_search` — async dispatcher, ``args_json -> str``,
  never raises.

Success envelope::

    {"query": "...", "backend": "ddg", "results": [
        {"title": "...", "url": "...", "snippet": "..."}, ...]}

Degraded / failure envelope (still well-formed so the loop continues)::

    {"query": "...", "backend": "ddg", "results": [], "error": "..."}
"""

from __future__ import annotations

import html
import json
import os
import re
import urllib.parse
from typing import Any

import httpx
import structlog

from corlinman_agent.web._common import (
    WebArgsInvalidError,
    WebFetchUnsafeHostError,
    _url_host,
    decode_args,
    is_safe_host,
    make_client,
    pin_transport,
)
from corlinman_agent.web.external_content import (
    detect_suspicious_patterns,
    wrap_external_content,
)

logger = structlog.get_logger(__name__)

#: Wire-stable tool name.
WEB_SEARCH_TOOL: str = "web_search"

#: Default / max number of results returned.
DEFAULT_MAX_RESULTS: int = 5
MAX_RESULTS_CEILING: int = 10

#: Keyless DuckDuckGo HTML endpoint. The ``lite``/``html`` endpoints
#: return server-rendered markup we can scrape without JS.
_DDG_ENDPOINT: str = "https://html.duckduckgo.com/html/"

#: SerpApi (key-based) endpoint — used only when a key is configured.
_SERPAPI_ENDPOINT: str = "https://serpapi.com/search.json"


def web_search_tool_schema() -> dict[str, Any]:
    """OpenAI-shaped tool descriptor for ``web_search``."""
    return {
        "type": "function",
        "function": {
            "name": WEB_SEARCH_TOOL,
            "description": (
                "Search the web for a query and return a ranked list of "
                "results (title, URL, snippet). Use this to find current "
                "information or locate a page, then call web_fetch on a "
                "promising URL to read it in full."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": (
                            "Optional cap on result count "
                            f"(default {DEFAULT_MAX_RESULTS}, "
                            f"max {MAX_RESULTS_CEILING})."
                        ),
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    }


def _parse_args(args_json: bytes | str) -> tuple[str, int]:
    raw = decode_args(args_json)
    query = raw.get("query")
    if not isinstance(query, str) or not query.strip():
        raise WebArgsInvalidError("missing or empty 'query' field")
    max_results = raw.get("max_results", DEFAULT_MAX_RESULTS)
    if not isinstance(max_results, int) or isinstance(max_results, bool):
        raise WebArgsInvalidError("'max_results' must be an integer")
    if max_results <= 0:
        raise WebArgsInvalidError("'max_results' must be positive")
    return query.strip(), min(max_results, MAX_RESULTS_CEILING)


def _select_backend() -> str:
    """Resolve the active search backend name. See module docstring for
    the precedence rules."""
    explicit = os.environ.get("CORLINMAN_WEB_SEARCH_BACKEND", "").strip().lower()
    if explicit:
        return explicit
    if os.environ.get("CORLINMAN_WEB_SEARCH_API_KEY", "").strip():
        return "serpapi"
    return "ddg"


# ---------------------------------------------------------------------------
# DuckDuckGo HTML scraping
# ---------------------------------------------------------------------------

# The html.duckduckgo.com result list: each hit is an <a class="result__a">
# anchor (title + href) followed by an <a class="result__snippet"> blurb.
_DDG_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="(?P<href>[^"]+)"[^>]*>'
    r"(?P<title>.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)
_DDG_SNIPPET_RE = re.compile(
    r'<a[^>]+class="[^"]*result__snippet[^"]*"[^>]*>(?P<snippet>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(markup: str) -> str:
    """Strip tags + unescape entities + collapse whitespace."""
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub("", markup))).strip()


def _normalise_ddg_href(href: str) -> str:
    """DuckDuckGo wraps result URLs in a ``/l/?uddg=<encoded>`` redirect
    on some endpoints. Unwrap it to the real target when present."""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlparse(href)
    if parsed.path.startswith("/l/") or "duckduckgo.com/l/" in href:
        params = urllib.parse.parse_qs(parsed.query)
        target = params.get("uddg")
        if target:
            return urllib.parse.unquote(target[0])
    return href


def _filter_unsafe_url(url: str) -> bool:
    """Return True if ``url`` is safe to surface to the model.

    Cheap, sync wrapper around :func:`is_safe_host`. We don't want a
    poisoned search-results page (e.g. an attacker who controls one of
    the top hits) to hand the LLM a link to ``http://10.0.0.1/admin``
    that it might then ``web_fetch``. The fetcher already enforces the
    same rule, but filtering here also keeps the model from ever
    seeing the internal URL.
    """
    try:
        is_safe_host(url)
    except WebFetchUnsafeHostError as exc:
        logger.info("web_search.dropped_unsafe", url=url, reason=str(exc))
        return False
    return True


def _parse_ddg_html(markup: str, max_results: int) -> list[dict[str, str]]:
    """Scrape result rows out of a DuckDuckGo HTML response."""
    titles = list(_DDG_RESULT_RE.finditer(markup))
    snippets = [m.group("snippet") for m in _DDG_SNIPPET_RE.finditer(markup)]
    results: list[dict[str, str]] = []
    for idx, match in enumerate(titles):
        if len(results) >= max_results:
            break
        url = _normalise_ddg_href(match.group("href"))
        title = _clean(match.group("title"))
        snippet = _clean(snippets[idx]) if idx < len(snippets) else ""
        if not url or not title:
            continue
        if not _filter_unsafe_url(url):
            continue
        results.append({"title": title, "url": url, "snippet": snippet})
    return results


async def _search_ddg(
    query: str, max_results: int, client: httpx.AsyncClient
) -> list[dict[str, str]]:
    response = await client.post(_DDG_ENDPOINT, data={"q": query})
    response.raise_for_status()
    return _parse_ddg_html(response.text, max_results)


# ---------------------------------------------------------------------------
# SerpApi (key-based, opt-in)
# ---------------------------------------------------------------------------


async def _search_serpapi(
    query: str, max_results: int, client: httpx.AsyncClient
) -> list[dict[str, str]]:
    api_key = os.environ.get("CORLINMAN_WEB_SEARCH_API_KEY", "").strip()
    if not api_key:
        raise WebArgsInvalidError(
            "serpapi backend selected but "
            "CORLINMAN_WEB_SEARCH_API_KEY is not set"
        )
    response = await client.get(
        _SERPAPI_ENDPOINT,
        params={"q": query, "api_key": api_key, "num": max_results},
    )
    response.raise_for_status()
    payload = response.json()
    organic = payload.get("organic_results") or []
    results: list[dict[str, str]] = []
    for hit in organic[:max_results]:
        url = hit.get("link") or ""
        title = hit.get("title") or ""
        if not url or not title:
            continue
        if not _filter_unsafe_url(str(url)):
            continue
        results.append(
            {
                "title": str(title),
                "url": str(url),
                "snippet": str(hit.get("snippet") or ""),
            }
        )
    return results


def _wrap_results(
    results: list[dict[str, str]], source: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Fence each result's untrusted ``snippet`` and collect injection flags.

    Search snippets are untrusted web text exactly like a fetched body —
    a poisoned result can carry prompt-injection prose. We wrap the
    snippet in randomized markers (via :func:`wrap_external_content`) and
    surface any suspicious-pattern hits as advisory metadata. The
    ``title`` / ``url`` are left as-is (the URL is already SSRF-filtered
    upstream and the title is short ranking text), but suspicious-pattern
    detection still scans the raw snippet.
    """
    wrapped: list[dict[str, Any]] = []
    flags: list[str] = []
    for hit in results:
        snippet = hit.get("snippet", "")
        flags.extend(detect_suspicious_patterns(snippet))
        new_hit: dict[str, Any] = dict(hit)
        if snippet:
            new_hit["snippet"] = wrap_external_content(snippet, source=source)
        wrapped.append(new_hit)
    # De-dup flags while preserving order.
    seen: set[str] = set()
    unique_flags: list[str] = []
    for flag in flags:
        if flag in seen:
            continue
        seen.add(flag)
        unique_flags.append(flag)
    return wrapped, unique_flags


async def dispatch_web_search(
    *,
    args_json: bytes | str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    """Translate one ``web_search`` tool call into a JSON envelope.

    Parameters
    ----------
    args_json
        Raw ``ToolCallEvent.args_json`` bytes (or decoded string).
    transport
        Test seam — an :class:`httpx.MockTransport` in unit tests,
        ``None`` in production.

    Returns
    -------
    str
        JSON string for ``ToolResult.content``. Always returns; never
        raises. Search being unavailable degrades to
        ``{"results": [], "error": "..."}`` rather than failing the
        reasoning loop.
    """
    backend = _select_backend()
    try:
        query, max_results = _parse_args(args_json)
    except WebArgsInvalidError as exc:
        return json.dumps(
            {"backend": backend, "results": [], "error": f"args_invalid: {exc.message}"}
        )

    try:
        # The search backend hits a fixed, hard-coded endpoint
        # (``html.duckduckgo.com`` or ``serpapi.com``). Validate + PIN the
        # backend host so a DNS-rebind against that hostname cannot redirect
        # the backend request itself to an internal address between the
        # guard's lookup and the connect's (SEC-012); the SSRF surface here
        # is both the search-result URL list (filtered in ``_parse_*``) AND
        # the request-to-backend transport. Redirects stay on for
        # compatibility with DDG's canonicalisation 30x's — they target the
        # same pinned host.
        endpoint = _SERPAPI_ENDPOINT if backend == "serpapi" else _DDG_ENDPOINT
        backend_host = _url_host(endpoint) or ""
        backend_transport: httpx.AsyncBaseTransport | None = transport
        if backend in ("ddg", "serpapi"):
            try:
                backend_ips = is_safe_host(endpoint)
            except WebFetchUnsafeHostError as exc:
                logger.warning(
                    "web_search.unsafe_backend",
                    backend=backend,
                    endpoint=endpoint,
                    reason=str(exc),
                )
                return json.dumps(
                    {
                        "query": query,
                        "backend": backend,
                        "results": [],
                        "error": f"search_unavailable: unsafe_backend: {exc}",
                    }
                )
            backend_transport = pin_transport(
                transport, host=backend_host, pinned_ip=backend_ips[0]
            )
        async with make_client(
            transport=backend_transport, follow_redirects=True
        ) as client:
            if backend == "serpapi":
                results = await _search_serpapi(query, max_results, client)
            elif backend == "ddg":
                results = await _search_ddg(query, max_results, client)
            else:
                return json.dumps(
                    {
                        "query": query,
                        "backend": backend,
                        "results": [],
                        "error": f"unknown_backend: {backend}",
                    }
                )
        wrapped_results, suspicious = _wrap_results(results, source=backend)
        envelope: dict[str, Any] = {
            "query": query,
            "backend": backend,
            "results": wrapped_results,
        }
        if suspicious:
            envelope["suspicious_patterns"] = suspicious
        return json.dumps(envelope)
    except WebArgsInvalidError as exc:
        return json.dumps(
            {
                "query": query,
                "backend": backend,
                "results": [],
                "error": f"backend_misconfigured: {exc.message}",
            }
        )
    except httpx.TimeoutException as exc:
        logger.info("web_search.timeout", query=query, error=str(exc))
        return json.dumps(
            {
                "query": query,
                "backend": backend,
                "results": [],
                "error": f"timeout: {exc}",
            }
        )
    except httpx.HTTPError as exc:
        logger.info("web_search.http_error", query=query, error=str(exc))
        return json.dumps(
            {
                "query": query,
                "backend": backend,
                "results": [],
                "error": f"search_unavailable: {exc}",
            }
        )
    except Exception as exc:  # noqa: BLE001 - dispatcher must never raise
        logger.exception("web_search.unexpected", query=query)
        return json.dumps(
            {
                "query": query,
                "backend": backend,
                "results": [],
                "error": f"search_unavailable: {exc}",
            }
        )
