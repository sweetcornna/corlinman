"""``McpAdapter`` — the live-manager ↔ persistent-store bridge.

This is the seam the existing ``/admin/plugins/{name}/{enable,disable,
restart}`` routes (``plugins.py``) reach for via
``state.extras["mcp_adapter"]``, and the seam the new MCP marketplace
routes (``mcp_market.py``) drive for install / remove / list.

It couples two halves of the MCP-server lifecycle:

* the **live** :class:`~corlinman_mcp_server.client_manager.McpClientManager`
  — the connected pool whose tools are exposed to the agent plane. The
  manager owns the hot-plug operations (``add_server`` /
  ``remove_server`` / ``restart_one`` / ``enable_one`` / ``disable_one``).
* the **persistent** :class:`~corlinman_server.system.marketplace.
  mcp_store.McpServerStore` — the SQLite registry that survives restarts
  and records each server's launch spec + provenance + the enabled flag.

Every mutation keeps the two in sync: an enable flips the live peer up
*and* persists ``enabled=1`` so the next boot reconnects it; an install
persists a row ``enabled=0`` without ever touching the live pool (the
operator enables it explicitly afterwards). A *config-declared* server
(one that exists in the live pool because it was read from the TOML
``[mcp]`` table, with no store row) is the tricky case: toggling it must
materialise a store row carrying its captured launch spec — otherwise
the next boot re-reads the unchanged TOML and the toggle silently
reverts. Because boot registers stored specs *after* config ones with
``replace=True``, that materialised row's ``enabled`` flag then wins.
``reconfigure`` follows the same rule for edits to env / command / url.
Both handles are optional — a degraded boot may wire neither — so every
method tolerates ``None`` gracefully rather than raising.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from corlinman_mcp_server.client_manager import McpClientManager

    from corlinman_server.system.marketplace.mcp_store import McpServerStore

log = structlog.get_logger(__name__)

__all__ = ["McpAdapter"]


class McpAdapter:
    """Bridge between the live MCP client manager and the SQLite store.

    Construct with either, both, or neither handle wired. The
    constructor never touches the wires; mutations gate on presence and
    no-op (or skip the missing half) when a handle is absent.
    """

    def __init__(
        self,
        manager: McpClientManager | None = None,
        store: McpServerStore | None = None,
        *,
        on_changed: Callable[[], Any] | None = None,
    ) -> None:
        self._manager = manager
        self._store = store
        # Fired after a mutation that changes the live tool set so the
        # gateway re-advertises the tool plane (issue #108: hot-plug left
        # advertised schemas stale until restart). Same refresh entrypoint
        # the tools/list_changed listener uses.
        self._on_changed = on_changed

    async def _fire_changed(self) -> None:
        if self._on_changed is None:
            return
        try:
            result = self._on_changed()
            if asyncio.iscoroutine(result):
                await result
        except Exception as exc:  # noqa: BLE001 — refresh never breaks a mutation
            log.warning("mcp_adapter.on_changed_failed", error=str(exc))

    # -- lifecycle toggles served via the /admin/plugins seam -----------

    async def enable_one(self, name: str) -> bool:
        """Bring the named server up in the live pool *and* persist
        ``enabled=1``.

        Returns whether the live manager knew the server (the store
        write is best-effort and never gates the return). Tolerates a
        ``None`` manager / ``None`` store.
        """
        existed = await self._manager_call("enable_one", name)
        self._persist_enabled(name, True)
        await self._fire_changed()
        return existed

    async def disable_one(self, name: str) -> bool:
        """Drop the named server's live peer *and* persist ``enabled=0``."""
        existed = await self._manager_call("disable_one", name)
        self._persist_enabled(name, False)
        await self._fire_changed()
        return existed

    async def restart_one(self, name: str) -> bool:
        """Tear down + reconnect the named server in the live pool.

        Does not change the persisted ``enabled`` flag — a restart is a
        runtime operation, not a state change. Tolerates a ``None``
        manager.
        """
        existed = await self._manager_call("restart_one", name)
        await self._fire_changed()
        return existed

    # -- marketplace operations ----------------------------------------

    async def install(
        self,
        spec: dict[str, Any],
        *,
        source: str = "github",
        version: str | None = None,
    ) -> Any | None:
        """Persist a freshly-downloaded MCP server **disabled**.

        The marketplace install flow downloads a manifest, parses it
        into ``spec``, then calls here. We persist the row with
        ``enabled=0`` and deliberately *do not* connect it — the
        operator enables it explicitly afterwards (via the
        ``/admin/plugins/{name}/enable`` seam) once they've reviewed the
        spec + supplied any required env.

        Returns the persisted :class:`~corlinman_server.system.
        marketplace.mcp_store.InstalledMcpServer` row, or ``None`` when
        no store is wired (a degraded boot — the install can't persist).
        """
        name = str(spec.get("name") or "").strip()
        if not name:
            raise ValueError("MCP server spec must carry a non-empty 'name'")
        if self._store is None:
            log.warning("mcp_adapter.install_no_store", name=name)
            return None
        row = self._store.upsert(
            name,
            spec,
            source=source,
            version=version,
            enabled=False,
        )
        # Register the spec with the live manager **disabled** so a later
        # enable_one() can hot-connect it without a restart. add_server
        # only brings a server up when the manager is connected AND the
        # spec is enabled, so this registration is inert until enable.
        if self._manager is not None:
            try:
                from corlinman_mcp_server.client_manager import McpServerSpec

                mspec = McpServerSpec.from_mapping(
                    name, {**spec, "enabled": False}
                )
                await self._manager.add_server(mspec, replace=True)
            except Exception as exc:  # noqa: BLE001 — best-effort
                log.warning(
                    "mcp_adapter.install_register_failed",
                    name=name,
                    error=str(exc),
                )
        # add_server(replace=True) tears down any live server of the same
        # name; if that server was ready, its tools just vanished — refresh
        # so the stale synthetic entry + advertised snapshot are pruned
        # (Codex #110). The new spec is registered disabled, so it adds no
        # tools of its own until a later enable_one() (which also refreshes).
        await self._fire_changed()
        log.info(
            "mcp_adapter.installed",
            name=name,
            source=source,
            version=version,
        )
        return row

    async def remove(self, name: str) -> bool:
        """Uninstall: drop the live peer *and* delete the persisted row.

        Returns whether the store row was deleted (the live teardown is
        best-effort). Tolerates a ``None`` manager / ``None`` store.
        """
        await self._manager_call("remove_server", name)
        deleted = False
        if self._store is not None:
            try:
                deleted = self._store.delete(name)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "mcp_adapter.store_delete_failed",
                    name=name,
                    error=str(exc),
                )
        await self._fire_changed()
        log.info("mcp_adapter.removed", name=name, deleted=deleted)
        return deleted

    async def reconfigure(
        self,
        name: str,
        patch: dict[str, Any],
        *,
        version: str | None = None,
    ) -> Any | None:
        """Edit an installed/config server's launch spec **in place**.

        ``patch`` is a shallow override map merged over the server's
        current launch spec — the keys an operator may legitimately want
        to change without a delete + reinstall: ``env`` (secrets),
        ``command`` / ``args`` (stdio launch), ``url`` / ``headers``
        (ws/http), and ``transport``. ``env`` / ``headers`` *replace*
        their counterpart wholesale (so an operator can drop a secret),
        which mirrors the install-time env merge being explicit.

        The merged spec is re-persisted to the store (capturing the live
        spec first for a config-declared server that has no store row yet,
        so the edit survives a restart) and re-registered with the live
        manager via ``add_server(replace=True)``; an enabled server is
        reconnected immediately so the new env/command takes effect
        without a separate restart.

        Returns the persisted row (or ``None`` when no store is wired);
        raises ``KeyError`` when the server is unknown to both halves.
        """
        if not name:
            raise ValueError("MCP server name must be non-empty")
        base = self._current_spec_mapping(name)
        if base is None:
            raise KeyError(name)

        enabled = bool(base.get("enabled", False))
        merged: dict[str, Any] = {**base}
        # Whitelist the operator-editable keys; ignore anything else so a
        # caller can't smuggle in a name change or the enabled flag here
        # (toggling stays on the enable/disable seam).
        for key in ("transport", "command", "url"):
            if key in patch and patch[key] is not None:
                merged[key] = patch[key]
        if "args" in patch and patch["args"] is not None:
            merged["args"] = [str(a) for a in patch["args"]]
        if "env" in patch and patch["env"] is not None:
            merged["env"] = {
                str(k): str(v) for k, v in dict(patch["env"]).items()
            }
        if "headers" in patch and patch["headers"] is not None:
            merged["headers"] = {
                str(k): str(v) for k, v in dict(patch["headers"]).items()
            }
        merged["enabled"] = enabled

        prev_source = self._store_source(name)
        resolved_version = version if version is not None else (
            self._store_version(name)
        )

        # Persist first so a manager hiccup can't desync the durable row.
        row = None
        if self._store is not None:
            try:
                row = self._store.upsert(
                    name,
                    merged,
                    source=prev_source or "config",
                    version=resolved_version,
                    enabled=enabled,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "mcp_adapter.reconfigure_store_failed",
                    name=name,
                    error=str(exc),
                )

        # Re-register live so the new spec takes effect; an enabled server
        # is reconnected by add_server (replace tears down the old peer).
        if self._manager is not None:
            try:
                from corlinman_mcp_server.client_manager import McpServerSpec

                mspec = McpServerSpec.from_mapping(name, merged)
                await self._manager.add_server(mspec, replace=True)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "mcp_adapter.reconfigure_register_failed",
                    name=name,
                    error=str(exc),
                )
        await self._fire_changed()
        log.info(
            "mcp_adapter.reconfigured",
            name=name,
            enabled=enabled,
            version=resolved_version,
        )
        return row

    # -- merged listing -------------------------------------------------

    def servers(self) -> list[dict[str, Any]]:
        """Merge the persisted catalogue with live connection status.

        Every installed row (from the store) is surfaced with its
        provenance + persisted ``enabled`` flag, enriched with the live
        manager's view when one is connected (``status`` =
        ``ready``/``error``/``pending``, the live tool count, and any
        connection ``error`` string). Servers that exist live but were
        never persisted (config-file servers) are appended too so the UI
        sees the full pool.
        """
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()

        live = self._live_index()

        # 1. Persisted catalogue first — these carry provenance.
        for installed in self._store_list():
            name = installed.name
            seen.add(name)
            row: dict[str, Any] = {
                "name": name,
                "source": installed.source,
                "version": installed.version,
                "enabled": bool(installed.enabled),
                "installed_at": _iso(installed.installed_at),
                "updated_at": _iso(installed.updated_at),
                "transport": str(installed.spec.get("transport", "") or ""),
                "status": "stopped",
                "tools": 0,
                "error": None,
            }
            managed = live.get(name)
            if managed is not None:
                row["status"] = str(getattr(managed, "status", "pending"))
                row["tools"] = len(getattr(managed, "tools", []) or [])
                row["error"] = getattr(managed, "error", None)
            rows.append(row)

        # 2. Live-only servers (e.g. config-file declared) the store
        # doesn't know about — surface them so nothing is hidden.
        for name, managed in live.items():
            if name in seen:
                continue
            spec = getattr(managed, "spec", None)
            rows.append(
                {
                    "name": name,
                    "source": "config",
                    "version": None,
                    "enabled": bool(getattr(spec, "enabled", False)),
                    "installed_at": None,
                    "updated_at": None,
                    "transport": str(getattr(spec, "transport", "") or ""),
                    "status": str(getattr(managed, "status", "pending")),
                    "tools": len(getattr(managed, "tools", []) or []),
                    "error": getattr(managed, "error", None),
                }
            )

        rows.sort(key=lambda r: str(r.get("name", "")))
        return rows

    # -- internals ------------------------------------------------------

    async def _manager_call(self, method: str, name: str) -> bool:
        """Invoke ``method(name)`` on the live manager, awaiting an async
        result. Returns ``False`` when no manager is wired (or the call
        returns a falsy result), tolerating the missing-half case."""
        if self._manager is None:
            return False
        fn = getattr(self._manager, method, None)
        if fn is None:
            log.warning("mcp_adapter.manager_method_missing", method=method)
            return False
        res = fn(name)
        if hasattr(res, "__await__"):
            res = await res
        return bool(res)

    def _persist_enabled(self, name: str, enabled: bool) -> None:
        """Durably record the enabled flag.

        For a marketplace-installed server the row already exists, so a
        cheap ``set_enabled`` flips the flag. For a *config-declared*
        server there is no store row yet — ``set_enabled`` would raise
        ``McpServerNotFound`` and the toggle would silently revert on the
        next restart (boot re-reads the unchanged TOML). To make the
        toggle durable we capture the server's live launch spec and
        ``upsert`` it into the store (``source="config"``); because boot
        registers stored specs *after* config ones with
        ``replace=True``, the stored enabled flag then wins on restart.
        Tolerates a missing store / a server unknown to both halves.
        """
        if self._store is None:
            return
        try:
            self._store.set_enabled(name, enabled)
            return
        except Exception as exc:  # noqa: BLE001
            from corlinman_server.system.marketplace.mcp_store import (
                McpServerNotFound,
            )

            if not isinstance(exc, McpServerNotFound):
                log.warning(
                    "mcp_adapter.store_set_enabled_failed",
                    name=name,
                    enabled=enabled,
                    error=str(exc),
                )
                return
        # No store row — this is a config-declared server. Persist its
        # live spec so the toggle survives a restart.
        spec_map = self._live_spec_mapping(name)
        if spec_map is None:
            log.debug(
                "mcp_adapter.store_set_enabled_skipped",
                name=name,
                enabled=enabled,
            )
            return
        spec_map["enabled"] = enabled
        try:
            self._store.upsert(
                name,
                spec_map,
                source="config",
                version=None,
                enabled=enabled,
            )
            log.info(
                "mcp_adapter.config_server_persisted",
                name=name,
                enabled=enabled,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "mcp_adapter.config_server_persist_failed",
                name=name,
                enabled=enabled,
                error=str(exc),
            )

    def _live_spec_mapping(self, name: str) -> dict[str, Any] | None:
        """Project the live manager's spec for ``name`` to a config-shaped
        mapping (the keys ``McpServerSpec.from_mapping`` reads back),
        or ``None`` when the server isn't in the live pool."""
        managed = self._live_index().get(name)
        if managed is None:
            return None
        spec = getattr(managed, "spec", None)
        if spec is None:
            return None
        try:
            raw = dataclasses.asdict(spec)
        except TypeError:
            return None
        # Drop the name (it's the store key) so a later from_mapping(name)
        # owns it; keep transport/command/args/env/url/headers/timeouts.
        raw.pop("name", None)
        return dict(raw)

    def _current_spec_mapping(self, name: str) -> dict[str, Any] | None:
        """The launch spec to edit: the persisted row's spec when one
        exists (authoritative + carries the enabled flag), else the live
        spec for a config-declared server. ``None`` when unknown to
        both."""
        if self._store is not None:
            try:
                row = self._store.get(name)
            except Exception:  # noqa: BLE001
                row = None
            if row is not None:
                base = dict(getattr(row, "spec", {}) or {})
                base["enabled"] = bool(getattr(row, "enabled", False))
                return base
        live = self._live_spec_mapping(name)
        if live is not None:
            managed = self._live_index().get(name)
            spec = getattr(managed, "spec", None)
            live["enabled"] = bool(getattr(spec, "enabled", False))
            return live
        return None

    def _store_source(self, name: str) -> str | None:
        if self._store is None:
            return None
        try:
            row = self._store.get(name)
        except Exception:  # noqa: BLE001
            return None
        return getattr(row, "source", None) if row is not None else None

    def _store_version(self, name: str) -> str | None:
        if self._store is None:
            return None
        try:
            row = self._store.get(name)
        except Exception:  # noqa: BLE001
            return None
        return getattr(row, "version", None) if row is not None else None

    def _store_list(self) -> list[Any]:
        if self._store is None:
            return []
        try:
            return list(self._store.list())
        except Exception as exc:  # noqa: BLE001
            log.warning("mcp_adapter.store_list_failed", error=str(exc))
            return []

    def _live_index(self) -> dict[str, Any]:
        """Map ``name -> McpManagedServer`` for the live pool, or ``{}``
        when no manager is wired."""
        if self._manager is None:
            return {}
        try:
            managed_list = self._manager.servers()
        except Exception as exc:  # noqa: BLE001
            log.warning("mcp_adapter.manager_servers_failed", error=str(exc))
            return {}
        out: dict[str, Any] = {}
        for managed in managed_list:
            spec = getattr(managed, "spec", None)
            name = str(getattr(spec, "name", "") or "")
            if name:
                out[name] = managed
        return out


def _iso(value: Any) -> str | None:
    """Best-effort ISO render of a datetime-ish value."""
    if value is None:
        return None
    try:
        return str(value.isoformat())
    except AttributeError:
        return str(value)
