"""Process-wide web-search defaults, applied from the ``[web_search]`` block.

The ``web_search`` tool runs inside the **agent** process, which never sees
the gateway's config snapshot — it learns configuration solely through the
``py-config.json`` sidecar. Before this module the backend and API key were
readable *only* from ``CORLINMAN_WEB_SEARCH_*`` environment variables, and
the agent systemd unit deliberately carries no ``EnvironmentFile``: it sets
exactly ``HOME`` / ``CORLINMAN_EXECUTION_STATE_DIR`` / ``CORLINMAN_PY_CONFIG``
/ ``CORLINMAN_PY_SOCKET``. Every native deployment therefore fell through to
the keyless DuckDuckGo HTML scrape with no way to configure anything else.

Precedence, highest first:

1. **these defaults** (what the operator set in the UI);
2. ``CORLINMAN_WEB_SEARCH_*`` environment variables;
3. the keyless built-in default (``ddg``).

Config sits above the env vars deliberately, matching
:mod:`corlinman_agent.voice.defaults`: the env vars predate the settings
page, and an operator changing a value in the UI must not be silently
overridden by a stale export on the host.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "WebSearchDefaults",
    "apply_web_search_config",
    "get_web_search_defaults",
    "reset_web_search_defaults",
    "set_web_search_defaults",
    "web_search_defaults_from_config",
]


@dataclass(frozen=True, slots=True)
class WebSearchDefaults:
    """Operator-chosen defaults; empty strings mean "not configured"."""

    backend: str = ""
    api_key: str = ""

    def as_dict(self) -> dict[str, Any]:
        # ``api_key`` is deliberately reported as a presence flag, never a
        # value — this dict feeds structured logs and the admin read model.
        return {"backend": self.backend, "api_key_set": bool(self.api_key)}


_DEFAULTS = WebSearchDefaults()
_LOCK = threading.RLock()


def get_web_search_defaults() -> WebSearchDefaults:
    with _LOCK:
        return _DEFAULTS


def set_web_search_defaults(defaults: WebSearchDefaults) -> None:
    global _DEFAULTS
    with _LOCK:
        _DEFAULTS = defaults


def reset_web_search_defaults() -> None:
    """Restore the unconfigured state (used by tests)."""
    set_web_search_defaults(WebSearchDefaults())


def _clean_str(raw: Any) -> str:
    return raw.strip() if isinstance(raw, str) and raw.strip() else ""


def web_search_defaults_from_config(
    section: Mapping[str, Any] | None,
) -> WebSearchDefaults:
    """Parse a ``[web_search]`` config block into :class:`WebSearchDefaults`.

    Tolerant by design — a malformed value falls back to "unconfigured"
    rather than raising, because this runs on the sidecar-load path where a
    bad block must not take the agent process down.
    """
    if not isinstance(section, Mapping):
        return WebSearchDefaults()
    return WebSearchDefaults(
        backend=_clean_str(section.get("backend")).lower(),
        api_key=_clean_str(section.get("api_key")),
    )


def apply_web_search_config(
    section: Mapping[str, Any] | None,
) -> WebSearchDefaults:
    """Apply a whole ``[web_search]`` block.

    The single entry point both the in-process boot path and the agent
    sidecar loader call, so the two can never drift apart.
    """
    defaults = web_search_defaults_from_config(section)
    set_web_search_defaults(defaults)
    return defaults
