"""Restricted public-origin learning.

The agent status-card links (and the ``agent_status_card`` tool) need a
public base URL — ``https://host`` — to build a tappable
``{public_url}/status/{token}`` link. Operators should set it explicitly via
``[server].public_url`` / ``CORLINMAN_PUBLIC_URL`` in production, but as a
restricted fallback we can **learn** an allow-listed origin from real inbound
HTTP requests:

* A request that arrives through the real public hostname (e.g. a browser
  opening the admin UI, or a chat user tapping a status link) carries the
  origin in its ``Host`` header, or in ``X-Forwarded-Host`` +
  ``X-Forwarded-Proto`` only when the client is a trusted reverse proxy.
  :class:`OriginLearningMiddleware` extracts that origin and
  persists it to ``<data_dir>/public_origin`` (one line, atomic write,
  only when it changes).
* The channel reply path (gateway process) and the ``agent_status_card``
  tool (separate agent process) both fall back to this learned file when no
  explicit ``public_url`` is configured — so the first real request through
  an allowed public hostname can light the feature up everywhere, no restart
  needed.

Loopback / bind-placeholder hosts (``localhost``, ``127.0.0.1``,
``0.0.0.0``, ``::1``, ``testserver``) are ignored: a health check or a
curl-from-the-box must never get learned as the public origin. Explicit
config always wins — when ``public_url`` is set the learned file is never
consulted.
"""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Callable, Iterable
from pathlib import Path
from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send

__all__ = [
    "REMEMBERED_ORIGIN_FILENAME",
    "OriginLearningMiddleware",
    "is_trusted_proxy",
    "load_remembered_origin",
    "origin_from_headers",
    "remember_origin",
    "remembered_origin_path",
]

#: One-line file under the data dir holding the most-recently-learned origin.
REMEMBERED_ORIGIN_FILENAME = "public_origin"

#: Hosts that must never be learned as a public origin (loopback / bind
#: placeholders / the Starlette TestClient host). Compared on the host part
#: only (port stripped).
_NON_PUBLIC_HOSTS: frozenset[str] = frozenset(
    {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]", "testserver", ""}
)


def remembered_origin_path(data_dir: Path | None) -> Path | None:
    """Path to the learned-origin file under ``data_dir`` (None if no dir)."""
    if data_dir is None:
        return None
    return Path(data_dir) / REMEMBERED_ORIGIN_FILENAME


def _first_forwarded(value: str | None) -> str | None:
    """Leftmost entry of a possibly comma-chained forwarded header."""
    if not value:
        return None
    first = value.split(",", 1)[0].strip()
    return first or None


def _strip_default_port(host: str, scheme: str) -> str:
    """Drop ``:80`` / ``:443`` when they match the scheme (cosmetic)."""
    if scheme == "https" and host.endswith(":443"):
        return host[: -len(":443")]
    if scheme == "http" and host.endswith(":80"):
        return host[: -len(":80")]
    return host


def _host_part(host: str) -> str:
    """Return the comparison host (lowercase, port stripped)."""
    value = host.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[: end + 1] if end != -1 else value
    if value.count(":") == 1:
        return value.rsplit(":", 1)[0]
    return value


def _canonical_origin(origin: str) -> str | None:
    """Normalize ``scheme://host`` enough for allow-list comparison."""
    value = origin.strip()
    if not value:
        return None
    parsed = urlsplit(value if "://" in value else f"//{value}")
    host = parsed.netloc or parsed.path
    if not host:
        return None
    scheme = (parsed.scheme or "").lower()
    if scheme and scheme not in {"http", "https"}:
        return None
    host = _strip_default_port(host.lower(), scheme or "http")
    return f"{scheme}://{host}" if scheme else host


def _origin_allowed(
    origin: str, allowed_public_origins: Iterable[str] | None
) -> bool:
    """Return whether ``origin`` is permitted to be learned.

    ``None`` preserves the historical no-allow-list mode for direct helper
    callers. An empty iterable is an explicit deny-all list, which is what the
    gateway installs when auto-learning is enabled without operator allow-list
    configuration. Entries may be full origins (``https://bot.example.com``) or
    bare hosts (``bot.example.com``).
    """
    if allowed_public_origins is None:
        return True

    candidate = _canonical_origin(origin)
    if candidate is None:
        return False
    candidate_host = _canonical_origin(
        candidate.removeprefix("http://").removeprefix("https://")
    )

    for raw in allowed_public_origins:
        allowed = _canonical_origin(str(raw))
        if not allowed:
            continue
        if "://" in allowed:
            if candidate == allowed:
                return True
            continue
        if candidate_host == allowed:
            return True
    return False


def is_trusted_proxy(client: object, trusted_proxies: Iterable[str] | None) -> bool:
    """Return True when the ASGI ``scope['client']`` IP is trusted.

    ``trusted_proxies`` accepts literal IPs or CIDR ranges (for example
    ``127.0.0.1`` or ``10.0.0.0/8``). Invalid entries are ignored so a bad
    config cannot break request handling.
    """
    if not trusted_proxies:
        return False
    host = None
    if isinstance(client, (list, tuple)) and client:
        host = client[0]
    elif isinstance(client, str):
        host = client
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(str(host).strip())
    except ValueError:
        return False
    for raw in trusted_proxies:
        value = str(raw).strip()
        if not value:
            continue
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError:
            continue
        if addr in network:
            return True
    return False


def origin_from_headers(
    headers: dict[str, str],
    fallback_scheme: str = "http",
    *,
    use_forwarded: bool = False,
    allowed_public_origins: Iterable[str] | None = None,
) -> str | None:
    """Derive ``scheme://host`` from request headers, or ``None``.

    ``headers`` keys are treated case-insensitively (pass a dict with
    lowercased keys). ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` are honored
    only when ``use_forwarded`` is true (the middleware sets this only for
    trusted reverse-proxy clients); otherwise the ASGI scheme + bare ``Host``
    header are used. Loopback / placeholder hosts and origins outside
    ``allowed_public_origins`` are not learned.
    """
    get = lambda k: headers.get(k)  # noqa: E731 - tiny local accessor
    scheme = fallback_scheme or "http"
    if use_forwarded:
        scheme = _first_forwarded(get("x-forwarded-proto")) or scheme
    scheme = scheme.lower()
    if scheme not in ("http", "https"):
        scheme = "http"

    host = get("host")
    if use_forwarded:
        host = _first_forwarded(get("x-forwarded-host")) or host
    if not host:
        return None
    host = host.strip()
    if _host_part(host) in _NON_PUBLIC_HOSTS:
        return None

    origin = f"{scheme}://{_strip_default_port(host, scheme)}"
    if not _origin_allowed(origin, allowed_public_origins):
        return None
    return origin


def load_remembered_origin(data_dir: Path | None) -> str:
    """Read the learned origin (``""`` when absent / unreadable)."""
    path = remembered_origin_path(data_dir)
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def remember_origin(
    data_dir: Path | None,
    origin: str,
    *,
    allowed_public_origins: Iterable[str] | None = None,
) -> bool:
    """Persist ``origin`` under ``data_dir`` iff it changed.

    Returns ``True`` when the file was (re)written, ``False`` otherwise
    (no data dir, empty origin, or unchanged). Atomic via a temp-file
    rename so a concurrent reader never sees a half-written value.
    """
    path = remembered_origin_path(data_dir)
    if path is None or not origin:
        return False
    if not _origin_allowed(origin, allowed_public_origins):
        return False
    if load_remembered_origin(data_dir) == origin:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(origin, encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


class OriginLearningMiddleware:
    """ASGI middleware that learns the public origin from inbound requests.

    Pure ASGI (not ``BaseHTTPMiddleware``) so it adds no per-request task
    overhead and never touches the response body. It only inspects request
    headers, debounces via an in-memory ``_last`` cache (disk is touched
    only when the origin actually changes), and fires an optional
    ``on_learn(origin)`` callback so the channel feature can re-arm live.

    Disabled (transparent pass-through) when ``explicitly_configured`` is
    True — an operator-set ``public_url`` must not be shadowed by a learned
    value.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        data_dir: Path | None,
        explicitly_configured: bool = False,
        on_learn: Callable[[str], None] | None = None,
        allowed_public_origins: Iterable[str] | None = None,
        trusted_proxies: Iterable[str] | None = None,
    ) -> None:
        self.app = app
        self._data_dir = data_dir
        self._enabled = data_dir is not None and not explicitly_configured
        self._on_learn = on_learn
        self._allowed_public_origins = tuple(allowed_public_origins or ())
        self._trusted_proxies = tuple(trusted_proxies or ())
        # Seed the debounce cache from any previously-learned value so we
        # don't rewrite an identical origin on the first request after boot.
        self._last = load_remembered_origin(data_dir) if self._enabled else ""

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if self._enabled and scope.get("type") == "http":
            try:
                self._learn(scope)
            except Exception:  # noqa: BLE001 - learning must never break a request
                pass
        await self.app(scope, receive, send)

    def _learn(self, scope: Scope) -> None:
        if not self._data_dir:
            return
        raw = scope.get("headers") or []
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1") for k, v in raw
        }
        fallback_scheme = str(scope.get("scheme") or "http")
        use_forwarded = is_trusted_proxy(scope.get("client"), self._trusted_proxies)
        origin = origin_from_headers(
            headers,
            fallback_scheme,
            use_forwarded=use_forwarded,
            allowed_public_origins=self._allowed_public_origins,
        )
        if not origin or origin == self._last:
            return
        if remember_origin(
            self._data_dir,
            origin,
            allowed_public_origins=self._allowed_public_origins,
        ):
            self._last = origin
            if self._on_learn is not None:
                try:
                    self._on_learn(origin)
                except Exception:  # noqa: BLE001
                    pass
