"""``corlinman_server.gateway.core.config`` — gateway config loader.

Python port of ``rust/crates/corlinman-core/src/config.rs`` (the loader
half). This is the **keystone sibling** the gateway entrypoint
lazy-imports: when it is absent ``entrypoint._load_config`` logs
``gateway.sibling_missing module=...core.config`` and the gateway boots
in degraded mode. Landing this module is Parcel P0 of the Python-port
runtime-completion plan (see ``docs/PLAN_PORT_COMPLETION.md`` §0–§3 and
``docs/contracts/runtime-wiring.md``).

What it does
------------

:func:`load_from_path` reads a ``config.toml`` with :mod:`tomllib`,
resolves every ``{ env = "X" }`` / ``{ env = "X", default = "Y" }``
reference against :data:`os.environ`, and returns the result.

Return shape — *a plain ``dict``*
---------------------------------

The gateway has a large surface of **dict-shaped** config readers:
``routes_admin_b.state.config_snapshot`` does ``isinstance(snap,
Mapping)``; admin routes do ``cfg.get("channels")`` / ``cfg.get(
"providers")``; ``config_watcher.diff_sections`` takes ``dict``.
Breaking any of them would ripple through the whole admin tree.

Therefore :func:`load_from_path` deliberately returns a **plain
``dict``** (env-refs already resolved). It does *not* return a bespoke
typed object — dict access is the contract. A typed/attribute view can
be layered on later (``Config`` mapping wrapper) without changing this
function's return type, because a wrapper that subclasses ``dict``
keeps every existing reader working; that is left to a follow-up so P0
stays minimal and zero-risk.

Env-ref resolution — what stays raw
-----------------------------------

There are *two* distinct config consumers and they want different
shapes:

* **The runtime** (provider registry / chat service — Waves P1/P2):
  wants secrets *resolved* to their actual values. That is what this
  loader produces, and it is what gets attached to
  :class:`~corlinman_server.gateway.core.state.AppState.config`.
* **The admin API** (``/admin/providers`` etc.): explicitly inspects
  the *unresolved* ``{ env = "OPENAI_API_KEY" }`` shape (it surfaces
  "which env var" to the operator and never wants the literal secret).
  Those routes read a *separate* raw-``tomllib`` snapshot wired in
  ``entrypoint._mount_routes`` (``_admin_b_config_loader``) — this
  loader does **not** feed them, so resolving env-refs here is safe.

A resolved secret collapses ``{ env = "X" }`` to the string value of
``os.environ["X"]`` (or the ``default``, or ``None`` when neither is
available — matching the Rust ``Option<String>`` "no auth" semantics).
``py_config.render_py_config`` already accepts bare-string secrets, so
the JSON-drop handshake keeps working unchanged.

The function is intentionally total: a malformed file raises so the
caller (``entrypoint._load_config``) can log ``config.load_failed`` and
fall back to degraded mode, exactly as it does today.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

__all__ = [
    "load_from_path",
    "parse_config",
    "resolve_env_refs",
]


# ---------------------------------------------------------------------------
# Env-ref resolution
# ---------------------------------------------------------------------------


def _is_env_ref(value: Any) -> bool:
    """A value is an env-ref iff it is a mapping with an ``env`` key and
    no keys outside the recognised ``{env, default}`` pair.

    The strict key check stops us from accidentally collapsing a
    legitimate nested table that merely *happens* to contain an ``env``
    sub-key (e.g. a ``[scheduler.jobs.action]`` table with an
    ``env = { KEY = "val" }`` block) into a scalar.
    """
    if not isinstance(value, Mapping):
        return False
    if "env" not in value:
        return False
    return set(value.keys()) <= {"env", "default"}


def _resolve_env_ref(ref: Mapping[str, Any]) -> str | None:
    """Resolve a single ``{ env = "X" }`` / ``{ env = "X", default = "Y" }``
    reference against :data:`os.environ`.

    Precedence: live env var → ``default`` → ``None``. An empty-string
    env var counts as *present* (mirrors the Rust ``std::env::var``
    behaviour — only an *unset* var falls through to ``default``).
    """
    env_name = ref.get("env")
    default = ref.get("default")
    if env_name is None:
        # ``{ env = <non-string> }`` — degrade to the default rather
        # than crash; an operator typo shouldn't take the gateway down.
        return None if default is None else str(default)
    value = os.environ.get(str(env_name))
    if value is not None:
        return value
    return None if default is None else str(default)


def resolve_env_refs(value: Any) -> Any:
    """Recursively replace every ``{ env = ... }`` reference inside
    ``value`` with its resolved string (or ``None``).

    Walks dicts and lists; scalars pass through untouched. The input is
    not mutated — a fresh structure is returned so the caller can keep
    the raw parse if it needs it.
    """
    if _is_env_ref(value):
        return _resolve_env_ref(value)
    if isinstance(value, Mapping):
        return {k: resolve_env_refs(v) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_env_refs(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Parse + load
# ---------------------------------------------------------------------------


def parse_config(text: str) -> dict[str, Any]:
    """Parse TOML ``text`` and resolve env-refs. Returns a plain dict.

    Split out from :func:`load_from_path` so callers that already hold
    the file contents (the config-watcher's parser hook, tests) can
    reuse the exact same resolution path without a disk round-trip.

    Raises :class:`tomllib.TOMLDecodeError` on malformed input.
    """
    raw = tomllib.loads(text)
    resolved = resolve_env_refs(raw)
    # ``resolve_env_refs`` returns the same top-level shape it was
    # given; a TOML document's root is always a table → always a dict.
    assert isinstance(resolved, dict)  # noqa: S101 — invariant guard
    return resolved


def load_from_path(path: Path | str) -> dict[str, Any]:
    """Load + parse the gateway config TOML at ``path``.

    This is the symbol ``entrypoint._load_config`` reaches for via
    ``getattr(core_config, "load_from_path", None)``. It returns a
    **plain ``dict``** with every ``{ env = "X" }`` reference resolved
    against :data:`os.environ` — the runtime config object attached to
    :class:`~corlinman_server.gateway.core.state.AppState.config`.

    The returned dict is safe to hand to every existing dict-shaped
    reader (``cfg.get(...)``, ``isinstance(cfg, Mapping)``,
    ``config_watcher.diff_sections``).

    Raises :class:`FileNotFoundError` when ``path`` does not exist and
    :class:`tomllib.TOMLDecodeError` on malformed TOML — the caller
    (``entrypoint._load_config``) already guards the file-exists case
    and catches parse failures to fall back to degraded mode.
    """
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    cfg = parse_config(text)
    logger.debug(
        "gateway.core.config.loaded",
        path=str(p),
        sections=sorted(cfg.keys()),
    )
    return cfg
