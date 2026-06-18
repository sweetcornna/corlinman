"""``corlinman-gateway`` console-script entrypoint.

Python port of ``rust/crates/corlinman-gateway/src/main.rs``.

Boot sequence (parity with the Rust binary, simplified for an
ASGI-on-uvicorn deployment):

1. Parse ``--config <path>`` / ``--host`` / ``--port`` / ``--data-dir``
   from the CLI (also accepts ``CORLINMAN_CONFIG`` / ``BIND`` / ``PORT``
   / ``CORLINMAN_DATA_DIR`` env vars to keep deployments that already
   set them working).
2. Initialise telemetry (OTLP exporter + structlog binding) via
   :mod:`corlinman_server.telemetry`.
3. Run the one-shot legacy data-file migration when the config gates it.
4. Emit the Rust→Python config handshake JSON drop (so any in-process
   consumer that watches ``CORLINMAN_PY_CONFIG`` sees a non-empty
   registry from the first request).
5. Build the FastAPI :class:`fastapi.FastAPI` app via :func:`build_app`
   (lazy-imports the routes/middleware/core/grpc submodules being landed
   by sibling agents).
6. Run uvicorn programmatically with graceful shutdown wired to
   SIGTERM/SIGINT.

Sibling-module wiring
---------------------

Other agents own ``gateway/core/``, ``gateway/middleware/``,
``gateway/routes/``, ``gateway/grpc/``, ``gateway/services/``,
``gateway/evolution/``. Their modules may not exist when this file is
imported, so the FastAPI app factory uses :func:`_lazy_import` to swallow
:class:`ImportError` and log the missing wiring. The expected contract:

* ``gateway.core.AppState.build(config=...)`` → returns an ``AppState``
  bundle (analogue of the Rust ``AppState`` struct).
* ``gateway.middleware.install(app, state)`` → installs every
  cross-cutting middleware (tracing, approval gate, tenant resolution).
* ``gateway.routes.mount(app, state)`` → mounts every HTTP route
  (chat / admin / channels / canvas / …).
* ``gateway.grpc.serve_placeholder_in_background(state, cancel)`` →
  spawns the Rust→Python placeholder UDS server (returns an awaitable).
* ``gateway.<sibling>.bootstrap(state)`` → optional startup hook every
  runtime sibling may export. The lifespan iterates a fixed list
  (``providers``, ``services``, ``evolution``) and calls each module's
  ``bootstrap`` if present. A hook may return ``None``, an awaitable,
  or a list of :class:`asyncio.Task`; returned tasks are registered
  into the background list and cancelled + awaited at shutdown. New
  Wave-1 runtime modules plug in by adding a ``bootstrap`` symbol — the
  seam itself does not need re-editing. See
  ``docs/contracts/runtime-wiring.md`` §2 for the full contract.

Each hook is best-effort: a missing sibling logs ``warning`` and the
gateway boots in degraded mode so a partial port can still serve.

The config loader (``gateway.core.config.load_from_path``) is a
sibling too — :func:`_load_config` lazy-imports it. It is no longer
missing (Parcel P0); a TOML parse failure still falls through to
degraded mode.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from collections.abc import Awaitable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, cast

import structlog

from corlinman_server.gateway.lifecycle.admin_seed import (
    ensure_admin_credentials,
    resolve_admin_config_path,
)

# ``build_app`` (which stays here) calls these directly; the rest are
# re-exported so external importers (``agent_servicer`` imports
# ``_build_agent_registry_stack`` lazily from this module) and the
# ``test_fix_BUG06`` monkeypatch of ``entrypoint._build_state`` keep
# resolving against this module's namespace.
from corlinman_server.gateway.lifecycle.app_factory import (
    _build_agent_registry_stack,  # noqa: F401 — re-export (agent_servicer)
    _build_state,
    _DegradedAppState,  # noqa: F401 — re-export for external importers
    _install_cors_middleware,
    _install_origin_learning_middleware,
    _install_security_middleware,
    _make_channels_writer,  # noqa: F401 — re-export for test importers
    _make_chat_refresh_fn,
    _make_config_swap_fn,
    _mount_routes,
    _mount_ui_static,
    _repo_agents_dir,  # noqa: F401 — re-export for external importers
)
from corlinman_server.gateway.lifecycle.bootstrap_constants import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    SIGTERM_EXIT_CODE,
    _emit_py_config_drop,
    _identity_sweep_loop,
    list_default_scheduler_jobs,
)

# Phase 5: the module-level C2 wiring helpers moved to a sibling leaf. They
# are re-exported here so ``build_app`` / ``_lifespan`` keep calling them
# off this module, and so external importers
# (``tests/test_gf_c2_wiring.py`` does
# ``from ...entrypoint import _wire_c2_handles`` and CALLS it) keep
# resolving against this module's namespace.
from corlinman_server.gateway.lifecycle.c2_wiring import (
    _build_agent_runner_fn,  # noqa: F401 — re-export
    _wire_c2_handles,
    _wire_plugin_hotload,
)
from corlinman_server.gateway.lifecycle.cli_helpers import (
    _build_parser,
    _lazy_import,
    _resolve_bind,
    _resolve_config_path,
    _resolve_data_dir,
    _should_run_legacy_migration,
)
from corlinman_server.gateway.lifecycle.config_loading import (
    _load_config,
    _start_config_watcher,
    _wire_status_links,
)
from corlinman_server.gateway.lifecycle.config_resolve import (
    _extract_section,
)
from corlinman_server.gateway.lifecycle.legacy_migration import (
    migrate_legacy_data_files,
)
from corlinman_server.gateway.lifecycle.scheduler_integration import (
    DEFAULT_UPDATE_CHECK_JOB_NAME,
    _effective_scheduler_config,
    _register_default_darwin_curate_job,
    _register_default_evolution_engine_job,
    _register_default_evolution_shadow_job,
    _register_default_persona_decay_job,
    _register_default_persona_life_advance_job,
    _register_default_update_check_job,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Lazy-import helper for sibling modules
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Shared config / scheduler-job readers retained in this module.
#
# The default scheduler-job *registration* helpers (W2.2 update-check,
# W3 v2.1 darwin-curate) and the config→SchedulerJob conversion now live
# in :mod:`corlinman_server.gateway.lifecycle.scheduler_integration`.
# ``_extract_section`` stays here because ~15 sibling lifecycle helpers
# read config sections through it; ``list_default_scheduler_jobs`` is part
# of this module's public ``__all__`` surface and is re-exported from
# :mod:`corlinman_server.gateway.lifecycle.bootstrap_constants`.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# gap-fill v1.15 — CONTRACT C2 wiring spine + identity sweep
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------


def build_app(
    *,
    config_path: Path | None = None,
    data_dir: Path | None = None,
) -> Any:
    """Build the FastAPI app + AppState and wire every sibling module.

    Returns a :class:`fastapi.FastAPI` instance ready to be served by
    uvicorn. The app exposes ``app.state.corlinman_state`` for tests /
    middleware that need the shared handle.

    Sibling agents that haven't landed yet just log a warning and skip
    their wiring step — the app still starts (in degraded mode) so the
    integration step can roll forward iteratively.
    """
    try:
        from fastapi import FastAPI
    except ImportError as exc:  # pragma: no cover — fastapi is a runtime dep
        raise RuntimeError(
            "fastapi is required for the gateway entrypoint; "
            "add it to corlinman-server's dependencies"
        ) from exc

    cfg = _load_config(config_path)
    resolved_data_dir = data_dir or _resolve_data_dir(None, cfg)

    # Stamp the boot-resolved dir onto the (stateless) /v1/files route so
    # the chat file store lives in the SAME tree as the journal / session
    # stores even when the dir came from --data-dir / [server].data_dir
    # rather than $CORLINMAN_DATA_DIR (W3 review follow-up).
    try:
        from corlinman_server.gateway.routes import files as files_route

        files_route.configure_data_dir(resolved_data_dir)
    except ImportError:  # pragma: no cover — routes are a runtime dep
        pass

    # Phase 4 W1 4-1A Item 5: one-shot legacy data-file migration. Gated
    # on tenants config; default-off for back-compat.
    if _should_run_legacy_migration(cfg):
        try:
            migrate_legacy_data_files(resolved_data_dir)
        except OSError as exc:
            logger.warning(
                "gateway.legacy_migration.failed",
                data_dir=str(resolved_data_dir),
                error=str(exc),
            )

    # Feature C last-mile: re-emit the JSON drop so any in-process
    # consumer that mtime-watches CORLINMAN_PY_CONFIG sees a fully-formed
    # registry from boot.
    _emit_py_config_drop(cfg)

    state = _build_state(cfg, resolved_data_dir)

    # Resolve the on-disk path the admin-seed routine writes to / reads
    # back from. Cached on the FastAPI app so the lifespan handler can
    # re-use it after :func:`_mount_routes` already stamped it onto the
    # ``AdminState``. We compute it eagerly here so a missing
    # ``[admin]`` block still has a target path on first boot.
    admin_config_path = resolve_admin_config_path(
        cli_config_path=config_path, data_dir=resolved_data_dir
    )

    @asynccontextmanager
    async def _lifespan(app: Any):  # type: ignore[no-untyped-def]
        # Seed default ``admin``/``root`` credentials before the sibling
        # bootstraps fire — admin routes that load credentials lazily
        # (services / evolution) must see the resolved hash. The
        # ``AdminState`` was already registered with the singleton
        # during ``_mount_routes`` so we mutate it in place; FastAPI
        # only starts accepting requests after this coroutine yields.
        admin_a_state = getattr(app.state, "corlinman_admin_a_state", None)
        admin_b_state = getattr(app.state, "corlinman_admin_b_state", None)
        try:
            seeded = await ensure_admin_credentials(
                config_path=admin_config_path
            )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("gateway.admin_seed.failed", error=str(exc))
            seeded = None

        if admin_a_state is not None and seeded is not None:
            admin_a_state.admin_username = seeded.username
            admin_a_state.admin_password_hash = seeded.password_hash
            admin_a_state.config_path = seeded.config_path
            admin_a_state.must_change_password = seeded.must_change_password

        # SEC-007: mirror the seeded ``must_change_password`` flag onto the
        # admin-B state so the shared ``_auth_shim`` gate fires uniformly
        # across both route bundles. Admin-B's state owns its own copy
        # (rather than reaching back into the admin-A singleton) so test
        # fixtures that mount admin-B in isolation can't accidentally
        # inherit a leftover must_change flag from a sibling test.
        if admin_b_state is not None and seeded is not None:
            if hasattr(admin_b_state, "must_change_password"):
                admin_b_state.must_change_password = seeded.must_change_password

        # R1-001 security fix: open the multi-tenant ``tenants.sqlite``
        # admin DB and rebind it onto the api-key middleware state
        # installed during ``build_app``. Without this rebind the
        # middleware fails closed (401 ``admin_db_not_configured``);
        # rebinding lets minted tenant API keys actually authenticate
        # against ``/v1/*``. Also stamped onto ``admin_a_state.admin_db``
        # so the existing ``/admin/api-keys`` + ``/admin/tenants`` admin
        # routes (gated by the admin-auth + UI cookie path) can mint /
        # list keys against the same handle. Best-effort: a missing
        # ``tenants.sqlite`` (e.g. read-only data dir) leaves the
        # middleware in its fail-closed state — the gateway still boots
        # but ``/v1/*`` returns 401 ``admin_db_not_configured`` until
        # the operator fixes the data dir. Closed in the lifespan-exit
        # ``finally`` block below.
        if resolved_data_dir is not None:
            try:
                from corlinman_server.tenancy import AdminDb

                _admin_db = await AdminDb.open(
                    resolved_data_dir / "tenants.sqlite"
                )
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "gateway.admin_db.open_failed",
                    path=str(resolved_data_dir / "tenants.sqlite"),
                    error=str(exc),
                )
                _admin_db = None

            if _admin_db is not None:
                # Rebind onto the middleware state so the live AdminDb
                # is what every ``/v1/*`` request gets verified against.
                api_key_state = getattr(
                    app.state, "api_key_auth", None
                )
                if api_key_state is not None:
                    api_key_state.admin_db = _admin_db
                if admin_a_state is not None:
                    admin_a_state.admin_db = _admin_db
                # Stash on app.state so the teardown finally-block (and
                # any future code) can resolve it without a second open.
                app.state.corlinman_admin_db = _admin_db
                logger.info(
                    "gateway.admin_db.opened",
                    path=str(resolved_data_dir / "tenants.sqlite"),
                )

        # Open the persona store (async) + seed builtin Grantley on
        # first boot. Best-effort — failure leaves persona_store=None
        # and the /admin/personas + /admin/channels/qq/humanlike routes
        # return 503 ``persona_store_missing`` instead of crashing the
        # gateway.
        if admin_a_state is not None and resolved_data_dir is not None:
            try:
                from corlinman_server.persona import (
                    PersonaStore,
                    seed_builtin_personas,
                )

                _ps = await PersonaStore.open(
                    resolved_data_dir / "personas.sqlite"
                )
                await seed_builtin_personas(_ps)
                admin_a_state.persona_store = _ps
                logger.info("gateway.persona_store.opened")
            except Exception as exc:  # pragma: no cover — best-effort
                logger.warning(
                    "gateway.persona_store.init_failed", error=str(exc)
                )

            # W1 Persona Studio: companion asset store for emoji +
            # reference image packs. Filesystem layout lives under
            # ``<data_dir>/personas/<persona_id>/{emoji,reference}/``;
            # metadata in ``persona_assets.sqlite`` next to the
            # main personas DB. Best-effort — failure leaves the
            # asset routes returning 503 but bare persona CRUD works.
            try:
                from corlinman_server.persona import PersonaAssetStore

                _pas = await PersonaAssetStore.open(
                    resolved_data_dir / "persona_assets.sqlite",
                    resolved_data_dir / "personas",
                )
                admin_a_state.persona_asset_store = _pas
                logger.info("gateway.persona_asset_store.opened")
            except Exception as exc:  # pragma: no cover — best-effort
                logger.warning(
                    "gateway.persona_asset_store.init_failed",
                    error=str(exc),
                )

        # gap-fill v1.15 — CONTRACT C2 wiring spine.
        #
        # Construct + publish the six C2 handles onto the AppState so the
        # agent servicer (wire-A) + scheduler resolve them via getattr.
        # Every branch is independently best-effort: a failure leaves the
        # slot ``None`` and the consumer degrades (memory-free chat, no
        # persona placeholders, identity routes 503, scheduler run_agent
        # ``runner_not_registered``, no hooks). Boot never crashes here.
        # The identity sweep task is scheduled later (once ``background``
        # exists) so the lifespan-exit ``finally`` cancels + awaits it.
        if resolved_data_dir is not None:
            await _wire_c2_handles(
                app, state, admin_a_state, resolved_data_dir, cfg
            )

        # W1.3 — task-observability surface. Open the per-turn journal
        # the agent servicer also opens lazily on first chat, and
        # construct one :class:`JournalBackedEmitter` for the whole
        # gateway. The emitter is then wired into:
        #
        # * the admin_b state (so the SSE replay + cost routes can
        #   resolve it from the singleton);
        # * ``app.state`` (so other lifespan code / tests can introspect
        #   it without going through routes_admin_b);
        # * the existing AppState bag (so the agent servicer + runner
        #   pool can pick it up when they construct ReasoningLoops).
        #
        # All best-effort — a missing AgentJournal / events module logs
        # a warning and the gateway still boots, with the SSE / replay /
        # cost routes returning typed 503 ``observability_disabled``.
        observability_journal: Any | None = None
        observability_emitter: Any | None = None
        if resolved_data_dir is not None:
            try:
                from corlinman_server.agent_journal import AgentJournal
                from corlinman_server.gateway.observability import (
                    JournalBackedEmitter,
                    LiveSubagentRegistry,
                )

                observability_journal = await AgentJournal.open_from_env(
                    resolved_data_dir / "agent_journal.sqlite"
                )
                # W2.x — live registry of INLINE subagents, fed off the
                # emitter's subagent-lifecycle envelopes so the
                # /admin/subagents overview shows turn-spawned children too
                # (background children already get durable store rows).
                live_subagent_registry = LiveSubagentRegistry()
                observability_emitter = JournalBackedEmitter(
                    observability_journal,
                    subagent_observer=live_subagent_registry.observe,
                )

                # Publish onto AdminState so the W1.3 admin routes can
                # find both handles via ``get_admin_state()``.
                if admin_b_state is not None:
                    admin_b_state.journal = observability_journal
                    admin_b_state.event_emitter = observability_emitter
                    admin_b_state.live_subagent_registry = (
                        live_subagent_registry
                    )
                    # Bridge the AppState log broadcaster onto the
                    # admin-B state under the field name the
                    # /admin/logs/stream route reads (``log_broadcast``,
                    # not ``log_broadcaster``). Without this the route
                    # returns 503 ``logs_disabled`` even when the
                    # broadcaster was installed during _build_state.
                    bcaster = getattr(state, "log_broadcaster", None)
                    if bcaster is not None and getattr(
                        admin_b_state, "log_broadcast", None
                    ) is None:
                        try:
                            admin_b_state.log_broadcast = bcaster
                        except (AttributeError, TypeError):  # pragma: no cover
                            pass

                # Publish onto app.state for tests + future producers
                # that need the same shared emitter.
                app.state.corlinman_journal = observability_journal
                app.state.corlinman_event_emitter = observability_emitter

                # Publish onto AppState so the agent servicer (mounted
                # via gateway.services / gateway.grpc) can pick up the
                # same emitter on construction. ``extras`` is the
                # documented free-form bag — attach by a stable key so
                # downstream consumers can probe for it.
                extras = getattr(state, "extras", None)
                if isinstance(extras, dict):
                    extras["event_emitter"] = observability_emitter
                    extras["journal"] = observability_journal
                # Also expose as first-class attributes so a duck-typed
                # consumer (``getattr(state, "event_emitter", None)``)
                # finds it without reaching into ``.extras``.
                with suppress(AttributeError, TypeError):
                    state.event_emitter = observability_emitter
                    state.journal = observability_journal

                logger.info(
                    "gateway.observability.emitter_installed",
                    journal=str(resolved_data_dir / "agent_journal.sqlite"),
                )
            except Exception as exc:  # pragma: no cover — best-effort
                logger.warning(
                    "gateway.observability.init_failed", error=str(exc)
                )

        # W1.1: GitHub-releases update checker. Best-effort wire — a
        # missing system module / unwritable data dir logs at WARN and
        # the gateway boots clean; the /admin/system/* routes then
        # return a typed 503 ``update_checker_disabled`` envelope.
        # Scheduler wiring belongs to W2.2; here we only construct the
        # checker + publish it onto AdminState so the admin routes can
        # resolve it.
        if resolved_data_dir is not None:
            try:
                from corlinman_server.system import (
                    SystemUpdateCheckConfig,
                    UpdateChecker,
                )

                cfg_dict: dict[str, Any] = {}
                if isinstance(cfg, dict):
                    system_section = cfg.get("system")
                    if isinstance(system_section, dict):
                        update_section = system_section.get("update_check")
                        if isinstance(update_section, dict):
                            cfg_dict = update_section
                update_cfg = SystemUpdateCheckConfig.from_mapping(cfg_dict)

                if update_cfg.enabled:
                    update_checker = UpdateChecker(
                        config=update_cfg,
                        cache_path=resolved_data_dir / ".update_check.json",
                    )
                    if admin_b_state is not None:
                        admin_b_state.update_checker = update_checker
                    app.state.corlinman_update_checker = update_checker
                    logger.info(
                        "gateway.system.update_checker_installed",
                        repo=update_cfg.repo,
                        interval_hours=update_cfg.interval_hours,
                        include_prereleases=update_cfg.include_prereleases,
                    )

                    # W2.2: register the default cron job that polls the
                    # checker on the same rhythm as the configured
                    # interval. The job uses a ``run_tool``-shaped
                    # JobAction pointing at the ``system.update_check``
                    # builtin registered in
                    # :mod:`corlinman_server.scheduler.builtins`. We
                    # *don't* mutate the loaded TOML — the admin
                    # ``GET /admin/scheduler/jobs`` route reads jobs
                    # straight off the config snapshot, so a default
                    # we stash here surfaces only to the scheduler
                    # runtime (once spawned) and to the test surface
                    # via :func:`list_default_scheduler_jobs`.
                    #
                    # Operator override wins: when an explicit
                    # ``[[scheduler.jobs]] name = "system.update_check"``
                    # is present in the config we leave the default
                    # off so the operator's cron expression / timezone
                    # / action choice is the one the scheduler runs.
                    _register_default_update_check_job(
                        app, cfg, update_cfg.interval_hours
                    )
                else:
                    logger.info(
                        "gateway.system.update_checker_disabled_by_config"
                    )
            except Exception as exc:  # pragma: no cover — best-effort
                logger.warning(
                    "gateway.system.update_checker_init_failed",
                    error=str(exc),
                )

        # W3 v2.1 — schedule the daily darwin rubric scan in parallel
        # with the update-check job. Independent best-effort: a
        # registration failure here must not block the gateway boot.
        try:
            _register_default_darwin_curate_job(app, cfg)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "gateway.evolution.darwin_curate_job.init_failed",
                error=str(exc),
            )

        # R2 persona-liveness — schedule the hourly mood/fatigue decay
        # sweep so persona state actually drifts toward baseline instead
        # of being frozen forever. Independent best-effort, same as above.
        try:
            _register_default_persona_decay_job(app, cfg)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "gateway.persona.decay_job.init_failed",
                error=str(exc),
            )

        # R8 PASSIVE (L2) — default-off daily EvolutionEngine run-once pass.
        # Only registers the cron job when [evolution.engine] enabled = true
        # in the gateway config; the builtin is always wired by the builtins
        # package import. Independent best-effort, same as above.
        try:
            _register_default_evolution_engine_job(app, cfg)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "gateway.evolution.engine_job.init_failed",
                error=str(exc),
            )

        # R8 PASSIVE (L3) — default-off daily ShadowTester pass. Only
        # registers the cron job when [evolution.shadow] enabled = true in
        # the gateway config; the builtin is always wired by the builtins
        # package import. Independent best-effort, same as above.
        try:
            _register_default_evolution_shadow_job(app, cfg)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "gateway.evolution.shadow_job.init_failed",
                error=str(exc),
            )

        # R3 autonomous life-advance — default-off daily beat. Only
        # registers the cron job when [persona.life_advance] enabled = true
        # in the gateway config; the builtin is always wired by the
        # builtins package import.
        try:
            _register_default_persona_life_advance_job(app, cfg)
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "gateway.persona.life_advance_job.init_failed",
                error=str(exc),
            )

        # W1.3 (one-click upgrade) — wire the audit log + the runtime-
        # mode-appropriate upgrader. Both are best-effort; the
        # ``/admin/system/upgrade*`` routes degrade to typed 503
        # (``upgrader_unavailable``) when either piece is missing, and
        # the ``/admin/system/audit`` route silently returns an empty
        # page when no log is wired.
        #
        # Mode detection precedence:
        #   1. ``CORLINMAN_RUNTIME_MODE`` env var (set by install.sh's
        #      install_native + the docker-compose template).
        #   2. ``/.dockerenv`` presence (we're clearly in a container).
        #   3. ``"unknown"`` — the upgrade endpoints short-circuit to
        #      503 so the operator can still use the copy-paste
        #      ``/admin/system/upgrade-commands`` fallback.
        if resolved_data_dir is not None:
            try:
                from corlinman_server.system import SystemAuditLog

                audit_log_path = resolved_data_dir / "system-audit.log"
                audit_log = SystemAuditLog(audit_log_path)
                if admin_b_state is not None:
                    admin_b_state.audit_log = audit_log
                app.state.corlinman_audit_log = audit_log
                logger.info(
                    "gateway.system.audit_log_installed",
                    path=str(audit_log_path),
                )
            except Exception as exc:  # pragma: no cover — best-effort
                logger.warning(
                    "gateway.system.audit_log_init_failed", error=str(exc)
                )
                audit_log = None

            mode_raw = os.environ.get("CORLINMAN_RUNTIME_MODE", "")
            mode = mode_raw.strip().lower() if isinstance(mode_raw, str) else ""
            if mode not in {"docker", "native"}:
                mode = "unknown"
            if mode == "unknown" and Path("/.dockerenv").exists():
                mode = "docker"

            try:
                from corlinman_server.system.upgrader import (  # type: ignore[import-not-found]
                    UpgradeStateStore,
                    resolve_upgrader,
                )

                upgrade_state_store = UpgradeStateStore(
                    resolved_data_dir / ".upgrade-state.json"
                )
                # NOTE: no upgrader __init__ accepts ``audit_log`` (the audit
                # log is installed separately on app.state / admin_b_state
                # above); passing it here raised a TypeError that silently
                # disabled the native upgrader in prod. ``data_dir`` is routed
                # to the native upgrader only (DockerUpgrader derives its paths
                # from the container) inside resolve_upgrader.
                upgrader = resolve_upgrader(
                    mode,
                    store=upgrade_state_store,
                    data_dir=resolved_data_dir,
                )
                if upgrader is not None:
                    if admin_b_state is not None:
                        admin_b_state.upgrader = upgrader
                    app.state.corlinman_upgrader = upgrader
                    logger.info(
                        "gateway.system.upgrader_installed", mode=mode
                    )
                else:
                    logger.info(
                        "gateway.system.upgrader_disabled_for_mode",
                        mode=mode,
                    )
            except ImportError as exc:
                # W1.1/W1.2 not landed yet — degrade cleanly.
                logger.warning(
                    "gateway.system.upgrader_module_missing", error=str(exc)
                )
            except Exception as exc:  # pragma: no cover — best-effort
                logger.warning(
                    "gateway.system.upgrader_init_failed",
                    mode=mode,
                    error=str(exc),
                )

        # W1.3 (multi-agent): background subagent dispatch surface.
        #
        # Owns the persistent :class:`SubagentTaskStore` (atomic JSON at
        # ``$DATA_DIR/.subagent-state.json``) and an
        # :class:`AsyncSubagentDispatcher` published onto AdminState so
        # the ``/admin/subagents*`` routes resolve it via
        # ``get_admin_state()``, and so the persistent store boots (its
        # D3 orphan-reconcile runs here on every restart). The dispatcher
        # is constructed with a ``run_child_factory`` that intentionally
        # raises: end-to-end BACKGROUND dispatch is NOT wired (the servicer
        # never threads this dispatcher into the spawn tool path), so the
        # model-facing ``subagent_spawn`` schema deliberately no longer
        # advertises ``run_in_background`` (D4). The factory's raise is the
        # belt-and-braces backstop for any hand-crafted background request
        # that slips through: ``_run`` folds it into a clean ``failed`` row.
        # Both pieces are best-effort: a failure leaves the routes serving
        # a typed 503 ``subagent_dispatcher_unavailable``.
        if resolved_data_dir is not None:
            try:
                from corlinman_server.system.subagent import (
                    AsyncSubagentDispatcher,
                    SubagentRequest,
                    SubagentTaskStore,
                    default_persist_path,
                )

                subagent_store = SubagentTaskStore(
                    default_persist_path(resolved_data_dir)
                )

                async def _unwired_run_child_factory(
                    req: SubagentRequest,
                ) -> Any:
                    # Background dispatch is intentionally not wired (D4): a
                    # real factory would close over the supervisor + agent
                    # registry + provider and be threaded into the servicer's
                    # spawn path, which it is not. Until that lands, this
                    # raises; the dispatcher's :meth:`_run` catches it and
                    # flips the row to ``failed``. The model-facing schema
                    # does not advertise ``run_in_background``, so this path
                    # is only reachable by a hand-crafted background request.
                    raise RuntimeError(
                        "subagent background dispatch is not wired; "
                        "run_in_background is not advertised on the "
                        "subagent_spawn schema (see tool_wrapper D4)"
                    )

                # W3.1: thread the existing one-click-upgrade audit log
                # into the dispatcher so background subagent lifecycle
                # transitions surface on /admin/system Audit alongside
                # upgrades + credential rotations. The audit log is
                # `app.state.corlinman_audit_log` when wiring succeeded
                # earlier in this same block; otherwise None (best-effort).
                _audit_log = getattr(
                    app.state, "corlinman_audit_log", None
                )
                # Thread the operator-configured per-tenant ceiling from
                # ``[subagent] max_concurrent_per_tenant`` into the
                # dispatcher. Previously the dispatcher was built without
                # this kwarg, so it silently fell back to the hardcoded
                # ``DEFAULT_MAX_CONCURRENT_PER_TENANT`` and any value the
                # operator set in config had no effect. Only forward a
                # positive int; anything else (missing / 0 / non-numeric /
                # bool) leaves the dispatcher on its default.
                _subagent_cfg = _extract_section(cfg, "subagent")
                _max_concurrent = _extract_section(
                    _subagent_cfg, "max_concurrent_per_tenant"
                )
                _dispatcher_kwargs: dict[str, Any] = {
                    "store": subagent_store,
                    "run_child_factory": _unwired_run_child_factory,
                    "journal": observability_journal,
                    "audit_log": _audit_log,
                }
                if (
                    isinstance(_max_concurrent, int)
                    and not isinstance(_max_concurrent, bool)
                    and _max_concurrent > 0
                ):
                    _dispatcher_kwargs["max_concurrent_per_tenant"] = (
                        _max_concurrent
                    )
                subagent_dispatcher = AsyncSubagentDispatcher(
                    **_dispatcher_kwargs
                )
                if admin_b_state is not None:
                    admin_b_state.subagent_store = subagent_store
                    admin_b_state.subagent_dispatcher = subagent_dispatcher
                app.state.corlinman_subagent_store = subagent_store
                app.state.corlinman_subagent_dispatcher = (
                    subagent_dispatcher
                )
                logger.info(
                    "gateway.subagent.dispatcher_installed",
                    persist=str(
                        default_persist_path(resolved_data_dir)
                    ),
                )
            except Exception as exc:  # pragma: no cover — best-effort
                logger.warning(
                    "gateway.subagent.dispatcher_init_failed",
                    error=str(exc),
                )

        # W1.3 (skill hub): wire the ClawHubClient + the in-process
        # install task store onto admin_b. Both are best-effort — a
        # failure here just means the ``/admin/skills/hub/*`` routes
        # collapse to their offline envelopes (search/featured return
        # ``offline: true``; install POST returns a typed 503). The
        # client owns an httpx.AsyncClient + TTL cache and must be
        # closed cleanly in the lifespan teardown so the WAL of its
        # cache file is flushed.
        if admin_b_state is not None:
            try:
                from corlinman_server.gateway.routes_admin_b.marketplace._skills_lib import (
                    SkillInstallTaskStore,
                )

                # ``ClawHubClient`` doesn't take an audit log directly
                # (the installer writes the ``skill.installed`` rows;
                # the client only does anonymous read GETs). The audit
                # log is already on ``admin_b_state.audit_log`` from
                # earlier in this same block, so the install routes
                # pick it up through state when they call into the
                # installer.
                # Marketplace source (GitHub registry by default). The
                # same GitHub source backs all three markets: the skills
                # tab is served either by the legacy ClawHubClient
                # (``default_source = "clawhub"``) or by a GitHub-backed
                # adapter presenting the same client surface — so the
                # ``/admin/skills/hub/*`` routes + installer are unchanged
                # — while the MCP + plugin markets read this same source
                # off ``admin_b_state.extras["marketplace_source"]``.
                from corlinman_server.system.marketplace import (
                    load_marketplace_config,
                )
                from corlinman_server.system.marketplace.accel import (
                    GithubAccelerator,
                )
                from corlinman_server.system.marketplace.github_source import (
                    GitHubSource,
                )
                from corlinman_server.system.marketplace.skill_adapter import (
                    SkillHubSourceAdapter,
                )
                from corlinman_server.system.skill_hub import (
                    ClawHubClient,
                )

                _mp_cfg = load_marketplace_config(state.config)
                marketplace_source = GitHubSource(
                    repo=_mp_cfg.registry_repo,
                    ref=_mp_cfg.registry_ref,
                    accel=GithubAccelerator(_mp_cfg.accel),
                    token=_mp_cfg.github_token,
                )
                admin_b_state.extras["marketplace_source"] = marketplace_source
                app.state.corlinman_marketplace_source = marketplace_source

                clawhub_client: Any
                if (
                    _mp_cfg.default_source == "clawhub"
                    and _mp_cfg.clawhub_enabled
                ):
                    clawhub_client = ClawHubClient()
                else:
                    clawhub_client = SkillHubSourceAdapter(marketplace_source)
                skill_install_store = SkillInstallTaskStore()
                admin_b_state.clawhub_client = clawhub_client
                admin_b_state.skill_install_store = skill_install_store
                app.state.corlinman_clawhub_client = clawhub_client
                app.state.corlinman_skill_install_store = skill_install_store
                logger.info("gateway.skill_hub.client_installed")
            except ImportError as exc:
                # W1.1 / W1.2 sibling agents haven't landed yet — degrade
                # cleanly so the rest of the boot continues.
                logger.warning(
                    "gateway.skill_hub.client_module_missing",
                    error=str(exc),
                )
            except Exception as exc:  # pragma: no cover — best-effort
                logger.warning(
                    "gateway.skill_hub.client_init_failed",
                    error=str(exc),
                )

        # W5.0: open the evolution sqlite + attach the curator / signals
        # repos to admin_b (the /admin/curator/* routes read them from
        # there) and to admin_a (W4.5 applier surfaces consult admin_a's
        # ``signals_repo`` / ``skill_registry_factory`` slots). All
        # best-effort — a sqlite open failure logs at WARN and the
        # gateway still boots, with the curator routes returning their
        # typed 503 envelopes instead.
        evolution_store: Any | None = None
        signals_repo: Any | None = None
        curator_state_repo: Any | None = None
        evolution_db_path = resolved_data_dir / "evolution.sqlite"
        try:
            from corlinman_evolution_store import (
                CuratorStateRepo,
                EvolutionStore,
                SignalsRepo,
            )

            # ``EvolutionStore.open`` is the async classmethod that
            # creates parents (sqlite makes the file; we make the dir).
            evolution_db_path.parent.mkdir(parents=True, exist_ok=True)
            evolution_store = await EvolutionStore.open(evolution_db_path)
            # The repos share the store's underlying aiosqlite
            # connection — there are no ``store.signals_repo()`` /
            # ``store.curator_state_repo()`` accessors today, so we
            # construct them directly off ``store.conn``.
            signals_repo = SignalsRepo(evolution_store.conn)
            curator_state_repo = CuratorStateRepo(evolution_store.conn)

            if admin_b_state is not None:
                admin_b_state.curator_state_repo = curator_state_repo
                admin_b_state.signals_repo = signals_repo
                # Re-expose the raw store on admin_b too — a couple of
                # legacy /admin/evolution routes look it up from there.
                admin_b_state.evolution_store = evolution_store

            if admin_a_state is not None:
                # Dataclass allows dynamic attribute writes; the
                # user-correction applier reads ``signals_repo`` /
                # ``skill_registry_factory`` from admin_a so its
                # background-review fork can resolve a per-profile
                # SkillRegistry view at correction time.
                admin_a_state.signals_repo = signals_repo
                factory = getattr(
                    admin_b_state, "skill_registry_factory", None
                )
                if factory is None and admin_a_state is not None:
                    # Fallback factory mirrors the one wired in
                    # _mount_routes — covers cases where admin_b isn't
                    # mounted but admin_a still wants to spawn reviews.
                    try:
                        from corlinman_skills_registry import (
                            SkillRegistry,
                        )

                        def _fallback_skill_registry(slug: str) -> Any:
                            skills_dir = (
                                resolved_data_dir
                                / "profiles"
                                / slug
                                / "skills"
                            )
                            skills_dir.mkdir(parents=True, exist_ok=True)
                            return SkillRegistry.load_from_dir(skills_dir)

                        factory = _fallback_skill_registry
                    except ImportError:  # pragma: no cover
                        factory = None
                admin_a_state.skill_registry_factory = factory

            # Stash the handle so the lifespan-exit ``finally`` can
            # close cleanly and external test code can introspect it.
            app.state._evolution_store = evolution_store
            app.state._evolution_signals_repo = signals_repo
            app.state._evolution_curator_state_repo = curator_state_repo
            logger.info(
                "gateway.evolution.store_opened",
                path=str(evolution_db_path),
            )
        except Exception as exc:  # pragma: no cover — defensive umbrella
            logger.warning(
                "gateway.evolution.store_open_failed",
                path=str(evolution_db_path),
                error=str(exc),
            )

        # Wire the RAG corpus store onto admin_b so /admin/rag/* (stats /
        # query / rebuild) + /admin/memory/decay/reset un-503. The Rust
        # gateway opened ``<data_dir>/kb.sqlite`` via
        # ``corlinman_vector::SqliteStore``; the Python port ships a
        # subset adapter (:class:`RagStore`) covering exactly the methods
        # those routes call. Best-effort — an unwritable data dir / open
        # failure leaves ``rag_store=None`` and the routes keep returning
        # their typed 503 (``rag_disabled`` / ``memory_admin_disabled``).
        # Closed in the lifespan-exit ``finally`` so the WAL is
        # checkpointed.
        if resolved_data_dir is not None:
            try:
                from corlinman_server.gateway.rag_store import RagStore

                kb_path = resolved_data_dir / "kb.sqlite"
                kb_path.parent.mkdir(parents=True, exist_ok=True)
                rag_store = await RagStore.open(kb_path)
                if admin_b_state is not None:
                    admin_b_state.rag_store = rag_store
                app.state.corlinman_rag_store = rag_store
                logger.info("gateway.rag.store_opened", path=str(kb_path))
            except Exception as exc:  # pragma: no cover — best-effort
                logger.warning(
                    "gateway.rag.store_open_failed",
                    path=str(resolved_data_dir / "kb.sqlite"),
                    error=str(exc),
                )

        grpc_mod = _lazy_import("corlinman_server.gateway.grpc")

        cancel = asyncio.Event()
        background: list[asyncio.Task[Any]] = []

        # gap-fill v1.15 — schedule the identity verification-phrase sweep
        # (CONTRACT C2). The store was wired by ``_wire_c2_handles`` above;
        # register the periodic ``sweep_expired_phrases`` loop here, now
        # that ``background`` exists, so the lifespan-exit ``finally``
        # cancels + awaits it on shutdown. No-op when no store was wired.
        _identity_store = getattr(state, "identity_store", None)
        if _identity_store is not None:
            sweep_task = asyncio.create_task(
                _identity_sweep_loop(_identity_store),
                name="gateway.identity.sweep_expired_phrases",
            )
            background.append(sweep_task)
            logger.info("gateway.identity.sweep_scheduled")

        # Parcel P14: build + connect the external MCP client manager
        # *before* the sibling-bootstrap loop, so ``services.bootstrap``
        # → ``build_tool_executor`` can bind ``mcp``-kind plugin dispatch
        # to live MCP servers. Best-effort: a missing package, no
        # ``[mcp]`` config, or an unreachable server degrades to "no MCP
        # tools" — the gateway still boots. Closed in the lifespan-exit
        # ``finally``.
        try:
            from corlinman_mcp_server import McpClientManager
            from corlinman_mcp_server.client_manager import McpServerSpec

            _mcp_manager = McpClientManager.from_config(state.config)

            # Marketplace-installed MCP servers persist across restarts in
            # ``<data_dir>/mcp_servers.sqlite``. Register every stored spec
            # *before* connect_all so enabled ones come up and disabled
            # (staged-but-not-enabled) ones stay registered-yet-idle.
            _mcp_store = None
            if resolved_data_dir is not None:
                try:
                    from corlinman_server.system.marketplace.mcp_store import (
                        McpServerStore,
                    )

                    _mcp_store = McpServerStore(
                        resolved_data_dir / "mcp_servers.sqlite"
                    )
                    for _row in _mcp_store.list():
                        try:
                            _spec = McpServerSpec.from_mapping(
                                _row.name,
                                {**_row.spec, "enabled": _row.enabled},
                            )
                            await _mcp_manager.add_server(_spec, replace=True)
                        except Exception as exc:  # pragma: no cover
                            logger.warning(
                                "gateway.mcp.store_spec_skipped",
                                server=_row.name,
                                error=str(exc),
                            )
                except Exception as exc:  # pragma: no cover — best-effort
                    logger.warning(
                        "gateway.mcp.store_open_failed", error=str(exc)
                    )

            await _mcp_manager.connect_all()
            state.extras["mcp_manager"] = _mcp_manager
            logger.info("gateway.mcp.manager_connected")

            # Light up the marketplace admin routes: the McpAdapter is the
            # seam the EXISTING /admin/plugins/{name}/{enable,disable,
            # restart} routes already call via extras["mcp_adapter"], and
            # the new /admin/mcp/* + /admin/plugins/market/* routes resolve
            # their stores + source off these same admin_b extras.
            if admin_b_state is not None:
                try:
                    from corlinman_server.gateway.routes_admin_b.marketplace.mcp_adapter import (
                        McpAdapter,
                    )

                    admin_b_state.extras["mcp_adapter"] = McpAdapter(
                        _mcp_manager, _mcp_store
                    )
                    if resolved_data_dir is not None:
                        from corlinman_server.system.marketplace.plugin_store import (
                            PluginStore,
                        )

                        _plugin_store = PluginStore(
                            resolved_data_dir / "plugins.sqlite"
                        )
                        admin_b_state.extras["plugin_store"] = _plugin_store
                        admin_b_state.extras["data_dir"] = resolved_data_dir
                        await _wire_plugin_hotload(
                            state,
                            admin_b_state,
                            _plugin_store,
                            resolved_data_dir,
                        )
                    logger.info("gateway.marketplace.wired")
                except Exception as exc:  # pragma: no cover — best-effort
                    logger.warning(
                        "gateway.marketplace.wire_failed", error=str(exc)
                    )
        except Exception as exc:
            logger.warning("gateway.mcp.manager_failed", error=str(exc))

        # Agent status-card channel links. Wire the corlinman-channels
        # feature ONCE here, *before* the sibling-bootstrap loop starts
        # the channels: each channel's reply path then appends a
        # "{public_url}/status/{token}" line so a chat user can tap
        # through to a read-only live status page. The minter is injected
        # as a closure (session_key -> signed token) so corlinman_channels
        # never has to import corlinman_server (import-linter layering).
        # No-op / links-off unless public_url is set AND the channels
        # feature flag is on — ``configure_status_links`` already guards.
        try:
            _wire_status_links(cfg, resolved_data_dir)
        except Exception as exc:  # pragma: no cover — best-effort
            logger.warning(
                "gateway.channels.status_links_wire_failed", error=str(exc)
            )

        # Generic sibling-bootstrap seam (see docs/contracts/runtime-
        # wiring.md §2). Each sibling module *may* export
        # ``bootstrap(state) -> None | Awaitable | list[asyncio.Task]``.
        # P0 made this list the single place new Wave-1 runtime modules
        # plug in: P1 (providers), P2/P3 (services — chat + channels),
        # and evolution all land here without editing the seam again.
        # The order is load-bearing: ``providers`` must boot before
        # ``services`` so the ChatService/registry attach points on
        # ``AppState`` are populated when the chat + channel bootstraps
        # read them.
        sibling_names = (
            "corlinman_server.gateway.providers",  # P1 — provider_registry
            "corlinman_server.gateway.services",   # P2/P3 — chat + channels
            "corlinman_server.gateway.evolution",  # evolution observer
        )
        for dotted in sibling_names:
            sibling = _lazy_import(dotted)
            if sibling is None:
                continue
            name = dotted.rsplit(".", 1)[-1]
            bootstrap = getattr(sibling, "bootstrap", None)
            if bootstrap is None:
                continue
            try:
                result = bootstrap(state)
                if isinstance(result, Awaitable):
                    result = await result
                # A bootstrap may hand back background tasks (channel
                # adapters, hot reloaders). Register them so the
                # lifespan-exit ``finally`` cancels + awaits them under
                # the same ``cancel`` event.
                if isinstance(result, asyncio.Task):
                    background.append(result)
                elif isinstance(result, (list, tuple)):
                    for item in result:
                        if isinstance(item, asyncio.Task):
                            background.append(item)
            except Exception as exc:  # pragma: no cover — sibling-owned
                logger.warning(
                    "gateway.sibling.bootstrap_failed",
                    sibling=name,
                    error=str(exc),
                )

        # Parcel P11: arm config hot-reload. The ConfigWatcher must boot
        # *after* the provider/services bootstraps so its first reload
        # re-applies onto a fully-wired AppState. Its debounce-loop task
        # is registered into ``background`` so the lifespan-exit
        # ``finally`` cancels + awaits it (which stops the fs observer +
        # SIGHUP handler) on shutdown.
        try:
            watcher_task = _start_config_watcher(app, state, config_path)
            if watcher_task is not None:
                background.append(watcher_task)
            # Bridge: publish the live ConfigWatcher onto admin_b_state so
            # ``POST /admin/config/reload`` (which reads
            # ``state.extras["config_watcher"]`` from the AdminState
            # singleton) can drive a manual reload.  Without this copy the
            # endpoint always returns 503 ``config_reload_disabled`` even
            # though a real watcher is running.
            _watcher_instance = getattr(state, "config_watcher", None)
            if _watcher_instance is not None and admin_b_state is not None:
                with suppress(AttributeError, TypeError):
                    admin_b_state.extras["config_watcher"] = _watcher_instance
            # Wire ``config_swap_fn`` UNCONDITIONALLY (even when the
            # fs-watcher is off, the default) so ``POST /admin/config``
            # publishes the operator's TOML edit to the live in-memory
            # snapshot and re-applies the idempotent providers/models
            # bootstraps — otherwise a save wrote disk but never reached the
            # running process (``_publish_snapshot`` no-op'd on a missing fn
            # while the UI still toasted success).
            if admin_b_state is not None:
                with suppress(AttributeError, TypeError):
                    admin_b_state.extras["config_swap_fn"] = _make_config_swap_fn(
                        app, state
                    )
                with suppress(AttributeError, TypeError):
                    admin_b_state.extras["chat_refresh_fn"] = _make_chat_refresh_fn(
                        state
                    )
                logger.debug(
                    "gateway.config_reload.swap_fn_wired",
                    path=str(config_path) if config_path else None,
                    watcher=_watcher_instance is not None,
                )
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "gateway.config_reload.watcher_start_failed", error=str(exc)
            )

        if grpc_mod is not None:
            serve = getattr(
                grpc_mod, "serve_placeholder_in_background", None
            )
            if serve is not None:
                try:
                    result = serve(state, cancel)
                    # ``serve_placeholder_in_background`` may hand back an
                    # already-scheduled Task (its name implies so) or a
                    # bare coroutine — accept either without double-wrap.
                    task = (
                        result
                        if isinstance(result, asyncio.Task)
                        else asyncio.create_task(result)
                    )
                    background.append(task)
                except Exception as exc:  # pragma: no cover — sibling-owned
                    logger.warning(
                        "gateway.grpc.bootstrap_failed", error=str(exc)
                    )

        # W5.0: wire the user-correction HookBus listener. Today no
        # other component constructs a shared HookBus in the gateway
        # boot path, so we build one here and publish it on
        # ``app.state.hook_bus`` for future producers (channels /
        # subagent supervisor / chat service) to reuse. The listener
        # itself only needs ``signals_repo`` and the applier callback;
        # missing either is an opt-out (we log + skip).
        user_correction_task: asyncio.Task[None] | None = None
        user_correction_applier: Any | None = None
        if signals_repo is not None:
            try:
                from corlinman_hooks import HookBus

                bus = getattr(app.state, "hook_bus", None)
                if bus is None:
                    # Capacity mirrors the default the observer / other
                    # subscribers expect — 256 events of slack per tier
                    # before a slow handler trips ``Lagged``.
                    bus = HookBus(capacity=256)
                    app.state.hook_bus = bus

                from corlinman_server.gateway.evolution import (
                    UserCorrectionApplier,
                    register_user_correction_listener,
                )

                def _resolve_provider(slug: str) -> tuple[Any, str]:
                    """Resolve ``(provider_instance, model_name)`` for a
                    profile. Today the gateway does not expose a stable
                    provider-lookup surface; we degrade to ``(None, "")``
                    and let ``UserCorrectionApplier`` short-circuit on
                    the resolver-failure gate. Wired here as a hook so
                    a sibling provider-wiring agent can later swap in
                    the real lookup without touching the listener.
                    """
                    return (None, "")

                # Closures over the just-attached admin_a slots — read
                # via getattr so a missing piece collapses to ``None``
                # rather than NameError. The applier's resolver
                # failure paths already log + gate gracefully.
                def _registry_for_profile(slug: str) -> Any:
                    fn = getattr(
                        admin_a_state, "skill_registry_factory", None
                    )
                    if fn is None:
                        raise RuntimeError("skill_registry_factory not wired")
                    return fn(slug)

                def _profile_root_for_profile(slug: str):
                    pstore = getattr(admin_a_state, "profile_store", None)
                    if pstore is None:
                        # Fall back to the conventional layout under
                        # ``<data_dir>/profiles/<slug>``.
                        return resolved_data_dir / "profiles" / slug
                    return Path(pstore.profiles_dir) / slug

                user_correction_applier = UserCorrectionApplier(
                    registry_for_profile=_registry_for_profile,
                    profile_root_for_profile=_profile_root_for_profile,
                    provider_for_profile=_resolve_provider,
                    rate_limit_seconds=30,
                    min_weight=0.7,
                )

                async def _on_signal(sig: Any) -> None:
                    # Fire-and-forget bridge — the listener already
                    # spawns ``asyncio.create_task`` around this
                    # callback, so a direct await is fine and keeps the
                    # ``last_fired`` map updates serialised.
                    await user_correction_applier.apply(sig)

                user_correction_task = register_user_correction_listener(
                    bus,
                    signals_repo,
                    on_signal=_on_signal,
                )
                background.append(user_correction_task)
                app.state._user_correction_applier = (
                    user_correction_applier
                )
                logger.info(
                    "gateway.evolution.user_correction_listener_registered"
                )
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "gateway.evolution.user_correction_listener_failed",
                    error=str(exc),
                )

        # W3 first-run-wizard contract D4 — restart broadcast.
        # Iterate every user that's pinned a "home channel" via
        # ``/sethome`` and emit a system-level restart notice for
        # each. The actual channel-send surface (TelegramSender /
        # OneBot action queue / Slack webhook) is owned by the
        # per-channel adapter and isn't directly addressable from
        # the entrypoint; we defer the send into a background task
        # so the lifespan doesn't block on per-channel availability,
        # and surface the planned broadcast on the structlog feed
        # so operators see the heads-up in the boot logs even when
        # the eventual outbound is still being wired up by a
        # follow-up wave.
        try:
            from corlinman_server import home_channel_store
            from corlinman_server.gateway.core.telemetry import (
                _pkg_version,
            )

            version_str = _pkg_version()
            homes_snapshot = home_channel_store.list_all_homes()
            if homes_snapshot:
                async def _broadcast_restart() -> None:
                    msg_body = (
                        f"🔄 服务器刚刚重启完成（v{version_str}）"
                    )
                    for row in homes_snapshot:
                        # Best-effort log — the structlog feed is
                        # fan-out by /admin/logs/stream so the
                        # operator sees the planned send the moment
                        # boot finishes. When a future wave wires
                        # the outbound channels handle onto
                        # AppState we'll route through that handle
                        # here instead.
                        logger.info(
                            "gateway.home_channel.restart_broadcast",
                            channel=row.channel,
                            account=row.account,
                            thread=row.thread,
                            sender=row.sender,
                            version=version_str,
                            message=msg_body,
                        )
                    logger.info(
                        "gateway.home_channel.restart_broadcast_complete",
                        homes=len(homes_snapshot),
                    )

                broadcast_task = asyncio.create_task(
                    _broadcast_restart(),
                    name="gateway.home_channel.restart_broadcast",
                )
                background.append(broadcast_task)
                logger.info(
                    "gateway.home_channel.restart_broadcast_scheduled",
                    homes=len(homes_snapshot),
                    version=version_str,
                )
            # Skip silently when no homes are registered — that's the
            # first-boot case before any user has issued /sethome.
        except Exception as exc:  # pragma: no cover — best-effort
            logger.warning(
                "gateway.home_channel.restart_broadcast_failed",
                error=str(exc),
            )

        # R4-F1 (CRITICAL): actually spawn the scheduler runtime.
        #
        # Rounds 1-3 fixed dispatch() routing (R3-002) but nothing ever
        # called ``scheduler.runner.spawn()``, so the per-job tick loops
        # were never created and the default cron jobs
        # (``system.update_check`` / ``evolution.darwin_curate``) never
        # fired — the prior FINAL_REPORT's "default jobs actually run"
        # claim was false. We build the effective job set (operator
        # ``[[scheduler.jobs]]`` + auto-registered defaults) and spawn it
        # under the shared ``cancel`` event so the lifespan-exit
        # ``finally`` cancels + awaits the tick tasks. The handle is
        # published on ``app.state`` + ``admin_b_state`` so the admin
        # "fire now" route triggers a job out-of-band via
        # ``SchedulerHandle.trigger()``. ``app.state`` is threaded into
        # every firing so ``run_tool`` builtins read a live state.
        try:
            sched_cfg = _effective_scheduler_config(app, cfg)
            # gap-fill (scheduler-runtime-jobs): we spawn a handle even when
            # there are zero config/default jobs so admin-created *runtime*
            # jobs (persisted in ``<data_dir>/scheduler_runtime_jobs.json``)
            # have a live :class:`SchedulerHandle` to register their tick
            # loops onto on boot — without this a process with only runtime
            # jobs would never fire them.
            _has_runtime_jobs = False
            try:
                _rt_path = (
                    resolved_data_dir / "scheduler_runtime_jobs.json"
                    if resolved_data_dir is not None
                    else None
                )
                _has_runtime_jobs = bool(_rt_path and _rt_path.is_file())
            except OSError:  # pragma: no cover — defensive
                _has_runtime_jobs = False
            if sched_cfg.jobs or _has_runtime_jobs:
                from corlinman_hooks import HookBus

                from corlinman_server.scheduler import spawn as _spawn_scheduler

                # gap-fill v1.15 (goals-cron): open the run-history store +
                # park it on app.state BEFORE spawn so each firing persists
                # an outcome row (``dispatch`` reads ``app_state.scheduler_
                # store``) and the per-job loop's missed-run catch-up can
                # read the last firing across restarts. Best-effort — a
                # store-open failure leaves catch-up + history off but the
                # scheduler still fires on schedule.
                if (
                    resolved_data_dir is not None
                    and getattr(app.state, "scheduler_store", None) is None
                ):
                    try:
                        from corlinman_server.scheduler import SchedulerStore

                        _sched_store = await SchedulerStore.open(
                            resolved_data_dir / "scheduler.sqlite"
                        )
                        app.state.scheduler_store = _sched_store
                        logger.info(
                            "gateway.scheduler.store_opened",
                            path=str(resolved_data_dir / "scheduler.sqlite"),
                        )
                    except Exception as exc:  # noqa: BLE001 — history optional
                        logger.warning(
                            "gateway.scheduler.store_open_failed",
                            error=str(exc),
                        )

                sched_bus = getattr(app.state, "hook_bus", None)
                if sched_bus is None:
                    sched_bus = HookBus(capacity=256)
                    app.state.hook_bus = sched_bus
                scheduler_handle = _spawn_scheduler(
                    sched_cfg, sched_bus, cancel, app_state=app.state
                )
                background.extend(scheduler_handle.tasks)
                app.state.corlinman_scheduler_handle = scheduler_handle
                if admin_b_state is not None:
                    admin_b_state.scheduler = scheduler_handle
                    # Publish ``app.state`` onto the admin extras so the
                    # scheduler routes can (a) mirror runtime-job metadata
                    # onto ``app_state.scheduler_job_metadata`` (the qzone
                    # builtin reads it at tick time) and (b) resolve the
                    # live handle for register/unregister on create / edit /
                    # pause / resume.
                    with suppress(AttributeError, TypeError):
                        admin_b_state.extras["app_state"] = app.state
                    # Rehydrate the persisted runtime-job overlay + register
                    # each enabled job's tick loop onto the fresh handle.
                    # Best-effort — a malformed sidecar leaves the overlay
                    # empty rather than aborting boot.
                    try:
                        from corlinman_server.gateway.routes_admin_b.infra._scheduler_lib import (
                            rehydrate_runtime_jobs_on_boot,
                        )

                        rehydrate_runtime_jobs_on_boot(admin_b_state)
                    except Exception as exc:  # pragma: no cover — best-effort
                        logger.warning(
                            "gateway.scheduler.runtime_rehydrate_failed",
                            error=str(exc),
                        )
                logger.info(
                    "gateway.scheduler.spawned",
                    jobs=[j.name for j in sched_cfg.jobs],
                )
            else:
                logger.info("gateway.scheduler.no_jobs")
        except Exception as exc:  # pragma: no cover — best-effort
            logger.warning("gateway.scheduler.spawn_failed", error=str(exc))

        try:
            yield
        finally:
            cancel.set()
            for task in background:
                task.cancel()
            for task in background:
                with suppress(asyncio.CancelledError, Exception):
                    await task
            # P14 teardown: close the external MCP client manager so
            # stdio child processes / ws connections are released.
            # Read ``extras`` defensively: a degraded boot uses
            # ``_DegradedAppState`` (``__slots__`` = config/data_dir, no
            # ``extras``), so an unguarded ``state.extras`` would raise
            # AttributeError out of this ``finally`` and abort every
            # remaining teardown step below — leaking the C2 sqlite stores.
            _extras = getattr(state, "extras", None)
            teardown_mcp_manager = (
                _extras.get("mcp_manager") if isinstance(_extras, dict) else None
            )
            if teardown_mcp_manager is not None:
                with suppress(Exception):
                    await cast(Any, teardown_mcp_manager).aclose()
                if isinstance(_extras, dict):
                    _extras.pop("mcp_manager", None)
            # W5.0 teardown: close the evolution sqlite cleanly so the
            # WAL file is checkpointed and tests don't leave stale
            # file handles open on Windows.
            store = getattr(app.state, "_evolution_store", None)
            if store is not None:
                try:
                    await store.close()
                except Exception as exc:  # pragma: no cover — defensive
                    logger.warning(
                        "gateway.evolution.store_close_failed",
                        error=str(exc),
                    )
                app.state._evolution_store = None
            # RAG-store teardown: close the kb.sqlite handle opened above so
            # its WAL is checkpointed and tests don't leak file descriptors.
            # Idempotent + safe when no store was wired.
            rag_store_handle = getattr(app.state, "corlinman_rag_store", None)
            if rag_store_handle is not None:
                try:
                    await rag_store_handle.close()
                except Exception as exc:  # pragma: no cover — defensive
                    logger.warning(
                        "gateway.rag.store_close_failed", error=str(exc)
                    )
                app.state.corlinman_rag_store = None
            # D12 teardown: cancel + await any in-flight background subagent
            # dispatch tasks BEFORE closing the journal they emit into, so a
            # shutdown doesn't orphan child-driving tasks against a
            # tearing-down provider / journal. Idempotent + safe when the
            # dispatcher was never wired.
            teardown_subagent_dispatcher = getattr(
                app.state, "corlinman_subagent_dispatcher", None
            )
            if teardown_subagent_dispatcher is not None:
                try:
                    await cast(Any, teardown_subagent_dispatcher).shutdown()
                except Exception as exc:  # pragma: no cover — defensive
                    logger.warning(
                        "gateway.subagent.dispatcher_shutdown_failed",
                        error=str(exc),
                    )

            # gap-fill v1.15 (C2) teardown: close the identity store +
            # runtime persona-state store + memory host opened by
            # ``_wire_c2_handles`` so their WAL files are checkpointed and
            # tests don't leak file descriptors. Each is best-effort +
            # idempotent (close suppresses its own errors / re-entry).
            for _attr, _label in (
                ("corlinman_identity_store", "identity.store"),
                ("corlinman_persona_state_store", "persona.state_store"),
                ("scheduler_store", "scheduler.store"),
            ):
                _handle = getattr(app.state, _attr, None)
                if _handle is not None:
                    closer = getattr(_handle, "close", None)
                    if closer is not None:
                        try:
                            res = closer()
                            if hasattr(res, "__await__"):
                                await res
                        except Exception as exc:  # pragma: no cover
                            logger.warning(
                                f"gateway.c2.{_label}.close_failed",
                                error=str(exc),
                            )
                    with suppress(AttributeError, TypeError):
                        setattr(app.state, _attr, None)
            _mem_host = getattr(state, "memory_host", None)
            if _mem_host is not None:
                _mem_close = getattr(_mem_host, "close", None) or getattr(
                    _mem_host, "aclose", None
                )
                if _mem_close is not None:
                    try:
                        res = _mem_close()
                        if hasattr(res, "__await__"):
                            await res
                    except Exception as exc:  # pragma: no cover
                        logger.warning(
                            "gateway.c2.memory_host.close_failed",
                            error=str(exc),
                        )
                with suppress(AttributeError, TypeError):
                    state.memory_host = None

            # W1.3 teardown: close the observability journal so its WAL
            # file is checkpointed before the process exits.
            obs_journal = getattr(app.state, "corlinman_journal", None)
            if obs_journal is not None:
                try:
                    await obs_journal.close()
                except Exception as exc:  # pragma: no cover — defensive
                    logger.warning(
                        "gateway.observability.journal_close_failed",
                        error=str(exc),
                    )
                app.state.corlinman_journal = None
                app.state.corlinman_event_emitter = None

            # W1.1 teardown: release the httpx client held by the
            # update checker. Safe when none was wired.
            update_checker_handle = getattr(
                app.state, "corlinman_update_checker", None
            )
            if update_checker_handle is not None:
                try:
                    await update_checker_handle.aclose()
                except Exception as exc:  # pragma: no cover — defensive
                    logger.warning(
                        "gateway.system.update_checker_close_failed",
                        error=str(exc),
                    )
                app.state.corlinman_update_checker = None

            # W1.3 (skill hub) teardown: release the httpx client + any
            # TTL cache file handles held by the ClawHubClient. Safe when
            # none was wired (a degraded boot or W1.1 not landed yet).
            clawhub_client_handle = getattr(
                app.state, "corlinman_clawhub_client", None
            )
            if clawhub_client_handle is not None:
                try:
                    await clawhub_client_handle.aclose()
                except Exception as exc:  # pragma: no cover — defensive
                    logger.warning(
                        "gateway.skill_hub.client_close_failed",
                        error=str(exc),
                    )
                app.state.corlinman_clawhub_client = None
                app.state.corlinman_skill_install_store = None

            # Marketplace teardown: release the GitHub source's httpx
            # client. In GitHub mode the skills adapter above shares this
            # same object, so it may already be closed — aclose is
            # idempotent. Safe when none was wired.
            marketplace_source_handle = getattr(
                app.state, "corlinman_marketplace_source", None
            )
            if marketplace_source_handle is not None:
                try:
                    await marketplace_source_handle.aclose()
                except Exception as exc:  # pragma: no cover — defensive
                    logger.warning(
                        "gateway.marketplace.source_close_failed",
                        error=str(exc),
                    )
                app.state.corlinman_marketplace_source = None

            # R1-001 teardown: close the AdminDb sqlite handle opened
            # above so the WAL file is checkpointed and tests don't
            # leak file descriptors between cases. Idempotent —
            # ``AdminDb.close`` already suppresses its own errors.
            admin_db_handle = getattr(
                app.state, "corlinman_admin_db", None
            )
            if admin_db_handle is not None:
                with suppress(Exception):
                    await admin_db_handle.close()
                app.state.corlinman_admin_db = None
                # Unbind from the middleware state too so a second
                # request after teardown gets the explicit
                # ``admin_db_not_configured`` 401 instead of crashing
                # on a closed sqlite connection. Best-effort —
                # post-teardown requests are unusual.
                api_key_state = getattr(
                    app.state, "api_key_auth", None
                )
                if api_key_state is not None:
                    api_key_state.admin_db = None
                admin_a_state_after = getattr(
                    app.state, "corlinman_admin_a_state", None
                )
                if admin_a_state_after is not None:
                    admin_a_state_after.admin_db = None

    app = FastAPI(lifespan=_lifespan)
    app.state.corlinman_state = state
    # ``get_app_state`` (gateway.core.state) + the runtime route handlers
    # (chat / models, see docs/contracts/runtime-wiring.md) resolve the
    # live AppState via ``app.state.corlinman``. Keep ``corlinman_state``
    # for back-compat; ``corlinman`` is the documented contract name.
    app.state.corlinman = state
    app.state.corlinman_config = cfg
    app.state.corlinman_data_dir = resolved_data_dir

    _install_cors_middleware(app)

    _install_origin_learning_middleware(app, cfg, resolved_data_dir)

    _install_security_middleware(app, cfg)

    # Mount every routes submodule. Each submodule exposes a different
    # composition surface (per the parallel-agent contracts); we wire them
    # individually here to keep entrypoint.py the single composition root.
    admin_a_state, admin_b_state = _mount_routes(
        app, state, admin_config_path=admin_config_path
    )
    # Stash the admin state handles on ``app.state`` so the lifespan
    # closure (defined above ``_mount_routes``'s call) can populate the
    # seeded credentials once :func:`ensure_admin_credentials` runs, and
    # so W5.0's evolution-store wiring can stamp the curator/signals
    # repos onto admin_b once the sqlite handle is open.
    app.state.corlinman_admin_a_state = admin_a_state
    app.state.corlinman_admin_b_state = admin_b_state
    app.state.corlinman_admin_config_path = admin_config_path

    # Liveness + readiness net: if no routes module mounted a ``/health``
    # path, expose one here. ``mode`` is computed from the live runtime
    # rather than hard-coded — ``ok`` once the Wave 1 attach points
    # (provider registry + chat service) are wired, ``degraded`` while
    # either slot is still unfilled. See docs/contracts/runtime-wiring.md.
    _have_health = any(
        getattr(r, "path", None) == "/health" for r in app.routes
    )
    if not _have_health:

        @app.get("/health")
        async def _health() -> dict[str, str]:
            rt = getattr(app.state, "corlinman", None)
            wired = (
                rt is not None
                and getattr(rt, "provider_registry", None) is not None
                and getattr(rt, "chat", None) is not None
            )
            return {
                "status": "ok",
                "mode": "ok" if wired else "degraded",
            }

    _mount_ui_static(app)

    return app


# ---------------------------------------------------------------------------
# CLI / uvicorn driver
# ---------------------------------------------------------------------------


async def _serve(args: argparse.Namespace) -> int:
    """Build the app and run uvicorn until SIGTERM/SIGINT."""
    # Telemetry init (best-effort — missing OTLP endpoint is a no-op
    # inside the helper).
    try:
        from corlinman_server.telemetry import init_telemetry, shutdown_telemetry

        init_telemetry()
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("gateway.telemetry.init_failed", error=str(exc))

        def shutdown_telemetry() -> None:  # type: ignore[misc]
            return None

    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover — uvicorn is a runtime dep
        raise RuntimeError(
            "uvicorn is required for the gateway entrypoint; "
            "add it to corlinman-server's dependencies"
        ) from exc

    config_path = _resolve_config_path(args.config)
    # Load the config up-front so ``[server].bind`` / ``[server].port`` /
    # ``[server].data_dir`` can serve as fallbacks below CLI / env when
    # resolving the bind address + data dir. ``build_app`` re-loads it
    # internally (it stays self-contained for the test surface); a startup
    # double-read of a small TOML is negligible.
    cfg = _load_config(config_path)
    data_dir = (
        Path(args.data_dir) if args.data_dir else _resolve_data_dir(None, cfg)
    )
    host, port = _resolve_bind(args.host, args.port, cfg)

    app = build_app(config_path=config_path, data_dir=data_dir)

    uv_config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level=args.log_level,
        loop="asyncio",
        lifespan="on",
    )
    server = uvicorn.Server(uv_config)

    # Wire SIGTERM/SIGINT to uvicorn's graceful-shutdown flag. uvicorn
    # installs its own handlers when run via ``uvicorn.run``; we use
    # ``Server.serve`` so we can return the right exit code.
    loop = asyncio.get_running_loop()
    received: list[str] = []

    def _on_signal(name: str) -> None:
        received.append(name)
        logger.info("gateway.shutdown.signal", signal=name)
        server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            # Windows / restricted envs — uvicorn's own signal hooks
            # will still trip; we just won't relay the name. Tests on
            # those platforms are not in scope.
            loop.add_signal_handler(sig, _on_signal, sig.name)

    logger.info("gateway.serve.start", host=host, port=port)
    await server.serve()
    logger.info("gateway.serve.stopped")

    shutdown_telemetry()
    return SIGTERM_EXIT_CODE if any(r == "SIGTERM" for r in received) else 0


def main(argv: list[str] | None = None) -> None:
    """Console-script entrypoint.

    Registered (when ``pyproject.toml`` is updated by the integration
    step) as ``corlinman-gateway = "corlinman_server.gateway.lifecycle.entrypoint:main"``.
    """
    args = _build_parser().parse_args(argv)
    try:
        code = asyncio.run(_serve(args))
    except KeyboardInterrupt:
        code = SIGTERM_EXIT_CODE
    sys.exit(code)


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_UPDATE_CHECK_JOB_NAME",
    "SIGTERM_EXIT_CODE",
    "build_app",
    "list_default_scheduler_jobs",
    "main",
]
