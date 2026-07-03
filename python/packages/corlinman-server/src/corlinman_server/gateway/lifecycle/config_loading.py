"""Config loading + hot-reload helpers for the gateway entrypoint.

Extracted verbatim from
:mod:`corlinman_server.gateway.lifecycle.entrypoint` (Modularization
Phase 3 god-file reduction). These are the config-loading and live
config-hot-reload helpers the entrypoint's ``build_app`` / lifespan /
``_serve`` call:

* :func:`_load_config` — best-effort lazy load of the gateway config TOML.
* :func:`_wire_status_links` — arm the status-card channel-link feature.
* :func:`_reapply_hot_reloadable` — re-run the hot-reloadable sibling
  bootstraps whose config section changed.
* :func:`_config_hot_reload_enabled` — whether live fs-watch hot-reload
  is enabled (default OFF).
* :func:`_start_config_watcher` — build + start the :class:`ConfigWatcher`.

These functions call each other (``_start_config_watcher`` drives
``_reapply_hot_reloadable`` + ``_config_hot_reload_enabled``), so the
whole set moves together — the intra-group calls resolve inside this
module's namespace. The entrypoint re-imports every one of these back, so
its public surface and ``__all__`` are unchanged.

This module never imports the entrypoint module (no import cycle); the
sibling leaves it leans on (``_lazy_import`` from ``cli_helpers``,
``RESTART_REQUIRED_SECTIONS_LOCAL`` from ``bootstrap_constants``, and the
config-resolution helpers from ``config_resolve``) were already split out.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

import structlog

from corlinman_server.gateway.lifecycle.bootstrap_constants import (
    RESTART_REQUIRED_SECTIONS_LOCAL,
)
from corlinman_server.gateway.lifecycle.cli_helpers import (
    _lazy_import,
)
from corlinman_server.gateway.lifecycle.config_resolve import (
    _extract_section,
    _status_links_explicitly_configured,
)

logger = structlog.get_logger(__name__)


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
        the reload itself never fails on the notification side.

        A ``hooks``-section change additionally rebuilds the live
        HookRunner (shell keys + declarative groups + re-discovery) —
        historically the runner was boot-time-only, so a ``[hooks]`` edit
        silently did nothing until restart."""
        if section == "hooks":
            runner = getattr(state, "hook_runner", None)
            reload_fn = getattr(runner, "reload", None)
            if callable(reload_fn):
                try:
                    summary = reload_fn(
                        {"hooks": new if isinstance(new, dict) else {}}
                    )
                    logger.info("gateway.config_reload.hooks_reloaded", **summary)
                except Exception as exc:  # noqa: BLE001 — reload is best-effort
                    logger.warning(
                        "gateway.config_reload.hooks_reload_failed", error=str(exc)
                    )
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
