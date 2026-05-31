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
import importlib
import os
import signal
import sys
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import structlog

from corlinman_server.gateway.lifecycle.admin_seed import (
    ensure_admin_credentials,
    resolve_admin_config_path,
)
from corlinman_server.gateway.lifecycle.legacy_migration import (
    migrate_legacy_data_files,
)
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


# ---------------------------------------------------------------------------
# Lazy-import helper for sibling modules
# ---------------------------------------------------------------------------


def _lazy_import(dotted: str) -> Any | None:
    """Import ``dotted`` and return the module; ``None`` on ImportError.

    The siblings populated by parallel agents may not exist when this
    file is imported. Swallowing ``ImportError`` lets ``build_app`` boot
    in degraded mode without leaking partial-port state into a startup
    crash.
    """
    try:
        return importlib.import_module(dotted)
    except ImportError as exc:
        logger.warning(
            "gateway.sibling_missing",
            module=dotted,
            error=str(exc),
            detail=(
                "sibling submodule not present; gateway will boot in "
                "degraded mode without it"
            ),
        )
        return None


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _resolve_config_path(cli_value: str | None) -> Path | None:
    """``--config`` > ``$CORLINMAN_CONFIG`` > ``None``. Mirrors the
    Rust ``main::load_config`` precedence."""
    if cli_value:
        return Path(cli_value)
    env = os.environ.get("CORLINMAN_CONFIG")
    if env:
        return Path(env)
    return None


def _resolve_data_dir(cli_value: str | None) -> Path:
    """``--data-dir`` > ``$CORLINMAN_DATA_DIR`` > ``~/.corlinman`` >
    ``./.corlinman``. Mirrors ``corlinman_gateway::server::resolve_data_dir``."""
    if cli_value:
        return Path(cli_value)
    env = os.environ.get("CORLINMAN_DATA_DIR")
    if env:
        return Path(env)
    try:
        return Path.home() / ".corlinman"
    except (RuntimeError, OSError):
        return Path(".corlinman")


def _resolve_cors_origins() -> list[str]:
    """Parse the opt-in browser UI CORS allowlist."""
    raw = os.environ.get("CORLINMAN_CORS_ORIGINS", "")
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def _status_links_explicitly_configured(cfg: Any | None) -> bool:
    """True when an operator set ``public_url`` via config or env.

    When explicit, the learned-origin auto-detection must stand down so a
    health-check / loopback request can't shadow the operator's choice.
    """
    server_cfg = _extract_section(cfg, "server")
    config_public_url = _extract_section(server_cfg, "public_url")
    if isinstance(config_public_url, str) and config_public_url.strip():
        return True
    return bool(os.environ.get("CORLINMAN_PUBLIC_URL", "").strip())


def _wire_status_links(cfg: Any | None, data_dir: Path) -> bool:
    """Wire the agent status-card channel-link feature exactly once.

    Each channel's reply path calls
    ``corlinman_channels.service._status_link_line(session_key)`` which
    appends a ``{public_url}/status/{token}`` line when this feature is
    armed. We arm it here — before the channels start in the
    sibling-bootstrap loop — by handing
    :func:`corlinman_channels.service.configure_status_links` the public
    base URL + a ``minter`` closure that signs a token for a session key.

    Layering: the ``minter`` is a closure so ``corlinman_channels`` never
    imports ``corlinman_server`` at module top (import-linter rule).

    Resolution (dict-shaped config — ``load_from_path`` returns a plain
    dict), first non-empty wins:

    1. ``[server].public_url`` in config,
    2. the ``CORLINMAN_PUBLIC_URL`` env var,
    3. the **learned** public origin (``<data_dir>/public_origin``) written
       by :class:`~corlinman_server.gateway.origin_learn.OriginLearningMiddleware`
       from a real inbound request — this is the zero-config path: the
       first browser/status-link hit through the public hostname arms the
       feature with no operator action.

    Gated by ``[channels].status_url_in_replies`` (default ``True``). Safe
    no-op when no URL resolves: ``configure_status_links`` keeps links off
    unless public_url + enabled + minter are all truthy.

    Returns ``True`` when an explicit (config/env) URL was used — the
    caller installs the learning middleware only when this is ``False`` (so
    auto-detection runs only when there's nothing explicit to honour).
    """
    try:
        from corlinman_channels.service import configure_status_links

        from corlinman_server.gateway.origin_learn import (
            load_remembered_origin,
        )
        from corlinman_server.gateway.status_revocation import current_epoch
        from corlinman_server.gateway.status_token import (
            make_status_token,
            resolve_signing_key,
        )
    except ImportError as exc:
        logger.warning(
            "gateway.channels.status_links_import_failed", error=str(exc)
        )
        return False

    explicit = _status_links_explicitly_configured(cfg)

    server_cfg = _extract_section(cfg, "server")
    config_public_url = _extract_section(server_cfg, "public_url")
    config_public_url = (
        config_public_url.strip()
        if isinstance(config_public_url, str)
        else ""
    )
    env_public_url = os.environ.get("CORLINMAN_PUBLIC_URL", "").strip()
    learned_public_url = load_remembered_origin(data_dir)
    public_url = config_public_url or env_public_url or learned_public_url

    # The in-process agent servicer (agent_status_card tool) holds no
    # gateway config object and resolves its public base URL from
    # CORLINMAN_PUBLIC_URL first. When the URL came from config or the
    # learned-origin file (env unset), publish it into the process env here
    # so the tool's dispatch builds the same absolute link the channels do.
    if public_url and not env_public_url:
        os.environ["CORLINMAN_PUBLIC_URL"] = public_url

    channels_cfg = _extract_section(cfg, "channels")
    flag = _extract_section(channels_cfg, "status_url_in_replies")
    # Default-on: the feature only surfaces when public_url is also set,
    # so a default of True is safe — an operator who never sets a public
    # URL never sees a link.
    status_enabled = bool(public_url) and (flag is None or bool(flag))

    signing_key = resolve_signing_key(data_dir)

    configure_status_links(
        public_url=public_url,
        enabled=status_enabled,
        # Fold the session's live revocation epoch into each freshly-minted
        # link (#34) so a later ``revoke_session`` bump leaves already-shared
        # links behind while new ones keep working.
        minter=lambda sk: make_status_token(
            sk, signing_key, epoch=current_epoch(data_dir, sk)
        ),
    )

    if status_enabled:
        source = (
            "config"
            if config_public_url
            else "env"
            if env_public_url
            else "learned"
        )
        logger.info(
            "gateway.channels.status_links_enabled",
            public_url=public_url,
            source=source,
        )
    return explicit


def _load_config(path: Path | None) -> Any | None:
    """Best-effort config load.

    Sibling agents populate ``gateway.core.config`` (a Python port of
    ``corlinman_core::config::Config``). We import lazily so the
    entrypoint module stays importable without it. A missing file or
    missing loader returns ``None`` and the gateway boots with whatever
    defaults the downstream modules carry.
    """
    if path is None:
        return None
    if not path.exists():
        logger.warning("gateway.config.missing", path=str(path))
        return None
    core_config = _lazy_import("corlinman_server.gateway.core.config")
    if core_config is None:
        logger.warning(
            "gateway.config.no_loader",
            path=str(path),
            detail="gateway.core.config not present; skipping load",
        )
        return None
    loader: Callable[[Path], Any] | None = (
        getattr(core_config, "load_from_path", None)
        or getattr(core_config, "Config", None)
    )
    if loader is None:
        logger.warning("gateway.config.no_loader_symbol", path=str(path))
        return None
    try:
        cfg = loader(path)  # type: ignore[misc]
        # ``Config(path)`` returning a class is fine — the duck-typed
        # downstream code only reads attributes off whatever we hand it.
    except Exception as exc:
        logger.warning(
            "gateway.config.load_failed", path=str(path), error=str(exc)
        )
        return None
    logger.info("gateway.config.loaded", path=str(path))
    return cfg


def _should_run_legacy_migration(cfg: Any | None) -> bool:
    """Mirror the Rust gate: ``[tenants].enabled && [tenants].migrate_legacy_paths``.

    Default off — pre-Phase-4 deployments keep their flat layout unless
    the operator opts in.
    """
    if cfg is None:
        return False
    tenants = getattr(cfg, "tenants", None)
    if tenants is None and isinstance(cfg, dict):
        tenants = cfg.get("tenants")
    if tenants is None:
        return False
    enabled = (
        getattr(tenants, "enabled", None)
        if not isinstance(tenants, dict)
        else tenants.get("enabled")
    )
    migrate = (
        getattr(tenants, "migrate_legacy_paths", None)
        if not isinstance(tenants, dict)
        else tenants.get("migrate_legacy_paths")
    )
    return bool(enabled) and bool(migrate)


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


# ---------------------------------------------------------------------------
# Config hot-reload (Parcel P11)
# ---------------------------------------------------------------------------


#: Sibling bootstraps re-run when their owning config section changes on
#: a hot-reload. ``providers`` rebuilds ``AppState.provider_registry``
#: from the freshly-loaded ``[providers]`` / ``[models]`` tables. Each
#: sibling exports the same ``bootstrap(state)`` symbol the boot-time
#: seam calls — re-running it is idempotent (it replaces the handle).
_HOT_RELOAD_BOOTSTRAPS: tuple[tuple[str, str], ...] = (
    # (dotted module, section that triggers a re-run)
    ("corlinman_server.gateway.providers", "providers"),
    ("corlinman_server.gateway.providers", "models"),
)


def _reapply_hot_reloadable(state: Any, changed: list[str]) -> list[str]:
    """Re-run the sibling bootstraps whose config section changed.

    Returns the list of sibling module names that were re-applied.
    Best-effort per sibling: a failing bootstrap logs a warning and the
    rest still run — the gateway never crashes on a hot-reload. The
    ``providers`` bootstrap rebuilds ``AppState.provider_registry`` so a
    newly-added / edited provider goes live without a restart.
    """
    changed_set = set(changed)
    reapplied: list[str] = []
    seen: set[str] = set()
    for dotted, section in _HOT_RELOAD_BOOTSTRAPS:
        if section not in changed_set or dotted in seen:
            continue
        seen.add(dotted)
        sibling = _lazy_import(dotted)
        if sibling is None:
            continue
        bootstrap = getattr(sibling, "bootstrap", None)
        if bootstrap is None:
            continue
        name = dotted.rsplit(".", 1)[-1]
        try:
            result = bootstrap(state)
            if isinstance(result, Awaitable):
                # Hot-reload runs on the event loop; a sync bootstrap
                # (providers today) returns None — guard the async case
                # so a future async bootstrap still re-applies cleanly.
                logger.debug(
                    "gateway.config_reload.bootstrap_returned_awaitable",
                    sibling=name,
                )
            reapplied.append(name)
        except Exception as exc:  # pragma: no cover — sibling-owned
            logger.warning(
                "gateway.config_reload.bootstrap_failed",
                sibling=name,
                error=str(exc),
            )
    return reapplied


def _config_hot_reload_enabled(state: Any) -> bool:
    """Whether live config fs-watch hot-reload is enabled (default OFF).

    Honours ``CORLINMAN_CONFIG_HOT_RELOAD`` (env, wins) then
    ``[server].config_hot_reload`` in the loaded config. Off by default —
    see :func:`_start_config_watcher` for why.
    """
    import os

    raw = os.environ.get("CORLINMAN_CONFIG_HOT_RELOAD")
    if raw is not None:
        return raw.strip().lower() in ("1", "true", "yes", "on")
    cfg = getattr(state, "config", None)
    if isinstance(cfg, dict):
        server = cfg.get("server")
        if isinstance(server, dict):
            return bool(server.get("config_hot_reload", False))
    return False


def _start_config_watcher(app: Any, state: Any, config_path: Path | None) -> Any:
    """Build + start a :class:`ConfigWatcher` for the gateway config TOML.

    Returns the watcher's debounce-loop :class:`asyncio.Task` (registered
    into the lifespan's ``background`` list so it is cancelled + awaited
    at shutdown), or ``None`` when no watcher could be started (no config
    path, the file is missing, or the watcher module is absent).

    On every detected change the watcher re-loads via
    ``config.load_from_path``, swaps ``AppState.config``, diffs sections,
    and re-runs the hot-reloadable sibling bootstraps (provider registry
    rebuild). A malformed reload keeps the previous good snapshot.
    """
    if config_path is None or not config_path.exists():
        return None
    # gap-fill v1.15: live fs-watch hot-reload is OPT-IN (default OFF). A real
    # filesystem observer per boot accumulates OS watch handles and can race
    # the config-write path (it destabilises multi-boot test suites and adds
    # little value to most deployments, which restart to apply config). The
    # machinery stays fully wired; enable it with CORLINMAN_CONFIG_HOT_RELOAD=1
    # or ``[server].config_hot_reload = true``. When disabled we behave exactly
    # as before this gap-fill: no watcher, and POST /admin/config/reload stays
    # 503 (manual reload not armed).
    if not _config_hot_reload_enabled(state):
        return None
    watcher_mod = _lazy_import("corlinman_server.gateway.core.config_watcher")
    config_mod = _lazy_import("corlinman_server.gateway.core.config")
    if watcher_mod is None or config_mod is None:
        return None
    ConfigWatcher = getattr(watcher_mod, "ConfigWatcher", None)
    load_from_path = getattr(config_mod, "load_from_path", None)
    if ConfigWatcher is None or load_from_path is None:
        return None

    initial = getattr(state, "config", None)
    if not isinstance(initial, dict):
        # Degraded boot (parse failed at build time / no loader). Seed
        # with an empty dict so the watcher still arms — the first
        # successful reload then publishes the real snapshot.
        initial = {}

    def _on_reload(report: Any, old_cfg: dict[str, Any], new_cfg: dict[str, Any]) -> None:
        # Publish the new snapshot onto the live AppState first so any
        # re-applied bootstrap reads the fresh config.
        state.config = new_cfg
        with suppress(AttributeError, TypeError):
            app.state.corlinman_config = new_cfg
        changed = list(getattr(report, "changed_sections", []))
        restart_needed = sorted(
            set(changed) & RESTART_REQUIRED_SECTIONS_LOCAL()
        )
        reapplied = _reapply_hot_reloadable(state, changed)
        logger.info(
            "gateway.config_reload.applied",
            path=str(config_path),
            changed=changed,
            reapplied=reapplied,
        )
        for section in restart_needed:
            logger.warning(
                "gateway.config_reload.restart_required",
                section=section,
                detail=(
                    "section changed but cannot hot-apply; restart the "
                    "gateway for it to take effect"
                ),
            )

    def _validator(new_cfg: dict[str, Any]) -> list[str]:
        """Validate a reloaded config before it goes live.

        Returns a list of human-readable error strings — non-empty means
        the reload is rejected and the previous good snapshot is kept (the
        ConfigWatcher gates on this). Best-effort + defensive: any
        validator crash returns ``[]`` (accept) rather than wedging the
        watcher. Defers to the gateway core ``validate_config`` helper when
        present so the prod cascade's typed validation is honoured.
        """
        validate = getattr(config_mod, "validate_config", None)
        if validate is None:
            return []
        try:
            issues = validate(new_cfg)
        except Exception as exc:  # noqa: BLE001 — never wedge the watcher
            logger.warning("gateway.config_reload.validator_crashed", error=str(exc))
            return []
        if not issues:
            return []
        return [str(i) for i in issues]

    async def _hook_emitter(
        event: str, section: str, old: Any, new: Any
    ) -> None:
        """Fire a ``ConfigChanged`` notice onto the shared HookBus per
        changed section so in-process subscribers (evolution observer,
        future config-reactive components) react to a live edit. The
        watcher already swapped the snapshot before calling us; we only
        notify. Best-effort: a missing bus / emit failure is swallowed so
        the reload itself never fails on the notification side."""
        bus = getattr(app.state, "hook_bus", None)
        if bus is None:
            return
        emit = getattr(bus, "emit", None)
        if emit is None:
            return
        try:
            res = emit(
                {
                    "kind": "ConfigChanged",
                    "event": event,
                    "section": section,
                }
            )
            if hasattr(res, "__await__"):
                await res
        except Exception as exc:  # noqa: BLE001 — notification is best-effort
            logger.debug(
                "gateway.config_reload.hook_emit_failed",
                section=section,
                error=str(exc),
            )

    watcher = ConfigWatcher(
        config_path,
        initial,
        parser=load_from_path,
        validator=_validator,
        hook_emitter=_hook_emitter,
        on_reload=_on_reload,
    )
    # Expose the watcher on AppState so the admin /admin/config/reload
    # route (routes_admin_b) can drive a manual reload via the same
    # ConfigWatcher instance.
    with suppress(AttributeError, TypeError):
        state.config_watcher = watcher
    extras = getattr(state, "extras", None)
    if isinstance(extras, dict):
        extras["config_watcher"] = watcher

    async def _run() -> None:
        await watcher.start()
        try:
            # Park until cancelled — ``ConfigWatcher`` owns its own
            # debounce/SIGHUP tasks; this coroutine just keeps the
            # watcher alive for the process lifetime and tears it down.
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            pass
        finally:
            await watcher.stop()

    task = asyncio.create_task(_run(), name="gateway.config_watcher")
    logger.info("gateway.config_reload.watcher_started", path=str(config_path))
    return task


def RESTART_REQUIRED_SECTIONS_LOCAL() -> frozenset[str]:
    """Lazy accessor for the watcher's restart-required section set.

    Imported via a function (not a module-level ``from``) so the
    entrypoint stays importable when ``config_watcher`` is mid-port —
    consistent with the rest of the lazy-import discipline in this file.
    """
    watcher_mod = _lazy_import("corlinman_server.gateway.core.config_watcher")
    if watcher_mod is None:
        return frozenset()
    return getattr(watcher_mod, "RESTART_REQUIRED_SECTIONS", frozenset())


# ---------------------------------------------------------------------------
# W2.2 default scheduler-job registration
# ---------------------------------------------------------------------------


#: Canonical name of the default update-check cron job. Centralised so
#: the de-dupe check + tests + future runtime hooks all read the same
#: literal — change here and every callsite tracks.
DEFAULT_UPDATE_CHECK_JOB_NAME: str = "system.update_check"

#: Canonical name of the W3 v2 darwin daily rubric scan. Same naming
#: convention as ``system.update_check`` — ``<plugin>.<tool>`` — so the
#: scheduler's :class:`JobAction.run_tool` dispatch picks up the
#: ``EVOLUTION_DARWIN_CURATE_BUILTIN_NAME`` builtin by string match.
DEFAULT_EVOLUTION_DARWIN_CURATE_JOB_NAME: str = "evolution.darwin_curate"


def _config_has_scheduler_job(cfg: Any | None, name: str) -> bool:
    """``True`` when the loaded config already carries a job by ``name``.

    The gateway config loader hands back dict-shaped data (see
    ``gateway.core.config`` docstring), so we read
    ``cfg["scheduler"]["jobs"]`` and look for the first entry whose
    ``name`` matches. Tolerates a missing scheduler section / non-list
    ``jobs`` value / missing ``name`` keys without raising — the
    explicit-config detection only needs to ``True`` on a clean match.

    Plain dataclass-shaped configs (``cfg.scheduler.jobs``) also work;
    we duck-type on attribute then fall back to mapping access so a
    Wave-1 ``SimpleNamespace``-shaped test config goes through the
    same branch the production loader does.
    """
    scheduler = _extract_section(cfg, "scheduler")
    if scheduler is None:
        return False
    jobs = _extract_section(scheduler, "jobs")
    if not isinstance(jobs, (list, tuple)):
        return False
    for entry in jobs:
        entry_name = _extract_section(entry, "name")
        if isinstance(entry_name, str) and entry_name == name:
            return True
    return False


def _extract_section(obj: Any, key: str) -> Any:
    """Read ``obj[key]`` / ``obj.key`` tolerantly.

    Mirrors the ``_should_run_legacy_migration`` helper's discipline:
    the config may arrive as a plain dict (production loader), a
    dataclass-shaped wrapper (tests), or ``None`` (degraded boot). One
    helper keeps every caller's branch logic single-line.
    """
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _register_default_update_check_job(
    app: Any, cfg: Any | None, interval_hours: int
) -> None:
    """Stash a default ``system.update_check`` :class:`SchedulerJob` on ``app.state``.

    Behaviour matrix (matches the spec in W2.2 of
    ``docs/PLAN_AUTO_UPDATE.md``):

    * ``[system.update_check] enabled = false`` — *not* called (the
      caller guards on ``update_cfg.enabled`` first).
    * Explicit ``[[scheduler.jobs]] name = "system.update_check"``
      already in config — silent no-op so the operator's explicit
      cron / timezone / action wins.
    * Otherwise — appends a :class:`SchedulerJob` with cron
      ``"0 0 */{interval_hours} * * * *"`` and a ``run_tool``-shaped
      action pointing at the builtin name. The job lives on
      ``app.state.corlinman_default_scheduler_jobs`` (a list) so the
      scheduler runtime (once :func:`spawn` is wired into the lifespan)
      can pick it up alongside the config jobs, and tests can assert
      its presence without exercising the runtime.

    All log lines use the ``gateway.system.update_check_job.*`` prefix
    so a single grep surfaces the W2.2 wiring across boot logs.
    """
    if _config_has_scheduler_job(cfg, DEFAULT_UPDATE_CHECK_JOB_NAME):
        logger.info(
            "gateway.system.update_check_job.skipped_explicit_config",
            name=DEFAULT_UPDATE_CHECK_JOB_NAME,
        )
        return

    # Build the cron string in the project's 7-field grammar
    # (sec min hour dom mon dow year). ``0 0 */N * * * *`` fires at
    # the top of every Nth hour — matches every existing scheduler
    # job's choice in ``docs/config.example.toml``. Clamp the interval
    # so a misconfigured ``interval_hours = 0`` falls back to 1 (the
    # config dataclass already clamps to ``>=1`` but a degraded boot
    # may have skipped that path).
    interval = max(1, int(interval_hours))
    cron_expr = f"0 0 */{interval} * * * *"

    try:
        from corlinman_server.scheduler import JobAction, SchedulerJob

        job = SchedulerJob(
            name=DEFAULT_UPDATE_CHECK_JOB_NAME,
            cron=cron_expr,
            action=JobAction.run_tool(
                plugin="system",
                tool="update_check",
            ),
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "gateway.system.update_check_job.build_failed",
            error=str(exc),
        )
        return

    existing = getattr(app.state, "corlinman_default_scheduler_jobs", None)
    if not isinstance(existing, list):
        existing = []
    # De-dupe against the in-memory list too so a hot-reload that
    # re-runs this branch doesn't grow the list unbounded.
    if any(
        getattr(j, "name", None) == DEFAULT_UPDATE_CHECK_JOB_NAME
        for j in existing
    ):
        return
    existing.append(job)
    app.state.corlinman_default_scheduler_jobs = existing

    logger.info(
        "gateway.system.update_check_job.registered",
        name=DEFAULT_UPDATE_CHECK_JOB_NAME,
        cron=cron_expr,
    )


def _register_default_darwin_curate_job(app: Any, cfg: Any | None) -> None:
    """W3 v2.1 — stash a default ``evolution.darwin_curate`` scheduler
    job on ``app.state`` alongside the W2.2 update-check job.

    Same operator-override / de-dupe discipline as
    :func:`_register_default_update_check_job`:

    * Explicit ``[[scheduler.jobs]] name = "evolution.darwin_curate"``
      already in config → silent no-op so the operator's cron wins.
    * Otherwise → append a :class:`SchedulerJob` firing daily at
      ``"0 30 3 * * * *"`` (03:30 UTC, after the update-check window).
      Action is ``JobAction.run_tool(plugin="evolution",
      tool="darwin_curate")`` which the scheduler dispatches to the
      :data:`EVOLUTION_DARWIN_CURATE_BUILTIN_NAME` builtin.

    Log lines use the ``gateway.evolution.darwin_curate_job.*`` prefix
    so the wiring is greppable across boot logs.
    """
    name = DEFAULT_EVOLUTION_DARWIN_CURATE_JOB_NAME
    if _config_has_scheduler_job(cfg, name):
        logger.info(
            "gateway.evolution.darwin_curate_job.skipped_explicit_config",
            name=name,
        )
        return

    # Daily at 03:30 UTC. update_check fires every N hours; darwin
    # only needs once per day because skill content changes slowly.
    cron_expr = "0 30 3 * * * *"

    try:
        from corlinman_server.scheduler import JobAction, SchedulerJob

        job = SchedulerJob(
            name=name,
            cron=cron_expr,
            action=JobAction.run_tool(
                plugin="evolution",
                tool="darwin_curate",
            ),
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "gateway.evolution.darwin_curate_job.build_failed",
            error=str(exc),
        )
        return

    existing = getattr(app.state, "corlinman_default_scheduler_jobs", None)
    if not isinstance(existing, list):
        existing = []
    if any(getattr(j, "name", None) == name for j in existing):
        return
    existing.append(job)
    app.state.corlinman_default_scheduler_jobs = existing

    logger.info(
        "gateway.evolution.darwin_curate_job.registered",
        name=name,
        cron=cron_expr,
    )


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


def _scheduler_job_from_config_entry(entry: Any) -> Any | None:
    """Convert one ``[[scheduler.jobs]]`` config entry into a runtime
    :class:`SchedulerJob`, or ``None`` for a misshapen entry.

    R4-F1: operator-defined scheduler jobs were *display-only* in the
    Python port — the admin route rendered them but nothing converted
    them into runtime specs. This bridges the loaded-config shape (dict
    *or* dataclass, read via :func:`_extract_section`) into the
    :class:`SchedulerJob` the runtime spawns. Best-effort: one bad entry
    returns ``None`` and is skipped rather than aborting boot.
    """
    name = _extract_section(entry, "name")
    cron = _extract_section(entry, "cron")
    if not isinstance(name, str) or not name:
        return None
    if not isinstance(cron, str) or not cron:
        return None
    try:
        from corlinman_server.scheduler import JobAction, SchedulerJob
    except Exception:  # pragma: no cover — defensive
        return None

    action_obj = _extract_section(entry, "action")
    kind = _extract_section(action_obj, "type") or _extract_section(action_obj, "kind")
    if kind is None and isinstance(action_obj, dict):
        # Nested-key discriminant form: ``action = { subprocess = {...} }``.
        for key in ("subprocess", "run_tool", "run_agent"):
            if key in action_obj:
                kind = key
                break

    try:
        if kind == "subprocess":
            command = _extract_section(action_obj, "command")
            if not isinstance(command, str) or not command:
                return None
            raw_args = _extract_section(action_obj, "args")
            args = (
                tuple(str(a) for a in raw_args)
                if isinstance(raw_args, (list, tuple))
                else ()
            )
            raw_timeout = _extract_section(action_obj, "timeout_secs")
            timeout = int(raw_timeout) if isinstance(raw_timeout, int) else 600
            action = JobAction.subprocess(
                command=command, args=args, timeout_secs=timeout
            )
        elif kind == "run_tool":
            plugin = _extract_section(action_obj, "plugin")
            tool = _extract_section(action_obj, "tool")
            if not isinstance(plugin, str) or not isinstance(tool, str):
                return None
            action = JobAction.run_tool(
                plugin=plugin, tool=tool, args=_extract_section(action_obj, "args")
            )
        elif kind == "run_agent":
            prompt = _extract_section(action_obj, "prompt")
            if not isinstance(prompt, str) or not prompt:
                return None
            action = JobAction.run_agent(prompt=prompt)
        else:
            return None
    except Exception:  # pragma: no cover — defensive
        return None

    tz = _extract_section(entry, "timezone")
    return SchedulerJob(
        name=name, cron=cron, action=action, timezone=tz if isinstance(tz, str) else None
    )


def _effective_scheduler_config(app: Any, cfg: Any | None) -> Any:
    """Assemble the :class:`SchedulerConfig` the lifespan spawns.

    Merges, de-duped by name (first writer wins):

    1. operator ``[[scheduler.jobs]]`` from the loaded config; then
    2. the auto-registered defaults on
       ``app.state.corlinman_default_scheduler_jobs`` (the registration
       helpers only add a default when the config does *not* already
       declare it, so there is no overlap to resolve).
    """
    from corlinman_server.scheduler import SchedulerConfig

    jobs: list[Any] = []
    seen: set[str] = set()

    section = _extract_section(cfg, "scheduler")
    raw_jobs = _extract_section(section, "jobs")
    if isinstance(raw_jobs, (list, tuple)):
        for entry in raw_jobs:
            job = _scheduler_job_from_config_entry(entry)
            if job is not None and job.name not in seen:
                jobs.append(job)
                seen.add(job.name)

    for job in list_default_scheduler_jobs(app):
        jname = getattr(job, "name", None)
        if isinstance(jname, str) and jname and jname not in seen:
            jobs.append(job)
            seen.add(jname)

    return SchedulerConfig(jobs=tuple(jobs))


# ---------------------------------------------------------------------------
# gap-fill v1.15 — CONTRACT C2 wiring spine + identity sweep
# ---------------------------------------------------------------------------


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


def _build_agent_runner_fn(state: Any) -> Any:
    """Build the ``agent_runner_fn`` the scheduler's ``run_agent`` action
    invokes for a real one-turn agent run on a cron schedule.

    The returned coroutine accepts a ``prompt`` string, drives one turn
    through the live :class:`ChatService` parked on ``state.chat``, and
    returns a small result dict (``{"ok": ..., "reply"/"error": ...}``).
    It resolves the chat service lazily *per firing* so a scheduler job
    that fires before the chat bootstrap completed (or after a hot-reload
    swapped the service) always sees the current handle. When no chat
    service is wired the runner returns ``{"ok": False,
    "error": "chat_service_unavailable"}`` and the scheduler surfaces the
    firing as a failure on the hook bus rather than crashing.

    Channel delivery: when the job metadata carries a home-channel hint
    the result is logged on the structlog feed (the same best-effort
    delivery seam the restart-broadcast uses); a future outbound-handle
    wave can route through it without re-touching this closure.
    """

    async def _runner(prompt: str) -> dict[str, Any]:
        chat_service = getattr(state, "chat", None)
        if chat_service is None:
            extras = getattr(state, "extras", None)
            if isinstance(extras, dict):
                chat_service = extras.get("chat")
        if chat_service is None:
            return {"ok": False, "error": "chat_service_unavailable"}
        try:
            from corlinman_server.gateway_api.types import (
                InternalChatRequest,
                Message,
                Role,
            )
        except Exception as exc:  # noqa: BLE001 — degrade cleanly
            return {"ok": False, "error": "gateway_api_unavailable",
                    "message": str(exc)}

        model = ""
        cfg = getattr(state, "config", None)
        if isinstance(cfg, dict):
            models_cfg = cfg.get("models")
            if isinstance(models_cfg, dict):
                m = models_cfg.get("default")
                if isinstance(m, str) and m:
                    model = m

        request = InternalChatRequest(
            model=model,
            messages=[Message(role=Role.USER, content=prompt)],
            session_key=f"scheduler:run_agent:{uuid.uuid4().hex[:8]}",
            stream=False,
            max_tokens=None,
            temperature=None,
            attachments=[],
            binding=None,
        )
        cancel = asyncio.Event()
        reply_parts: list[str] = []
        try:
            stream = chat_service.run(request, cancel)
            async for event in stream:
                kind = getattr(event, "kind", None)
                if kind == "token_delta":
                    delta = getattr(event, "delta", None)
                    if isinstance(delta, str):
                        reply_parts.append(delta)
                elif kind == "error":
                    inner = getattr(event, "error", None)
                    return {
                        "ok": False,
                        "error": "chat_error",
                        "reason": str(getattr(inner, "reason", "unknown")),
                    }
                elif kind == "done":
                    break
        except Exception as exc:  # noqa: BLE001 — surface, never raise
            return {"ok": False, "error": "chat_service_failed",
                    "message": str(exc)}
        return {"ok": True, "reply": "".join(reply_parts)}

    return _runner


async def _wire_c2_handles(
    app: Any, state: Any, admin_a_state: Any, data_dir: Path, cfg: Any | None
) -> None:
    """Construct + publish the CONTRACT C2 handles onto ``state``.

    Sets (each best-effort, ``None`` on failure so the consumer degrades):

    * ``state.memory_host``     — corlinman_memory_host.LocalSqliteHost.
    * ``state.persona_resolver``— corlinman_persona.PersonaResolver over
      the runtime persona-STATE store (``agent_state.sqlite``); also stashed
      on ``extras`` + admin_a for the qzone builtin / future producers.
    * ``state.identity_store``  — corlinman_identity.SqliteIdentityStore;
      also stamped onto ``admin_a_state`` so the ``/admin/identity*`` routes
      un-503.
    * ``state.agent_runner_fn`` — async ``(prompt) -> dict`` for the
      scheduler ``run_agent`` action.
    * ``state.hook_runner``     — corlinman_hooks.runner.HookRunner with
      file-discovery (``CORLINMAN_HOOKS_DIR``).
    """
    # --- memory_host -----------------------------------------------------
    try:
        from corlinman_memory_host import LocalSqliteHost

        if getattr(state, "memory_host", None) is None:
            mem_path = data_dir / "memory.sqlite"
            state.memory_host = await LocalSqliteHost.open(
                "local", str(mem_path)
            )
            logger.info("gateway.c2.memory_host_wired", path=str(mem_path))
    except Exception as exc:  # noqa: BLE001 — memory-free chat degrades fine
        logger.warning("gateway.c2.memory_host_failed", error=str(exc))
        with suppress(AttributeError, TypeError):
            state.memory_host = None

    # --- persona_resolver (gap persona-life-resolver-dead) ---------------
    # The resolver reads ``{{persona.mood}}`` / ``{{persona.life_*}}`` off
    # the SAME runtime persona-STATE DB (``agent_state.sqlite``) the agent
    # ``persona_life_*`` tools write to, keyed by ``agent_id``. Publishing
    # it on AppState gives the prompt-render path a live read surface.
    try:
        from corlinman_persona import PersonaResolver
        from corlinman_persona.store import PersonaStore as _StateStore

        state_store = await _StateStore.open_or_create(
            data_dir / "agent_state.sqlite"
        )
        resolver = PersonaResolver(state_store)
        state.persona_resolver = resolver
        # Stash the open store handle so the lifespan-exit can close it and
        # the qzone builtin can reach the same DB without re-opening.
        app.state.corlinman_persona_state_store = state_store
        extras = getattr(state, "extras", None)
        if isinstance(extras, dict):
            extras["persona_resolver"] = resolver
            extras["persona_state_store"] = state_store
        if admin_a_state is not None:
            with suppress(AttributeError, TypeError):
                admin_a_state.persona_resolver = resolver
        logger.info("gateway.c2.persona_resolver_wired")
    except Exception as exc:  # noqa: BLE001 — placeholders fall back to ""
        logger.warning("gateway.c2.persona_resolver_failed", error=str(exc))
        with suppress(AttributeError, TypeError):
            state.persona_resolver = None

    # --- identity_store (gap identity-unwired-and-no-auth-gate) ----------
    try:
        from corlinman_identity import (
            SqliteIdentityStore,
            identity_db_path,
            legacy_default,
        )

        id_path = identity_db_path(data_dir, legacy_default())
        id_store = await SqliteIdentityStore.open(id_path)
        state.identity_store = id_store
        app.state.corlinman_identity_store = id_store
        # Un-503 the /admin/identity* routes by stamping the store onto the
        # AdminState the routes resolve via get_admin_state().
        if admin_a_state is not None:
            with suppress(AttributeError, TypeError):
                admin_a_state.identity_store = id_store
        extras = getattr(state, "extras", None)
        if isinstance(extras, dict):
            extras["identity_store"] = id_store
        logger.info("gateway.c2.identity_store_wired", path=str(id_path))
    except Exception as exc:  # noqa: BLE001 — routes 503 cleanly
        logger.warning("gateway.c2.identity_store_failed", error=str(exc))
        with suppress(AttributeError, TypeError):
            state.identity_store = None

    # --- agent_runner_fn (gap goals-cron-run-agent-dead) -----------------
    try:
        state.agent_runner_fn = _build_agent_runner_fn(state)
        logger.info("gateway.c2.agent_runner_fn_wired")
    except Exception as exc:  # noqa: BLE001
        logger.warning("gateway.c2.agent_runner_fn_failed", error=str(exc))
        with suppress(AttributeError, TypeError):
            state.agent_runner_fn = None

    # --- hook_runner (C3) ------------------------------------------------
    try:
        from corlinman_hooks.runner import HookRunner

        # The agent-level hooks config lives under ``[hooks]`` in the
        # loaded config; file-discovered HOOK.yaml/handler.py hooks load
        # from CORLINMAN_HOOKS_DIR (falls back to <data_dir>/hooks).
        hooks_cfg: dict[str, Any] = {}
        section = _extract_section(cfg, "hooks")
        if isinstance(section, dict):
            hooks_cfg = {"hooks": section}
        hooks_dir_env = os.environ.get("CORLINMAN_HOOKS_DIR")
        hooks_dir: Path | None
        if hooks_dir_env:
            hooks_dir = Path(hooks_dir_env)
        else:
            default_hooks_dir = data_dir / "hooks"
            hooks_dir = default_hooks_dir if default_hooks_dir.is_dir() else None
        runner = HookRunner(hooks_cfg, hooks_dir=hooks_dir)
        state.hook_runner = runner
        app.state.corlinman_hook_runner = runner
        extras = getattr(state, "extras", None)
        if isinstance(extras, dict):
            extras["hook_runner"] = runner
        logger.info(
            "gateway.c2.hook_runner_wired",
            hooks_dir=str(hooks_dir) if hooks_dir else None,
            discovered=getattr(runner, "discovered_events", {}),
        )
    except Exception as exc:  # noqa: BLE001 — no hooks degrades fine
        logger.warning("gateway.c2.hook_runner_failed", error=str(exc))
        with suppress(AttributeError, TypeError):
            state.hook_runner = None


# ---------------------------------------------------------------------------
# AppState bridge
# ---------------------------------------------------------------------------


def _build_state(cfg: Any | None, data_dir: Path) -> Any:
    """Construct the shared ``AppState`` bundle.

    Delegates to ``gateway.core.AppState`` when available; falls back to
    a minimal :class:`_DegradedAppState` so degraded-mode boots still
    have *some* object to pass into route handlers / tests.
    """
    core = _lazy_import("corlinman_server.gateway.core")
    if core is not None:
        builder: Any = (
            getattr(core, "build_app_state", None)
            or getattr(core, "AppState", None)
        )
        if builder is not None:
            built: Any = None
            try:
                built = builder(config=cfg, data_dir=data_dir)  # type: ignore[misc]
            except TypeError:
                # The real ``AppState`` doesn't accept ``data_dir`` as a
                # kwarg (it's a free-form attribute the gateway wires
                # at boot). Fall back to a ``config``-only call and
                # stamp ``data_dir`` afterwards so downstream code can
                # still do ``getattr(state, "data_dir", None)``.
                try:
                    built = builder(config=cfg)  # type: ignore[misc]
                except TypeError:
                    try:
                        built = builder()  # type: ignore[misc]
                    except Exception as exc:  # pragma: no cover — defensive
                        logger.warning(
                            "gateway.state.builder_failed",
                            builder=type(builder).__name__,
                            error=str(exc),
                        )
                except Exception as exc:  # pragma: no cover — defensive
                    logger.warning(
                        "gateway.state.builder_failed",
                        builder=type(builder).__name__,
                        error=str(exc),
                    )
            except Exception as exc:  # pragma: no cover — defensive
                logger.warning(
                    "gateway.state.builder_failed",
                    builder=type(builder).__name__,
                    error=str(exc),
                )
            if built is not None:
                # AppState is a dataclass without __slots__ — safe to
                # set attributes dynamically. This is the contract
                # ``_mount_routes`` reads via ``getattr(state,
                # "data_dir", None)`` to wire the profile store.
                with suppress(AttributeError, TypeError):
                    built.data_dir = data_dir
                if getattr(built, "config", None) is None and cfg is not None:
                    with suppress(AttributeError, TypeError):
                        built.config = cfg

                # Install the in-process log broadcaster + structlog
                # processor that fans every log event into the
                # /admin/logs/stream SSE feed. Without this step the
                # route returns 503 ``logs_disabled`` even though the
                # backend code exists — pre-1.6 deployments never wired
                # it. Best-effort: a degraded boot keeps the previous
                # 503 behaviour.
                try:
                    LogBroadcaster = getattr(core, "LogBroadcaster", None)
                    BroadcastHandler = getattr(core, "BroadcastLoggingHandler", None)
                    make_processor = getattr(core, "make_structlog_processor", None)
                    if (
                        LogBroadcaster is not None
                        and getattr(built, "log_broadcaster", None) is None
                    ):
                        broadcaster = LogBroadcaster()
                        built.log_broadcaster = broadcaster
                        # Attach a stdlib-logging Handler to the root
                        # logger so every ``logging.getLogger(...)`` call
                        # across the codebase (channels uses these
                        # directly) fans out to the SSE feed. The
                        # structlog processor below catches the smaller
                        # population that goes through structlog.
                        if BroadcastHandler is not None:
                            try:
                                import logging as _logging

                                handler = BroadcastHandler(broadcaster, level=_logging.INFO)
                                # Attach the handler to the root logger
                                # AND every existing named logger so
                                # libraries that set propagate=False
                                # (uvicorn, uvicorn.access, uvicorn.error,
                                # httpx, etc.) still surface events into
                                # the SSE feed. Idempotent: re-runs skip
                                # loggers that already carry the handler.
                                root = _logging.getLogger()
                                if not any(
                                    isinstance(h, BroadcastHandler)
                                    for h in root.handlers
                                ):
                                    root.addHandler(handler)
                                if root.level > _logging.INFO or root.level == 0:
                                    root.setLevel(_logging.INFO)
                                # Walk the existing logger registry. New
                                # loggers created later inherit from
                                # root, so attaching here covers
                                # known-stubborn-propagate ones AND any
                                # custom named loggers already alive at
                                # boot. ``Logger.manager.loggerDict`` is
                                # the documented introspection hook.
                                logger_dict = getattr(
                                    _logging.Logger.manager, "loggerDict", {}
                                )
                                for _name, lg in list(logger_dict.items()):
                                    if not isinstance(lg, _logging.Logger):
                                        continue
                                    if any(
                                        isinstance(h, BroadcastHandler)
                                        for h in lg.handlers
                                    ):
                                        continue
                                    # Only attach to loggers that
                                    # actively block propagation —
                                    # otherwise the root handler covers
                                    # them and we'd double-emit.
                                    if not lg.propagate:
                                        lg.addHandler(handler)
                                        if lg.level > _logging.INFO or lg.level == 0:
                                            lg.setLevel(_logging.INFO)
                                # Also attach explicitly to uvicorn's
                                # well-known loggers in case they were
                                # registered AFTER our walk (e.g. on the
                                # first request).
                                for name in (
                                    "uvicorn", "uvicorn.access", "uvicorn.error",
                                    "fastapi", "httpx",
                                ):
                                    lg = _logging.getLogger(name)
                                    if not any(
                                        isinstance(h, BroadcastHandler)
                                        for h in lg.handlers
                                    ):
                                        lg.addHandler(handler)
                                    if lg.level > _logging.INFO or lg.level == 0:
                                        lg.setLevel(_logging.INFO)
                            except Exception as exc:  # noqa: BLE001
                                logger.debug(
                                    "gateway.log_broadcast.stdlib_handler_attach_failed",
                                    error=str(exc),
                                )
                        if make_processor is not None:
                            # Attach the processor to structlog's default
                            # configuration too — for events emitted via
                            # ``structlog.get_logger`` which don't pass
                            # through stdlib's root.
                            try:
                                import structlog

                                cfg_obj = structlog.get_config()
                                processors = list(cfg_obj.get("processors", []))
                                processors.insert(-1, make_processor(broadcaster))
                                structlog.configure(processors=processors)
                            except Exception as exc:  # noqa: BLE001
                                logger.debug(
                                    "gateway.log_broadcast.processor_attach_failed",
                                    error=str(exc),
                                )
                        logger.info("gateway.log_broadcast.installed")
                except Exception as exc:  # noqa: BLE001 — best-effort
                    logger.warning(
                        "gateway.log_broadcast.install_failed",
                        error=str(exc),
                    )
                return built
    return _DegradedAppState(config=cfg, data_dir=data_dir)


class _DegradedAppState:
    """Minimal stand-in used when ``gateway.core`` isn't ported yet.

    Carries just enough state for the placeholder resolvers + a basic
    ``/health`` route to function. Sibling agents will replace this with
    the real ``AppState`` bundle.
    """

    __slots__ = ("config", "data_dir")

    def __init__(self, *, config: Any | None, data_dir: Path) -> None:
        self.config = config
        self.data_dir = data_dir

    def __repr__(self) -> str:  # pragma: no cover — debug only
        return f"_DegradedAppState(data_dir={self.data_dir!r})"


# ---------------------------------------------------------------------------
# Agent registry stack (W1.2)
# ---------------------------------------------------------------------------


def _repo_agents_dir() -> Path:
    """Resolve the bundled ``agents/`` dir on disk.

    Walks up from this module towards the repo root, picking the first
    ancestor that has both ``agents/`` and ``python/packages/``. This
    keeps the gateway boot working under ``uv run`` from the repo
    root, from a worktree, and from a wheel-installed deployment (in
    which case the upward walk just falls off the tree and we return
    a non-existent path — the registry loader silently ignores it).
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "agents"
        if candidate.is_dir() and (parent / "python" / "packages").is_dir():
            return candidate
    # Fallback: ``./agents`` relative to CWD. ``load_from_dir_stack``
    # skips non-existent paths so this is safe.
    return Path("agents")


def _build_agent_registry_stack(
    data_dir: Path | None,
) -> tuple[Any | None, Any | None]:
    """Compose the three-tier agent-card registry + an async reloader.

    Returns ``(registry, reload_callable)``. Either entry can be
    ``None`` when the agent package isn't available — callers degrade
    accordingly (the admin routes fall back to a raw filesystem scan).

    The reload helper is closure-captured so the admin POST/DELETE
    handlers can call it without threading the tier list through the
    AdminState dataclass.
    """
    try:
        from corlinman_agent.agents import AgentCardRegistry, AgentSource
    except ImportError as exc:  # pragma: no cover — package missing
        logger.warning("gateway.agent_registry.import_failed", error=str(exc))
        return None, None

    repo_dir = _repo_agents_dir()
    user_dir: Path | None = None
    if data_dir is not None:
        user_dir = Path(data_dir) / "agents"
        try:
            user_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover — defensive
            logger.warning(
                "gateway.agent_registry.user_overlay_mkdir_failed",
                path=str(user_dir),
                error=str(exc),
            )
            user_dir = None

    def _stack() -> list[tuple[Path, AgentSource]]:
        """Re-resolve the tier list per reload so a project overlay
        created mid-run is picked up without a full restart."""
        out: list[tuple[Path, AgentSource]] = [(repo_dir, "built-in")]
        if user_dir is not None:
            out.append((user_dir, "user"))
        project_dir = Path.cwd() / ".corlinman" / "agents"
        if project_dir.exists():
            out.append((project_dir, "project"))
        return out

    try:
        registry: Any = AgentCardRegistry.load_from_dir_stack(_stack())
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("gateway.agent_registry.initial_load_failed", error=str(exc))
        registry = None

    async def _reload() -> Any:
        """Re-scan the tier list. Errors are swallowed so callers
        always get a registry back (potentially the previous one).
        Synchronous under the hood but kept async so the route
        handlers can await it uniformly."""
        try:
            return AgentCardRegistry.load_from_dir_stack(_stack())
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "gateway.agent_registry.reload_failed", error=str(exc)
            )
            return None

    return registry, _reload


# ---------------------------------------------------------------------------
# Routes composition (parallel-agent contracts diverge per submodule)
# ---------------------------------------------------------------------------


def _mount_routes(
    app: Any, state: Any, *, admin_config_path: Path | None = None
) -> Any:
    """Mount every gateway routes submodule onto ``app``.

    Each W4 submodule exposes a different composition surface; this
    helper wires them all in one place so ``build_app`` stays compact:

    * ``routes.register.build_app_router(state)`` — top-level / endpoint set
    * ``routes_voice.mod.router(voice_state)`` — /v1/voice WebSocket
    * ``routes_admin_a.build_router()`` — admin A bundle (9 sub-routers)
    * ``routes_admin_b.build_router()`` — admin B bundle (13 sub-routers)

    Missing submodules log a warning and the gateway continues to boot
    in degraded mode (so a partial port still serves health checks).

    Returns a ``(admin_a_state, admin_b_state)`` tuple — each entry is
    the registered ``AdminState`` instance for that subtree (or ``None``
    when the submodule isn't present). The lifespan reaches into the
    admin_a slot to populate seeded credentials after
    :func:`ensure_admin_credentials` completes, and into the admin_b
    slot to attach the evolution-store repos opened by W5.0. The state
    objects are registered with ``set_admin_state`` here so test code
    that doesn't run the lifespan still sees a usable singleton.
    """
    routes_top = _lazy_import("corlinman_server.gateway.routes.register")
    if routes_top is not None:
        try:
            gateway_state_cls = getattr(routes_top, "GatewayState", None)
            build_app_router = getattr(routes_top, "build_app_router", None)
            if gateway_state_cls is not None and build_app_router is not None:
                # GatewayState is a dataclass of duck-typed optional deps;
                # we hand it the AppState handle so route handlers can
                # downcast as they need.
                gw_state = (
                    gateway_state_cls(app_state=state)
                    if hasattr(gateway_state_cls, "__dataclass_fields__")
                    and "app_state" in gateway_state_cls.__dataclass_fields__
                    else gateway_state_cls()
                )
                app.include_router(build_app_router(gw_state))
        except Exception as exc:  # pragma: no cover — sibling-owned
            logger.warning("gateway.routes.top.mount_failed", error=str(exc))

    routes_voice_mod = _lazy_import("corlinman_server.gateway.routes_voice.mod")
    if routes_voice_mod is not None:
        try:
            voice_router = routes_voice_mod.router()
            app.include_router(voice_router)
        except Exception as exc:  # pragma: no cover — sibling-owned
            logger.warning("gateway.routes_voice.mount_failed", error=str(exc))

    admin_a_state: Any | None = None
    admin_a = _lazy_import("corlinman_server.gateway.routes_admin_a")
    if admin_a is not None:
        try:
            admin_a_state_cls = getattr(admin_a, "AdminState", None)
            set_admin_a = getattr(admin_a, "set_admin_state", None)
            if admin_a_state_cls is not None and set_admin_a is not None:
                # Construct an AdminState seeded with what we can know
                # synchronously — admin_username / admin_password_hash /
                # must_change_password are populated by the lifespan once
                # ``ensure_admin_credentials`` resolves the disk state.
                data_dir = getattr(state, "data_dir", None)
                # Wave 3.1: wire the profile registry. Best-effort —
                # if the profiles submodule fails to import we leave
                # ``profile_store=None`` and the /admin/profiles* routes
                # 503 ``profile_store_missing`` rather than crashing the
                # gateway boot.
                profile_store: Any | None = None
                if data_dir is not None:
                    try:
                        from corlinman_server.profiles import ProfileStore

                        profile_store = ProfileStore(
                            Path(data_dir) / "profiles"
                        )
                        # Bootstrap a "default" profile on first run so
                        # the UI's profile-switcher always has at least
                        # one selectable entry.
                        if not profile_store.list():
                            profile_store.create(
                                slug="default",
                                display_name="Default",
                                description="Bootstrap profile",
                            )
                        # Seed the curated starter SKILL.md bundle into
                        # the default profile's skills/ dir on every
                        # boot. The copy is idempotent (existing files
                        # win, never overwritten) so operator edits
                        # stick and pre-existing installs pick up new
                        # bundled skills as the bundle grows over time.
                        # Best-effort — any failure logs a warning but
                        # does not block boot.
                        try:
                            from corlinman_server.gateway.lifecycle.starter_skills import (
                                seed_starter_skills,
                            )
                            from corlinman_server.profiles import (
                                profile_skills_dir,
                            )

                            seed_starter_skills(
                                profile_skills_dir(
                                    Path(data_dir), "default"
                                )
                            )
                        except Exception as seed_exc:  # pragma: no cover
                            logger.warning(
                                "gateway.starter_skills.seed_failed",
                                error=str(seed_exc),
                            )
                        # W6 Persona Studio — drop the bundled persona
                        # templates (Grantley's daily_job.json today) into
                        # ``<data_dir>/bundled_personas/`` so the
                        # ``/admin/scheduler/qzone/templates/{id}/enable``
                        # route can read them off disk. The seeder is
                        # idempotent at the per-persona-directory level
                        # (existing dir wins) so operator edits stick.
                        # Activation is still strictly opt-in; this only
                        # makes the JSON file available to the admin
                        # route.
                        try:
                            from corlinman_server.gateway.lifecycle.starter_skills import (
                                seed_bundled_personas,
                            )

                            seed_bundled_personas(
                                Path(data_dir) / "bundled_personas"
                            )
                        except Exception as seed_exc:  # pragma: no cover
                            logger.warning(
                                "gateway.bundled_personas.seed_failed",
                                error=str(seed_exc),
                            )
                    except Exception as exc:  # pragma: no cover
                        logger.warning(
                            "gateway.routes_admin_a.profile_store_init_failed",
                            error=str(exc),
                        )
                        profile_store = None
                # Persona store opens later inside the lifespan
                # (open + seed are both async, and _mount_routes is
                # sync). Leave the field None now; the lifespan setter
                # below populates it before FastAPI accepts requests.
                #
                # W1.2: build the stacked-directory agent registry +
                # expose an async reload helper. Best-effort — the
                # registry is optional surface (the routes degrade to
                # the legacy filesystem scan when it's absent).
                _agent_registry, _agent_registry_reload = (
                    _build_agent_registry_stack(data_dir)
                )
                admin_a_state = admin_a_state_cls(
                    data_dir=data_dir,
                    config_path=admin_config_path,
                    admin_write_lock=asyncio.Lock(),
                    profile_store=profile_store,
                    persona_store=None,
                    agent_registry=_agent_registry,
                    agent_registry_reload=_agent_registry_reload,
                )
                set_admin_a(admin_a_state)
            app.include_router(admin_a.build_router())
        except Exception as exc:  # pragma: no cover — sibling-owned
            logger.warning("gateway.routes_admin_a.mount_failed", error=str(exc))

    admin_b_state: Any | None = None
    admin_b = _lazy_import("corlinman_server.gateway.routes_admin_b")
    if admin_b is not None:
        try:
            admin_b_state_cls = getattr(admin_b, "AdminState", None)
            set_admin_b = getattr(admin_b, "set_admin_state", None)
            if admin_b_state_cls is not None and set_admin_b is not None:
                # W4.6: thread the curator UI handles through to the
                # admin_b state. ``profile_store`` matches the admin_a
                # field so /admin/curator/* can look up profile rows;
                # ``skill_registry_factory`` lazy-loads per-profile
                # SkillRegistry views. ``curator_state_repo`` and
                # ``signals_repo`` are populated by the lifespan once
                # the evolution sqlite is opened — left ``None`` here so
                # the routes 503 cleanly during a partial install.
                _admin_a_config_path = (
                    getattr(admin_a_state, "config_path", None)
                    if admin_a_state is not None
                    else None
                )

                def _admin_b_config_loader() -> dict[str, Any]:
                    """Fresh-read the live config TOML on every snapshot
                    call. Captures :data:`_admin_a_config_path` via
                    closure so the credentials + onboard PUT paths see
                    sections (notably ``[admin]``) other handlers may
                    have rewritten between snapshot reads — without it
                    the write-back collapses to ``{providers: {...}}``
                    and quietly wipes the operator's credentials.
                    """
                    import tomllib

                    if (
                        _admin_a_config_path is None
                        or not _admin_a_config_path.exists()
                    ):
                        return {}
                    try:
                        return tomllib.loads(
                            _admin_a_config_path.read_text(encoding="utf-8")
                        )
                    except (OSError, ValueError):
                        return {}

                _admin_b_state_kwargs: dict[str, Any] = {
                    "profile_store": (
                        getattr(admin_a_state, "profile_store", None)
                        if admin_a_state is not None
                        else None
                    ),
                    # Mirror the admin_a config_path so /admin/credentials*
                    # and /admin/onboard/finalize-skip can persist the
                    # [providers.*] block back to the same TOML the
                    # admin_seed bootstrap wrote. Without this the routes
                    # 503 with ``config_path_unset`` even though the
                    # gateway booted with a perfectly resolvable file.
                    "config_path": _admin_a_config_path,
                    # Fresh-read loader so the credentials and onboard
                    # routes see other sections (``[admin]`` first and
                    # foremost) when they rebuild + atomically rewrite
                    # the TOML.
                    "config_loader": _admin_b_config_loader,
                    # Admin-write lock shared with admin_a so the rotate
                    # / username / credentials writers don't race when
                    # both surfaces try to mutate the same TOML.
                    "admin_write_lock": (
                        getattr(admin_a_state, "admin_write_lock", None)
                        if admin_a_state is not None
                        else None
                    ),
                }
                # Per-profile registry factory: reads
                # ``<data_dir>/profiles/<slug>/skills`` for each call so
                # mid-run SKILL.md edits show up on the next fetch.
                data_dir_for_skills = getattr(state, "data_dir", None)
                if data_dir_for_skills is not None:
                    try:
                        from corlinman_skills_registry import (
                            SkillRegistry,
                        )

                        def _skill_registry_factory(slug: str) -> Any:
                            skills_dir = (
                                Path(data_dir_for_skills)
                                / "profiles"
                                / slug
                                / "skills"
                            )
                            return SkillRegistry.load_from_dir(skills_dir)

                        _admin_b_state_kwargs["skill_registry_factory"] = (
                            _skill_registry_factory
                        )
                    except ImportError as exc:  # pragma: no cover
                        logger.warning(
                            "gateway.routes_admin_b.skill_registry_factory_missing",
                            error=str(exc),
                        )
                admin_b_state = admin_b_state_cls(**_admin_b_state_kwargs)
                set_admin_b(admin_b_state)
            app.include_router(admin_b.build_router())
        except Exception as exc:  # pragma: no cover — sibling-owned
            logger.warning("gateway.routes_admin_b.mount_failed", error=str(exc))

    return admin_a_state, admin_b_state


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
    resolved_data_dir = data_dir or _resolve_data_dir(None)

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
                )

                observability_journal = await AgentJournal.open_from_env(
                    resolved_data_dir / "agent_journal.sqlite"
                )
                observability_emitter = JournalBackedEmitter(
                    observability_journal
                )

                # Publish onto AdminState so the W1.3 admin routes can
                # find both handles via ``get_admin_state()``.
                if admin_b_state is not None:
                    admin_b_state.journal = observability_journal
                    admin_b_state.event_emitter = observability_emitter
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
                subagent_dispatcher = AsyncSubagentDispatcher(
                    store=subagent_store,
                    run_child_factory=_unwired_run_child_factory,
                    journal=observability_journal,
                    audit_log=_audit_log,
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
                from corlinman_server.gateway.routes_admin_b.skills import (
                    SkillInstallTaskStore,
                )
                from corlinman_server.system.skill_hub import (
                    ClawHubClient,
                )

                # ``ClawHubClient`` doesn't take an audit log directly
                # (the installer writes the ``skill.installed`` rows;
                # the client only does anonymous read GETs). The audit
                # log is already on ``admin_b_state.audit_log`` from
                # earlier in this same block, so the install routes
                # pick it up through state when they call into the
                # installer.
                clawhub_client = ClawHubClient()
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

            mcp_manager = McpClientManager.from_config(state.config)
            await mcp_manager.connect_all()
            state.extras["mcp_manager"] = mcp_manager
            logger.info("gateway.mcp.manager_connected")
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
                # Also wire ``config_swap_fn`` so that
                # ``POST /admin/config`` (manual TOML edit + write-to-disk)
                # can update the live snapshot on AppState *and* inside the
                # ConfigWatcher's internal ArcSwap-equivalent
                # (_AtomicSnapshot).  The watcher's ``on_reload`` callback
                # already handles fs-triggered reloads; ``config_swap_fn``
                # is the admin-POST path where the new TOML is written
                # *by the operator* rather than detected by the fs watcher.
                def _config_swap_fn(new_cfg: Any) -> None:
                    with suppress(AttributeError, TypeError):
                        state.config = new_cfg
                    with suppress(AttributeError, TypeError):
                        app.state.corlinman_config = new_cfg
                    _snap = getattr(_watcher_instance, "_snapshot", None)
                    if _snap is not None and hasattr(_snap, "store"):
                        with suppress(Exception):
                            _snap.store(new_cfg)

                with suppress(AttributeError, TypeError):
                    admin_b_state.extras["config_swap_fn"] = _config_swap_fn
                logger.debug(
                    "gateway.config_reload.watcher_bridged_to_admin_b",
                    path=str(config_path),
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
            if sched_cfg.jobs:
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
            mcp_manager = (
                _extras.get("mcp_manager") if isinstance(_extras, dict) else None
            )
            if mcp_manager is not None:
                with suppress(Exception):
                    await mcp_manager.aclose()
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
            # D12 teardown: cancel + await any in-flight background subagent
            # dispatch tasks BEFORE closing the journal they emit into, so a
            # shutdown doesn't orphan child-driving tasks against a
            # tearing-down provider / journal. Idempotent + safe when the
            # dispatcher was never wired.
            subagent_dispatcher = getattr(
                app.state, "corlinman_subagent_dispatcher", None
            )
            if subagent_dispatcher is not None:
                try:
                    await subagent_dispatcher.shutdown()
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

    cors_origins = _resolve_cors_origins()
    if cors_origins:
        try:
            from fastapi.middleware.cors import CORSMiddleware

            app.add_middleware(
                CORSMiddleware,
                allow_origins=cors_origins,
                allow_credentials=True,
                allow_methods=[
                    "GET",
                    "POST",
                    "PUT",
                    "PATCH",
                    "DELETE",
                    "OPTIONS",
                ],
                allow_headers=[
                    "authorization",
                    "content-type",
                    "x-corlinman-source",
                ],
            )
        except ImportError as exc:  # pragma: no cover
            logger.warning("gateway.cors.middleware_missing", error=str(exc))

    # Zero-config public-origin learning. When no explicit public_url is
    # set, this middleware learns the public base URL from the first real
    # inbound request through the public hostname (honoring
    # X-Forwarded-Proto/Host behind a reverse proxy) and persists it to
    # ``<data_dir>/public_origin``. The ``on_learn`` callback re-arms the
    # channel status-link feature live, so the first browser/status-link
    # hit lights up the "🔗 实时状态" link in chat replies — no operator
    # action, no restart. Stands down entirely when public_url is explicit.
    try:
        from corlinman_server.gateway.origin_learn import (
            OriginLearningMiddleware,
        )

        def _rearm_status_links_on_learn(_origin: str) -> None:
            try:
                _wire_status_links(cfg, resolved_data_dir)
            except Exception as exc:  # noqa: BLE001 - best-effort re-arm
                logger.warning(
                    "gateway.channels.status_links_rearm_failed",
                    error=str(exc),
                )

        app.add_middleware(
            OriginLearningMiddleware,
            data_dir=resolved_data_dir,
            explicitly_configured=_status_links_explicitly_configured(cfg),
            on_learn=_rearm_status_links_on_learn,
        )
    except Exception as exc:  # noqa: BLE001 - never block boot on learning
        logger.warning("gateway.origin_learn.install_failed", error=str(exc))

    # Middleware before routes — order matters for ASGI stack walks.
    #
    # R1-001 security fix: install the ``/v1/*`` API-key gate at app
    # construction time. The middleware ships with ``admin_db=None`` (it
    # fails closed → 401 ``admin_db_not_configured``); the lifespan
    # below rebinds the real :class:`AdminDb` handle onto
    # ``app.state.api_key_auth.admin_db`` once the on-disk
    # ``tenants.sqlite`` is opened. Installing here (synchronously) is
    # mandatory because ``app.add_middleware`` is rejected once
    # FastAPI has started serving — we can't defer the install into
    # the lifespan even though the admin DB open itself must be async.
    middleware_mod = _lazy_import("corlinman_server.gateway.middleware")
    if middleware_mod is not None:
        install_api_key = getattr(
            middleware_mod, "install_api_key_middleware", None
        )
        if install_api_key is not None:
            # R2-001 security fix: extend the protected-prefix list to
            # cover the legacy bare aliases that ``gateway/routes/*`` mount
            # alongside the canonical ``/v1/...`` paths (e.g. ``/memory/upsert``
            # mirrors ``/v1/memory/upsert``; same for canvas, channels, and
            # the plugin callback). R1-001 only added ``/v1/`` so an
            # unauthenticated attacker could still hit the alias and wipe
            # memory docs, render canvas content, subscribe to canvas SSE
            # streams (exfiltrating live operator output), or poison parked
            # agent loops via fake plugin callbacks. ``/wechat/*`` is
            # intentionally excluded — it carries its own vendor-signed
            # nonce/timestamp envelope that does not use bearer tokens.
            try:
                install_api_key(
                    app,
                    admin_db=None,
                    protected_prefixes=(
                        "/v1/",
                        "/memory/",
                        "/canvas/",
                        # Gate the specific legacy webhook alias, NOT the bare
                        # ``/channels/`` prefix. The bare prefix also matches the
                        # static UI page routes (``/channels/qq``,
                        # ``/channels/telegram``, … and the per-channel admin
                        # pages), which a browser fetches without a bearer — so a
                        # bare prefix returned 401 ``missing_authorization`` for
                        # every channel admin page *before* the static UI mount
                        # was reached (the user-visible "channel pages cannot be
                        # accessed" bug). The only real bearer API under
                        # ``/channels/`` is the Telegram webhook legacy alias
                        # (gateway/routes/channels.py); keep that protected. The
                        # canonical ``/v1/channels/...`` stays gated by ``/v1/``
                        # above, and the in-app channel API lives under
                        # ``/api/channels/*`` (its own admin-session auth).
                        "/channels/telegram/webhook",
                        "/plugin-callback/",
                    ),
                )
            except Exception as exc:  # pragma: no cover — sibling-owned
                logger.warning(
                    "gateway.middleware.install_failed", error=str(exc)
                )

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

    # UI static fall-through. The docker image bakes the Next.js static
    # export into ``/app/ui-static``; this mount serves it for any path
    # not already claimed by an API route. SPA-style HTML routes
    # (/account/security, /profiles, /credentials, /evolution …) resolve
    # via the pre-rendered ``<route>.html`` files Next emits. Without
    # this mount the gateway answers every browser hit with 404 even
    # when the bundle is present on disk.
    ui_dir_env = os.environ.get("CORLINMAN_UI_DIR")
    if ui_dir_env:
        ui_path = Path(ui_dir_env)
        if ui_path.is_dir():
            try:
                from fastapi.staticfiles import StaticFiles
                from starlette.exceptions import (
                    HTTPException as StarletteHTTPException,
                )
                from starlette.types import Scope

                # Next static-export dynamic routes (e.g. /status/[token])
                # are exported as a SINGLE placeholder shell — for
                # /status/[token] with generateStaticParams()->[{token:
                # "__shell__"}] + dynamicParams=false, that's
                # ``status/__shell__.html``. A real request like
                # /status/<signed-token> has no file of its own, so we map
                # any unmatched path under such a prefix onto its shell;
                # the client then reads the token from window.location.
                # (key = URL prefix, value = exported shell file.)
                _DYNAMIC_SHELLS: dict[str, str] = {
                    "status/": "status/__shell__.html",
                }

                class _NextStaticFiles(StaticFiles):
                    async def _dynamic_shell(self, path: str, scope: Scope):
                        """Serve the exported shell for a path under a known
                        dynamic-route prefix (e.g. /status/<token> ->
                        status/__shell__.html), else ``None``.

                        ``path != shell`` keeps the shell file's own route
                        (/status/__shell__) resolving normally.
                        """
                        for prefix, shell in _DYNAMIC_SHELLS.items():
                            if path.startswith(prefix) and path != shell:
                                try:
                                    resp = await super().get_response(shell, scope)
                                except StarletteHTTPException:
                                    return None
                                if resp.status_code != 404:
                                    return resp
                                return None
                        return None

                    async def get_response(self, path: str, scope: Scope):
                        leaf = path.rsplit("/", 1)[-1]
                        if path and not path.endswith("/") and "." not in leaf:
                            try:
                                response = await super().get_response(
                                    f"{path}.html",
                                    scope,
                                )
                            except StarletteHTTPException as fallback_exc:
                                if fallback_exc.status_code != 404:
                                    raise
                            else:
                                if response.status_code != 404:
                                    return response

                        # With ``html=True`` StaticFiles RETURNS a 404.html
                        # response (status 404) for a missing file rather than
                        # raising — so we must inspect the status, not just
                        # catch. Either way, before serving that 404 we try
                        # the dynamic-segment shell (covers tokens with dots
                        # in the path, which skip the .html branch above).
                        try:
                            response = await super().get_response(path, scope)
                        except StarletteHTTPException as exc:
                            if exc.status_code != 404:
                                raise
                            shell = await self._dynamic_shell(path, scope)
                            if shell is not None:
                                return shell
                            try:
                                return await super().get_response("404.html", scope)
                            except StarletteHTTPException as fallback_exc:
                                if fallback_exc.status_code == 404:
                                    raise exc from fallback_exc
                                raise
                        if response.status_code == 404:
                            shell = await self._dynamic_shell(path, scope)
                            if shell is not None:
                                return shell
                        return response

                # Mount last so all explicit API routes (incl. /health,
                # /admin/*, /v1/*, /onboard) win in route resolution.
                app.mount(
                    "/",
                    _NextStaticFiles(directory=str(ui_path), html=True),
                    name="ui",
                )
                logger.info(
                    "gateway.ui.static_mounted", path=str(ui_path)
                )
            except Exception as exc:  # pragma: no cover — best effort
                logger.warning(
                    "gateway.ui.static_mount_failed",
                    path=str(ui_path),
                    error=str(exc),
                )
        else:
            logger.warning(
                "gateway.ui.static_dir_missing", path=str(ui_path)
            )

    return app


# ---------------------------------------------------------------------------
# CLI / uvicorn driver
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="corlinman-gateway",
        description=(
            "Run the corlinman gateway (Python port of the Rust "
            "corlinman-gateway binary)."
        ),
    )
    p.add_argument(
        "--config",
        dest="config",
        default=None,
        help="Path to the gateway config TOML. Falls back to "
        "$CORLINMAN_CONFIG, then no config (defaults).",
    )
    p.add_argument(
        "--host",
        dest="host",
        default=None,
        help=f"Bind host. Default: $BIND or {DEFAULT_HOST}.",
    )
    p.add_argument(
        "--port",
        dest="port",
        type=int,
        default=None,
        help=f"Bind port. Default: $PORT or {DEFAULT_PORT}.",
    )
    p.add_argument(
        "--data-dir",
        dest="data_dir",
        default=None,
        help="Override the data directory (default: $CORLINMAN_DATA_DIR or ~/.corlinman).",
    )
    p.add_argument(
        "--log-level",
        dest="log_level",
        default=os.environ.get("LOG_LEVEL", "info"),
        choices=("critical", "error", "warning", "info", "debug", "trace"),
        help="uvicorn log level. Default: $LOG_LEVEL or info.",
    )
    return p


def _resolve_bind(cli_host: str | None, cli_port: int | None) -> tuple[str, int]:
    host = cli_host or os.environ.get("BIND") or DEFAULT_HOST
    if cli_port is not None:
        port = cli_port
    else:
        env_port = os.environ.get("PORT")
        port = int(env_port) if env_port and env_port.isdigit() else DEFAULT_PORT
    return host, port


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
    data_dir = Path(args.data_dir) if args.data_dir else _resolve_data_dir(None)
    host, port = _resolve_bind(args.host, args.port)

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
