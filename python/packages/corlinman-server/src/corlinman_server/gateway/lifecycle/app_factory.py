"""``build_app`` module-level builders — extracted from ``entrypoint``.

Phase-4 extract-and-reimport of the FastAPI app-factory's *module-level*
builder helpers (not the ``build_app`` body itself, which stays in
:mod:`corlinman_server.gateway.lifecycle.entrypoint` along with the
lifespan closure, ``_serve``, and ``main``).

These helpers are re-imported back into ``entrypoint`` so ``build_app``
keeps calling them by their unqualified names and external importers
(notably ``agent_servicer`` importing ``_build_agent_registry_stack``
lazily from ``entrypoint``) continue to resolve via the re-export.

This module imports sibling *leaves* (``cli_helpers``, ``config_loading``,
``config_resolve``) but never ``entrypoint`` itself — there is no import
cycle because ``entrypoint`` imports *from* this module, not the reverse.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from pathlib import Path
from typing import Any

import structlog
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import (
    HTTPException as StarletteHTTPException,
)
from starlette.types import Scope

from corlinman_server.gateway.lifecycle.cli_helpers import (
    _lazy_import,
    _tenant_scope_params,
)
from corlinman_server.gateway.lifecycle.config_loading import (
    _reapply_hot_reloadable,
    _wire_status_links,
)
from corlinman_server.gateway.lifecycle.config_resolve import (
    _admin_session_cookie_secure_from_config,
    _resolve_allowed_public_origins,
    _resolve_cors_origins,
    _resolve_trusted_proxies,
    _status_links_explicitly_configured,
    _trust_forwarded_proto_from_config,
    _trusted_forwarded_proto_proxies_from_config,
)

logger = structlog.get_logger(__name__)


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


def _make_channels_writer(app: Any, admin_a_state: Any) -> Any:
    """Build the ``channels_writer`` callback the ``/admin/channels`` routes
    invoke to persist live channel-config edits (per-group keywords + the
    per-channel humanlike toggle).

    In prod this slot was never wired — only a test set it — so every
    ``PUT /admin/channels/{channel}/humanlike`` (and the keywords PUT)
    503'd ``channels_writer_missing``. The routes mutate
    ``admin_a_state.channels_config`` in place and the live humanlike
    resolver reads the same nested tables, so the edit already takes effect
    immediately; this writer makes it durable across restarts by patching
    the ``[channels]`` table in ``config.toml`` atomically. Scoped to the
    channels section so unrelated sections on disk are left untouched.
    """

    async def _writer(channels_cfg: dict[str, Any]) -> None:
        cfg_path = getattr(admin_a_state, "config_path", None)
        if cfg_path is None:
            raise RuntimeError("config_path unset; cannot persist channels config")
        cfg_path = Path(cfg_path)
        import tomllib

        try:
            on_disk = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            live = getattr(app.state, "config", None)
            on_disk = dict(live) if isinstance(live, dict) else {}
        on_disk["channels"] = channels_cfg

        try:
            import tomli_w

            serialised = tomli_w.dumps(on_disk)
        except ImportError:  # pragma: no cover — tomli_w is a hard dep
            import toml

            serialised = toml.dumps(on_disk)

        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cfg_path.with_suffix(cfg_path.suffix + ".new")
        tmp.write_text(serialised, encoding="utf-8")
        tmp.replace(cfg_path)

        # Keep the live full config in sync so a later full-config read /
        # snapshot reflects the channel edit too.
        live = getattr(app.state, "config", None)
        if isinstance(live, dict):
            live["channels"] = channels_cfg

    return _writer


def _make_config_swap_fn(app: Any, state: Any) -> Any:
    """Build the ``config_swap_fn`` the ``POST /admin/config`` route calls
    after it writes the edited TOML to disk.

    Previously this was wired ONLY when the fs-watcher (``ConfigWatcher``)
    was running, which is off by default — so on a normal deploy an editor
    save wrote disk but never updated the running process (``_publish_snapshot``
    no-op'd, yet the UI toasted success). Wiring it unconditionally makes the
    save publish to the live in-memory snapshot (``state.config`` /
    ``app.state.corlinman_config``) and re-run the *idempotent* hot-reloadable
    bootstraps for whichever top-level sections changed (today: providers /
    models — they rebuild ``provider_registry`` in place). Sections whose
    runtime is built once at boot (channels / agents / scheduler / ...) still
    need a restart — that is a separate, riskier teardown-rebuild lane and is
    surfaced honestly via ``_detect_restart_fields``.
    """

    def _config_swap_fn(new_cfg: Any) -> None:
        old = getattr(state, "config", None)
        changed: list[str] = []
        if isinstance(old, dict) and isinstance(new_cfg, dict):
            changed = [
                k
                for k in (set(old) | set(new_cfg))
                if old.get(k) != new_cfg.get(k)
            ]
        with suppress(AttributeError, TypeError):
            state.config = new_cfg
        with suppress(AttributeError, TypeError):
            app.state.corlinman_config = new_cfg
        # Keep a running ConfigWatcher's snapshot in sync when one exists.
        _watcher = getattr(state, "config_watcher", None)
        _snap = getattr(_watcher, "_snapshot", None) if _watcher else None
        if _snap is not None and hasattr(_snap, "store"):
            with suppress(Exception):
                _snap.store(new_cfg)
        # Re-apply the idempotent bootstraps for the sections that changed.
        if changed:
            with suppress(Exception):
                _reapply_hot_reloadable(state, changed)

    return _config_swap_fn


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
                config_snapshot = getattr(state, "config", None)
                session_cookie_secure = _admin_session_cookie_secure_from_config(
                    config_snapshot
                )
                trust_forwarded_proto = _trust_forwarded_proto_from_config(
                    config_snapshot
                )
                trusted_forwarded_proto_proxies = (
                    _trusted_forwarded_proto_proxies_from_config(config_snapshot)
                )
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
                    session_cookie_secure=session_cookie_secure,
                    trust_forwarded_proto=trust_forwarded_proto,
                    trusted_forwarded_proto_proxies=trusted_forwarded_proto_proxies,
                    profile_store=profile_store,
                    persona_store=None,
                    agent_registry=_agent_registry,
                    agent_registry_reload=_agent_registry_reload,
                )
                set_admin_a(admin_a_state)
                # Wire the channels-config write-back. Without this every
                # /admin/channels keywords + humanlike PUT 503s
                # ``channels_writer_missing`` (the slot was only ever set in
                # a test). The live resolver already reads the in-place
                # edit, so this just makes it durable across restarts.
                with suppress(Exception):
                    admin_a_state.channels_writer = _make_channels_writer(
                        app, admin_a_state
                    )
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
# Middleware install (order is LOAD-BEARING — Starlette applies middleware in
# REVERSE add order, so build_app must call these in the SAME sequence as the
# original inline blocks)
# ---------------------------------------------------------------------------


def _install_cors_middleware(app: Any) -> None:
    """Install the CORS middleware when explicit origins are configured.

    Side-effect-only extraction of build_app's inline CORS block; resolves
    the origins internally via :func:`_resolve_cors_origins`.
    """
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


def _install_origin_learning_middleware(
    app: Any, cfg: Any | None, resolved_data_dir: Path
) -> None:
    """Install the zero-config public-origin learning middleware.

    Side-effect-only extraction of build_app's inline block, including the
    ``_rearm_status_links_on_learn`` on-learn callback that re-arms the
    channel status-link feature live via :func:`_wire_status_links`.
    """
    # Zero-config public-origin learning. When no explicit public_url is
    # set, this middleware learns the public base URL from the first real
    # inbound request through an allowed public hostname (honoring
    # X-Forwarded-Proto/Host only from configured trusted proxies) and
    # persists it to ``<data_dir>/public_origin``. The ``on_learn`` callback
    # re-arms the channel status-link feature live, so the first
    # browser/status-link hit lights up the "🔗 实时状态" link in chat replies —
    # no operator action, no restart. Stands down entirely when public_url is explicit.
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
            allowed_public_origins=_resolve_allowed_public_origins(cfg),
            trusted_proxies=_resolve_trusted_proxies(cfg),
        )
    except Exception as exc:  # noqa: BLE001 - never block boot on learning
        logger.warning("gateway.origin_learn.install_failed", error=str(exc))


def _install_security_middleware(app: Any, cfg: Any | None) -> None:
    """Install the api-key gate + admin-session bridge + tenant-scope.

    Side-effect-only extraction of build_app's inline region. All three
    share the single ``middleware_mod`` lookup; the install order is
    preserved verbatim (api-key gate installed BEFORE tenant-scope) because
    on ``/v1/*`` the api-key-pinned tenant must win.
    """
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

            # Wire the admin-session bridge so the in-app chat UI (which
            # authenticates with the ``corlinman_session`` cookie, not an API
            # key) can reach ``/v1/chat/completions``. Set on the published
            # state AFTER install so a wiring failure degrades the bridge
            # gracefully (chat needs an API key) instead of taking down the
            # whole ``/v1`` gate. The resolver validates the cookie lazily at
            # request time via ``get_admin_state()``, so it does not matter
            # that the live session store is created lazily on first login.
            try:
                from corlinman_server.gateway.routes_admin_a._auth_shim import (
                    admin_session_tenant,
                )

                api_key_state = getattr(app.state, "api_key_auth", None)
                if api_key_state is not None:
                    api_key_state.admin_session_resolver = admin_session_tenant
            except Exception as exc:  # pragma: no cover — sibling-owned
                logger.warning(
                    "gateway.middleware.admin_bridge_wire_failed",
                    error=str(exc),
                )

        # SEC-06b: install the tenant-scope middleware so every ``/admin/*``
        # and ``/v1/*`` handler observes a resolved ``request.state.tenant``
        # instead of trusting a raw ``?tenant=`` query param. The middleware
        # was exported but never wired. It is additive — installed AFTER the
        # api-key gate so on ``/v1/*`` the api-key-pinned tenant (set from
        # the verified key row) still wins: the api-key middleware runs
        # inner and overwrites ``request.state.tenant`` unconditionally,
        # while tenant-scope only seeds a value for the surfaces the api-key
        # gate doesn't cover (notably ``/admin/api_keys*``). Single-tenant
        # default: ``enabled=False`` → every request transparently resolves
        # to the ``"default"`` tenant and nothing is ever rejected.
        install_tenant_scope = getattr(
            middleware_mod, "install_tenant_scope_middleware", None
        )
        if install_tenant_scope is not None:
            try:
                ts_enabled, ts_allowed, ts_fallback = _tenant_scope_params(cfg)
                install_tenant_scope(
                    app,
                    enabled=ts_enabled,
                    allowed=ts_allowed,
                    fallback=ts_fallback,
                )
                logger.info(
                    "gateway.tenant_scope.installed",
                    enabled=ts_enabled,
                    allowed=sorted(t.as_str() for t in ts_allowed),
                    fallback=ts_fallback.as_str(),
                )
            except Exception as exc:  # pragma: no cover — sibling-owned
                logger.warning(
                    "gateway.tenant_scope.install_failed", error=str(exc)
                )


# ---------------------------------------------------------------------------
# UI static fall-through mount
# ---------------------------------------------------------------------------

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
        normalized = path.replace("\\", "/").lstrip("/")
        for prefix, shell in _DYNAMIC_SHELLS.items():
            if normalized.startswith(prefix) and normalized != shell:
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


def _mount_ui_static(app: Any) -> None:
    """Mount the baked Next.js static export as the fall-through handler.

    Side-effect-only extraction of build_app's inline UI block: resolves
    ``$CORLINMAN_UI_DIR`` and, when it points at a real directory, mounts
    the :class:`_NextStaticFiles` app at ``/`` so every browser path not
    claimed by an explicit API route resolves against the bundle.
    """
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
