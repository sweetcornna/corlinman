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
operator enables it explicitly afterwards). Both handles are optional —
a degraded boot may wire neither — so every method tolerates ``None``
gracefully rather than raising.
"""

from __future__ import annotations

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
    ) -> None:
        self._manager = manager
        self._store = store

    # -- lifecycle toggles served via the /admin/plugins seam -----------

    async def enable_one(self, name: str) -> bool:
        """Bring the named server up in the live pool *and* persist
        ``enabled=1``.

        Returns whether the live manager knew the server (the store
        write is best-effort and never gates the return). Tolerates a
        ``None`` manager / ``None`` store.
        """
        existed = await self._manager_call("enable_one", name)
        self._store_set_enabled(name, True)
        return existed

    async def disable_one(self, name: str) -> bool:
        """Drop the named server's live peer *and* persist ``enabled=0``."""
        existed = await self._manager_call("disable_one", name)
        self._store_set_enabled(name, False)
        return existed

    async def restart_one(self, name: str) -> bool:
        """Tear down + reconnect the named server in the live pool.

        Does not change the persisted ``enabled`` flag — a restart is a
        runtime operation, not a state change. Tolerates a ``None``
        manager.
        """
        return await self._manager_call("restart_one", name)

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
        log.info("mcp_adapter.removed", name=name, deleted=deleted)
        return deleted

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

    def _store_set_enabled(self, name: str, enabled: bool) -> None:
        """Persist the enabled flag, tolerating a missing store / a row
        that isn't installed (a config-file server has no store row)."""
        if self._store is None:
            return
        try:
            self._store.set_enabled(name, enabled)
        except Exception as exc:  # noqa: BLE001
            # McpServerNotFound is expected for config-file servers that
            # were never installed via the marketplace; log + move on.
            log.debug(
                "mcp_adapter.store_set_enabled_skipped",
                name=name,
                enabled=enabled,
                error=str(exc),
            )

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
