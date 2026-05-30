"""Zero-config public-origin learning.

The agent status-card links (and the ``agent_status_card`` tool) need a
public base URL — ``https://host`` — to build a tappable
``{public_url}/status/{token}`` link. Operators *can* set it explicitly via
``[server].public_url`` / ``CORLINMAN_PUBLIC_URL``, but to keep setup
friction-free we also **learn** it from real inbound HTTP requests:

* A request that arrives through the real public hostname (e.g. a browser
  opening the admin UI, or a chat user tapping a status link) carries the
  origin in its ``Host`` / ``X-Forwarded-Host`` + ``X-Forwarded-Proto``
  headers. :class:`OriginLearningMiddleware` extracts that origin and
  persists it to ``<data_dir>/public_origin`` (one line, atomic write,
  only when it changes).
* The channel reply path (gateway process) and the ``agent_status_card``
  tool (separate agent process) both fall back to this learned file when no
  explicit ``public_url`` is configured — so the first real request through
  the public hostname lights the feature up everywhere, no restart needed.

Loopback / bind-placeholder hosts (``localhost``, ``127.0.0.1``,
``0.0.0.0``, ``::1``, ``testserver``) are ignored: a health check or a
curl-from-the-box must never get learned as the public origin. Explicit
config always wins — when ``public_url`` is set the learned file is never
consulted.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from starlette.types import ASGIApp, Receive, Scope, Send

__all__ = [
    "REMEMBERED_ORIGIN_FILENAME",
    "OriginLearningMiddleware",
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


def origin_from_headers(
    headers: dict[str, str], fallback_scheme: str = "http"
) -> str | None:
    """Derive ``scheme://host`` from request headers, or ``None``.

    ``headers`` keys are treated case-insensitively (pass a dict with
    lowercased keys). Honors ``X-Forwarded-Proto`` / ``X-Forwarded-Host``
    (reverse-proxy chain — takes the leftmost hop) and falls back to the
    bare ``Host`` header. Returns ``None`` for loopback / placeholder hosts
    so they are never learned.
    """
    get = lambda k: headers.get(k)  # noqa: E731 - tiny local accessor
    scheme = (
        _first_forwarded(get("x-forwarded-proto")) or fallback_scheme or "http"
    ).lower()
    if scheme not in ("http", "https"):
        scheme = "http"
    host = _first_forwarded(get("x-forwarded-host")) or get("host")
    if not host:
        return None
    host = host.strip()
    host_only = host.rsplit(":", 1)[0] if ":" in host and not host.endswith("]") else host
    if host_only.lower() in _NON_PUBLIC_HOSTS:
        return None
    return f"{scheme}://{_strip_default_port(host, scheme)}"


def load_remembered_origin(data_dir: Path | None) -> str:
    """Read the learned origin (``""`` when absent / unreadable)."""
    path = remembered_origin_path(data_dir)
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def remember_origin(data_dir: Path | None, origin: str) -> bool:
    """Persist ``origin`` under ``data_dir`` iff it changed.

    Returns ``True`` when the file was (re)written, ``False`` otherwise
    (no data dir, empty origin, or unchanged). Atomic via a temp-file
    rename so a concurrent reader never sees a half-written value.
    """
    path = remembered_origin_path(data_dir)
    if path is None or not origin:
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
    ) -> None:
        self.app = app
        self._data_dir = data_dir
        self._enabled = data_dir is not None and not explicitly_configured
        self._on_learn = on_learn
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
        origin = origin_from_headers(headers, fallback_scheme)
        if not origin or origin == self._last:
            return
        if remember_origin(self._data_dir, origin):
            self._last = origin
            if self._on_learn is not None:
                try:
                    self._on_learn(origin)
                except Exception:  # noqa: BLE001
                    pass
