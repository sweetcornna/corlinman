"""Boot-time constants + small bootstrap readers for the gateway entrypoint.

Extracted verbatim from
:mod:`corlinman_server.gateway.lifecycle.entrypoint` (Modularization
Phase 2 god-file reduction). This module is the CANONICAL home for the
bind-address defaults (:data:`DEFAULT_HOST` / :data:`DEFAULT_PORT`) and the
SIGTERM exit code, plus the small config-drop / scheduler-job / identity
sweep helpers the lifespan calls:

* :data:`DEFAULT_HOST` / :data:`DEFAULT_PORT` — the canonical bind defaults
  (``cli_helpers`` re-imports them so there is exactly one definition).
* :data:`SIGTERM_EXIT_CODE` — graceful-shutdown exit code.
* :func:`RESTART_REQUIRED_SECTIONS_LOCAL` — lazy accessor for the config
  watcher's restart-required section set.
* :func:`_emit_py_config_drop` — best-effort write of the Rust→Python
  config handshake JSON drop.
* :func:`list_default_scheduler_jobs` — public reader of the in-memory
  default scheduler-job list.
* :func:`_identity_sweep_loop` — periodic expired-verification-phrase sweep.

The entrypoint re-imports every one of these back, so its public surface
and ``__all__`` are unchanged. This module never imports the entrypoint
module nor ``cli_helpers`` (no import cycle); the one ``_lazy_import``
caller (:func:`RESTART_REQUIRED_SECTIONS_LOCAL`) resolves that helper
lazily inside the function so ``cli_helpers`` can re-import the bind
defaults from here without a top-level cycle.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import structlog

from corlinman_server.gateway.lifecycle.py_config import (
    default_py_config_path,
    write_py_config_sync,
)

logger = structlog.get_logger(__name__)

#: Mirrors ``corlinman_gateway::main::resolve_addr`` — same defaults so a
#: deployment-script that sets ``PORT`` / ``BIND`` against the Rust
#: binary keeps working against the Python port.
DEFAULT_HOST: str = "127.0.0.1"
DEFAULT_PORT: int = 6005
SIGTERM_EXIT_CODE: int = 143


def _emit_py_config_drop(cfg: Any | None) -> None:
    """Best-effort write of the JSON handshake file.

    No-op when ``cfg`` is ``None`` — there's nothing to render and the
    Python AI plane falls back to the legacy prefix table in that case
    (matches the Rust behaviour).
    """
    if cfg is None:
        return
    target = Path(
        os.environ.get("CORLINMAN_PY_CONFIG") or str(default_py_config_path())
    )
    try:
        write_py_config_sync(cfg, target)
        logger.info("gateway.py_config.written", path=str(target))
    except Exception as exc:
        logger.warning(
            "gateway.py_config.write_failed",
            path=str(target),
            error=str(exc),
        )


def RESTART_REQUIRED_SECTIONS_LOCAL() -> frozenset[str]:
    """Lazy accessor for the watcher's restart-required section set.

    Imported via a function (not a module-level ``from``) so the
    entrypoint stays importable when ``config_watcher`` is mid-port —
    consistent with the rest of the lazy-import discipline in this file.
    """
    from corlinman_server.gateway.lifecycle.cli_helpers import _lazy_import

    watcher_mod = _lazy_import("corlinman_server.gateway.core.config_watcher")
    if watcher_mod is None:
        return frozenset()
    return getattr(watcher_mod, "RESTART_REQUIRED_SECTIONS", frozenset())


def list_default_scheduler_jobs(app: Any) -> list[Any]:
    """Read the in-memory default scheduler-job list.

    Public helper so tests (and any future scheduler-spawn wiring) can
    inspect what the lifecycle registered without poking
    ``app.state.corlinman_default_scheduler_jobs`` directly. Returns a
    *copy* so callers can iterate freely without racing the lifespan.
    Empty list when nothing was registered or the slot is missing.
    """
    jobs = getattr(app.state, "corlinman_default_scheduler_jobs", None)
    if isinstance(jobs, list):
        return list(jobs)
    return []


#: Identity verification-phrase sweep cadence (seconds). Phrases TTL out
#: after ``corlinman_identity.DEFAULT_TTL_MIN``; a sweep every 10 min keeps
#: the expired-phrase table small without hammering sqlite.
_IDENTITY_SWEEP_INTERVAL_SECS: int = 600


async def _identity_sweep_loop(store: Any) -> None:
    """Periodically purge expired verification phrases.

    Calls ``store.sweep_expired_phrases()`` every
    :data:`_IDENTITY_SWEEP_INTERVAL_SECS` seconds until cancelled. Each
    sweep is best-effort: a sqlite hiccup logs a warning and the loop
    keeps going so a transient error doesn't kill the periodic cleanup.
    Exits cleanly on :class:`asyncio.CancelledError` (lifespan shutdown).
    """
    sweep = getattr(store, "sweep_expired_phrases", None)
    if sweep is None:
        return
    while True:
        try:
            await asyncio.sleep(_IDENTITY_SWEEP_INTERVAL_SECS)
        except asyncio.CancelledError:
            return
        try:
            purged = await sweep()
            if purged:
                logger.info("gateway.identity.sweep_purged", count=int(purged))
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 — never kill the loop
            logger.warning("gateway.identity.sweep_failed", error=str(exc))
