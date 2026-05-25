"""Shared :class:`AdminState` for the ``routes_admin_b`` sub-routers.

Mirrors ``rust/crates/corlinman-gateway/src/routes/admin/mod.rs::AdminState``
but only carries the slots actually consumed by the Python-side ports.
Slots are typed loosely (``Any``) because the concrete protocols live in
sibling packages that may be reshuffled — keeping the contract narrow at
this seam avoids churn rippling into every route module.

The state is *not* a FastAPI ``Depends`` directly; instead each module
calls :func:`get_admin_state` which reads from a module-global slot
populated by :func:`set_admin_state`. This mirrors the Rust
``with_state(state)`` pattern (where every sub-router got an Arc-clone
of the same backing store) and avoids threading dependency-injection
through every route's signature.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request


@dataclass
class AdminState:
    """Runtime handles every admin route may need.

    All fields are optional — handlers gate on presence with the same
    "503 ``<subsystem>_disabled``" convention the Rust admin tree uses.
    """

    # Live config snapshot (a callable returning the current dict-shaped
    # config). Implementations may swap an ArcSwap-equivalent here once
    # the Python-side config-watcher lands; today the gateway
    # bootstrapper supplies a lambda that returns a freshly-cloned dict.
    config_loader: Any | None = None

    # Plugin registry — corlinman_providers.plugins.PluginRegistry.
    plugins: Any | None = None

    # Evolution store handle (corlinman_evolution_store.EvolutionStore).
    evolution_store: Any | None = None

    # Memory host (corlinman_memory_host.MemoryHost) for /admin/memory.
    memory_host: Any | None = None

    # RAG vector store handle (structural; populated by the deployment).
    rag_store: Any | None = None

    # Multi-tenant admin DB
    # (corlinman_server.tenancy.AdminDb).
    admin_db: Any | None = None

    # Scheduler runtime handle
    # (corlinman_server.scheduler.SchedulerHandle).
    scheduler: Any | None = None

    # Log broadcaster — lazy import of
    # corlinman_server.gateway.core.log_broadcast.LogBroadcaster.
    log_broadcast: Any | None = None

    # On-disk path of the active TOML config (None when started without
    # one — POST /admin/config etc 503 with `config_path_unset`).
    config_path: Path | None = None

    # Python-side py-config.json drop, re-emitted after admin writes.
    py_config_path: Path | None = None

    # Data dir (per-tenant SQLite roots live under here).
    data_dir: Path | None = None

    # Admin credentials/session state. Kept in the same shape as
    # routes_admin_a.AdminState so both bundles can share one auth guard.
    admin_username: str | None = None
    admin_password_hash: str | None = None
    session_store: Any | None = None

    # Allowed-tenants set for federation middleware.
    allowed_tenants: frozenset[str] = frozenset()

    # In-process write lock — every admin route that mutates config TOML
    # must take this so concurrent POST/PATCH calls don't clobber each
    # other.
    admin_write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # Mapping of any extra subsystem handles a particular route module
    # needs (e.g. scheduler_history). Kept as a free-form bag so the
    # boot path can wire one-offs without growing the dataclass.
    extras: dict[str, Any] = field(default_factory=dict)

    # -- W4.6: curator UI surface ---------------------------------------
    #
    # The ``/admin/curator`` routes consume three handles:
    #
    # * ``curator_state_repo`` — :class:`corlinman_evolution_store.
    #   CuratorStateRepo` (async, over the evolution sqlite). Drives the
    #   per-profile threshold tunables + pause toggle + run history.
    # * ``signals_repo`` — :class:`corlinman_evolution_store.SignalsRepo`
    #   (async, same connection). Curator runs emit ``EVENT_*`` rows so
    #   the run/preview routes thread it through to
    #   :func:`maybe_run_curator`.
    # * ``skill_registry_factory`` — synchronous callable
    #   ``(profile_slug: str) -> corlinman_skills_registry.SkillRegistry``.
    #   The bootstrapper wires the factory to read each profile's skills
    #   dir; the routes only need a way to materialise a *current* view
    #   of skills for one profile without taking a dependency on the
    #   skills-loading internals.
    #
    # All three are typed loosely (``Any``) so this dataclass stays
    # importable even when the evolution-store / skills-registry packages
    # aren't installed at import time (the routes 503 with a typed error
    # envelope instead).
    profile_store: Any | None = None
    curator_state_repo: Any | None = None
    signals_repo: Any | None = None
    skill_registry_factory: Any | None = None

    # -- W1.3: task-observability surface --------------------------------
    #
    # ``journal`` is the :class:`~corlinman_server.agent_journal.AgentJournal`
    # the gateway already opens for per-turn resume (T4.1). The SSE
    # replay route reads ``turn_events`` from it; the cost-aggregate
    # route reads the ``turns`` table plus the ``TurnComplete`` event
    # payloads for pre-W1.2 fallback.
    #
    # ``event_emitter`` is the :class:`~corlinman_server.gateway.
    # observability.JournalBackedEmitter` instance the gateway lifecycle
    # constructs once and shares with every reasoning loop / runner pool /
    # subagent supervisor. The SSE-live route grabs a subscriber off it
    # to receive envelopes in real time. ``None`` keeps the routes' typed
    # 503 ``observability_disabled`` envelope live so an in-progress port
    # / a degraded boot still serves cleanly.
    journal: Any | None = None
    event_emitter: Any | None = None

    # -- W1.1: GitHub-releases update checker ----------------------------
    #
    # ``update_checker`` is the
    # :class:`~corlinman_server.system.UpdateChecker` the gateway
    # lifecycle constructs once and shares with every admin route in
    # this bundle. The ``/admin/system/*`` routes read it through
    # :func:`get_admin_state` and degrade with a typed 503
    # (``update_checker_disabled``) when it is absent — which keeps the
    # gateway booting cleanly even when the system module is excluded
    # from the build or the data dir is unwritable.
    update_checker: Any | None = None

    # -- W1.3 (one-click upgrade): upgrader + audit log -----------------
    #
    # ``upgrader`` is the :class:`~corlinman_server.system.upgrader.
    # UpgraderProtocol` instance the gateway lifecycle constructs once
    # per process (Docker or native, picked by ``CORLINMAN_RUNTIME_MODE``).
    # ``None`` keeps the ``/admin/system/upgrade*`` routes' typed 503
    # (``upgrader_unavailable``) envelope live so a runtime-mode-unknown
    # boot still serves cleanly — the operator can still use the
    # copy-paste fallback from ``/admin/system/upgrade-commands``.
    #
    # ``audit_log`` is the :class:`~corlinman_server.system.SystemAuditLog`
    # the lifecycle opens against ``<data_dir>/system-audit.log``. Every
    # upgrade request + state transition records into it; the
    # ``/admin/system/audit`` route tails it newest-first. ``None``
    # collapses the audit-tail route to ``[]`` rather than 503 — a
    # missing log is the empty-history case, not a degradation.
    #
    # Both are typed ``Any`` to avoid an import cycle between the routes
    # bundle and the upgrader / audit modules at type-check time.
    upgrader: Any | None = None
    audit_log: Any | None = None

    # -- W1.3 (multi-agent): background subagent dispatch ----------------
    #
    # ``subagent_store`` is the :class:`~corlinman_server.system.subagent.
    # SubagentTaskStore` opened against ``$DATA_DIR/.subagent-state.json``.
    # ``subagent_dispatcher`` is the :class:`~corlinman_server.system.
    # subagent.AsyncSubagentDispatcher` instance that wraps it + closes
    # over the supervisor/agent-registry/provider plumbing the tool
    # wrapper needs when ``run_in_background=true``. Both are ``None``
    # in degraded boots; the ``/admin/subagents*`` routes return a typed
    # 503 (``subagent_dispatcher_unavailable``) in that case.
    subagent_store: Any | None = None
    subagent_dispatcher: Any | None = None

    # -- W1.3 (skill hub): ClawHub client + install task store ----------
    #
    # ``clawhub_client`` is the
    # :class:`~corlinman_server.system.skill_hub.ClawHubClient` instance
    # the gateway lifecycle constructs once per process (it owns a
    # reusable :class:`httpx.AsyncClient` + a small TTL cache). The
    # ``/admin/skills/hub/*`` routes resolve it via ``get_admin_state``;
    # a ``None`` slot collapses the search / featured handlers to the
    # offline envelope rather than 503, which keeps the page renderable
    # when the hub is unreachable.
    #
    # ``skill_install_store`` is the in-process
    # :class:`~corlinman_server.gateway.routes_admin_b.skills.SkillInstallTaskStore`
    # the lifecycle constructs alongside the client. The install POST
    # handler registers one row per request_id; the SSE handler reads
    # state transitions off the same store. A ``None`` slot returns a
    # typed 503 ``skill_install_unavailable`` from the install routes.
    clawhub_client: Any | None = None
    skill_install_store: Any | None = None


_state: AdminState | None = None


def set_admin_state(state: AdminState | None) -> None:
    """Install (or clear) the process-global :class:`AdminState`.

    Called by the gateway bootstrapper before :func:`build_router` is
    mounted onto the FastAPI app. Tests reach for this to swap a
    fixture-built state.
    """
    global _state
    _state = state


def get_admin_state() -> AdminState:
    """Read the active :class:`AdminState`.

    Raises :class:`RuntimeError` when the state hasn't been installed —
    a clearer failure than a chain of ``None`` attribute errors deep
    inside a handler. Routes that legitimately operate without certain
    slots should still gate on the slot's presence after this call.
    """
    if _state is None:
        # Default to an empty state so handlers route through their
        # own disabled-503 branches rather than 500ing on missing
        # state. Mirrors the Rust ``AdminState::new`` default of "all
        # slots None".
        return AdminState()
    return _state


def config_snapshot(state: AdminState | None = None) -> Mapping[str, Any]:
    """Return the current config as a plain dict (or empty when unset).

    Convenience wrapper around ``state.config_loader()``. Routes call
    this so a missing loader collapses to ``{}`` instead of raising —
    most handlers gate on ``cfg.get("...")`` anyway.
    """
    st = state if state is not None else get_admin_state()
    if st.config_loader is None:
        return {}
    try:
        snap = st.config_loader()
        if isinstance(snap, Mapping):
            return snap
    except Exception:
        return {}
    return {}


# ---------------------------------------------------------------------------
# Auth dependency — lazy import of the middleware module so tests can
# import the routers without the middleware package being present.
# ---------------------------------------------------------------------------


async def require_admin(request: Request) -> None:
    """FastAPI dependency that enforces admin credentials."""
    from corlinman_server.gateway.routes_admin_a._auth_shim import (
        authenticate_admin_request,
        require_admin_dependency,
    )

    try:
        authenticate_admin_request(request, get_admin_state())
    except HTTPException as exc:
        if isinstance(exc.detail, dict) and exc.detail.get("reason") == "admin_not_configured":
            require_admin_dependency(request)
            return None
        raise
    return None
