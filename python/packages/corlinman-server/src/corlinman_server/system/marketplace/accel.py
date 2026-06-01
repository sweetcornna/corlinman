"""``corlinman_server.system.marketplace.accel`` — GitHub URL accelerator.

Users running corlinman on Chinese servers frequently can't reach
``github.com`` / ``raw.githubusercontent.com`` reliably. This module
rewrites every GitHub-host URL the marketplace fetches through a
configurable mirror so the catalog + downloads load fast.

Design constraints
------------------

* **Pure + deterministic.** :meth:`GithubAccelerator.accelerate` does no
  I/O and no clock/random reads, so the rewrite table is exhaustively
  unit-testable and the gateway's resume-safety rules are respected. The
  live reachability probe lives in the admin "Test acceleration" route,
  not here.
* **Only GitHub hosts are touched.** A non-GitHub URL (or an
  already-accelerated one) is returned unchanged — the accelerator is a
  no-op everywhere except the four GitHub origins.
* **Secrets never cross a public proxy.** The accelerator only rewrites
  the URL; the source layer is responsible for *dropping* an
  ``Authorization`` header when the rewritten host is a third-party
  proxy (``ghproxy`` / ``jsdelivr`` / ``custom``). See
  :meth:`is_trusted_host`.

Modes / presets
---------------

``mode``: ``off`` (never), ``on`` (always), ``auto`` (enable on a China
signal — see :meth:`enabled`).

``preset``:

* ``ghproxy`` / ``custom`` — *prefix* style: ``<base><original-url>`` (the
  ghproxy convention, e.g. ``https://ghproxy.com/https://raw...``). Works
  for raw content, codeload tarballs, and release assets alike.
* ``jsdelivr`` — rewrite ``raw.githubusercontent.com/<o>/<r>/<ref>/<p>``
  →  ``cdn.jsdelivr.net/gh/<o>/<r>@<ref>/<p>``. Raw repo content only;
  non-raw GitHub URLs pass through unchanged (jsdelivr can't serve
  release assets or the API).
* ``mirror`` — host substitution: swap the GitHub host for
  ``mirror_host`` (a self-hosted reverse proxy that preserves paths).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

__all__ = ["AccelSettings", "GithubAccelerator"]


#: The GitHub origins worth accelerating. Anything else is left alone.
_GITHUB_HOSTS = frozenset(
    {
        "github.com",
        "raw.githubusercontent.com",
        "codeload.github.com",
        "objects.githubusercontent.com",
        "api.github.com",
    }
)

#: Timezone tokens that signal a China-region host for ``mode = "auto"``.
#: Deliberately conservative — only unambiguous Chinese zones (US "CST"
#: is intentionally excluded; it collides with US Central Time).
_CN_TZ_TOKENS = (
    "asia/shanghai",
    "asia/chongqing",
    "asia/harbin",
    "asia/urumqi",
    "asia/kashgar",
    "prc",
)


@dataclass(frozen=True, slots=True)
class AccelSettings:
    """Resolved ``[marketplace.github_proxy]`` config."""

    mode: str = "off"  # off | auto | on
    preset: str = "ghproxy"  # ghproxy | jsdelivr | mirror | custom
    base: str = "https://ghproxy.com/"
    mirror_host: str = ""  # used when preset == "mirror"
    assume_region: str = ""  # "" | "cn" | "global" — forces the auto decision


class GithubAccelerator:
    """Rewrites GitHub URLs per :class:`AccelSettings`. Stateless + pure."""

    __slots__ = ("_settings",)

    def __init__(self, settings: AccelSettings | None = None) -> None:
        self._settings = settings or AccelSettings()

    @property
    def settings(self) -> AccelSettings:
        return self._settings

    # ------------------------------------------------------------------
    # Enablement (resolves ``auto`` against region signals)
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        """Whether rewriting is active right now.

        ``on`` → always, ``off`` → never, ``auto`` → :meth:`_detect_cn`.
        Pure: reads only settings + process env (no network, no clock).
        """
        mode = (self._settings.mode or "off").lower()
        if mode == "on":
            return True
        if mode == "off":
            return False
        return self._detect_cn()

    def _detect_cn(self) -> bool:
        """Heuristic China detection for ``mode = "auto"``.

        Precedence:

        1. ``assume_region`` config (``cn`` → True, ``global`` → False).
        2. ``CORLINMAN_REGION`` env (same two values).
        3. ``TZ`` env / configured timezone matching a Chinese zone token.

        Defaults to ``False`` when nothing matches — we never silently
        route a non-China host through a third-party mirror.
        """
        region = (self._settings.assume_region or "").lower()
        if region == "cn":
            return True
        if region == "global":
            return False
        env_region = os.environ.get("CORLINMAN_REGION", "").lower()
        if env_region == "cn":
            return True
        if env_region == "global":
            return False
        tz = os.environ.get("TZ", "").lower()
        return any(token in tz for token in _CN_TZ_TOKENS)

    # ------------------------------------------------------------------
    # Rewrite
    # ------------------------------------------------------------------

    def is_trusted_host(self, url: str) -> bool:
        """``True`` when ``url`` targets GitHub directly or a self-hosted
        ``mirror`` — i.e. it is safe to attach an ``Authorization`` token.

        A public-proxy rewrite (``ghproxy`` / ``jsdelivr`` / ``custom``)
        is *not* trusted; the source layer drops auth for those.
        """
        host = (urlsplit(url).hostname or "").lower()
        if host in _GITHUB_HOSTS:
            return True
        if self._settings.preset == "mirror" and self._settings.mirror_host:
            return host == _host_only(self._settings.mirror_host)
        return False

    def accelerate(self, url: str) -> str:
        """Return the (possibly) rewritten URL.

        No-op when disabled, when ``url`` is not a GitHub host, or when
        the chosen preset doesn't apply to this URL shape.
        """
        if not self.enabled:
            return url
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host not in _GITHUB_HOSTS:
            return url

        preset = (self._settings.preset or "ghproxy").lower()
        if preset in ("ghproxy", "custom"):
            base = self._settings.base.strip()
            if not base:
                return url
            # Prefix convention: <base>/<original-url>. We keep the full
            # original URL (scheme included) — that's what ghproxy-style
            # mirrors expect.
            return base.rstrip("/") + "/" + url
        if preset == "jsdelivr":
            return _to_jsdelivr(parts, url)
        if preset == "mirror":
            mirror = self._settings.mirror_host.strip()
            if not mirror:
                return url
            return _swap_host(parts, mirror)
        return url


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _host_only(value: str) -> str:
    """Extract a bare host from a ``host`` or ``scheme://host[/...]`` string."""
    value = value.strip()
    if "://" in value:
        return (urlsplit(value).hostname or "").lower()
    return value.split("/", 1)[0].lower()


def _to_jsdelivr(parts: object, url: str) -> str:
    """Rewrite a raw.githubusercontent.com URL to a jsdelivr ``gh`` URL.

    ``raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>`` becomes
    ``cdn.jsdelivr.net/gh/<owner>/<repo>@<ref>/<path>``. Any other GitHub
    URL shape (codeload tarball, release asset, API) is returned
    unchanged — jsdelivr only fronts raw repo content.
    """
    from urllib.parse import SplitResult

    assert isinstance(parts, SplitResult)
    if (parts.hostname or "").lower() != "raw.githubusercontent.com":
        return url
    segs = [s for s in parts.path.split("/") if s]
    if len(segs) < 4:
        return url
    owner, repo, ref, *rest = segs
    path = "/".join(rest)
    return f"https://cdn.jsdelivr.net/gh/{owner}/{repo}@{ref}/{path}"


def _swap_host(parts: object, mirror_host: str) -> str:
    """Replace the host of ``parts`` with ``mirror_host``, preserving path."""
    from urllib.parse import SplitResult, urlunsplit

    assert isinstance(parts, SplitResult)
    host = _host_only(mirror_host)
    return urlunsplit(("https", host, parts.path, parts.query, parts.fragment))
