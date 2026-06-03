"""``/admin/plugins*`` — plugin registry inspector + invoke + MCP lifecycle.

Port of ``rust/crates/corlinman-gateway/src/routes/admin/plugins.rs``.

Read-only routes (always on):

* ``GET    /admin/plugins``                — list summary rows.
* ``GET    /admin/plugins/{name}``         — manifest + diagnostics.
* ``POST   /admin/plugins/{name}/invoke``  — test-invoke one tool.

MCP lifecycle routes (require :attr:`AdminState.extras["mcp_adapter"]`):

* ``POST   /admin/plugins/{name}/disable``
* ``POST   /admin/plugins/{name}/enable``
* ``POST   /admin/plugins/{name}/restart``

Backed by ``corlinman_providers.plugins.PluginRegistry`` (W2 port).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_b.state import (
    get_admin_state,
    require_admin,
)

# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class PluginSummary(BaseModel):
    name: str
    version: str = ""
    status: str = "loaded"
    plugin_type: str = ""
    origin: str = ""
    tool_count: int = 0
    manifest_path: str = ""
    description: str = ""
    capabilities: list[str] = []
    shadowed_count: int = 0


class PluginDetail(BaseModel):
    summary: PluginSummary
    manifest: dict[str, Any] = {}
    diagnostics: list[dict[str, Any]] = []


class InvokeBody(BaseModel):
    tool: str
    arguments: dict[str, Any] = {}
    session_key: str | None = None
    timeout_ms: int | None = Field(default=None, ge=0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _summary_from_entry(entry: Any) -> PluginSummary:
    manifest = getattr(entry, "manifest", None) or {}
    if not isinstance(manifest, dict):
        # Pydantic / dataclass — try to read attributes directly.
        m = manifest
        caps_src = getattr(m, "capabilities", None)
        tools = getattr(caps_src, "tools", []) if caps_src is not None else []
        return PluginSummary(
            name=str(getattr(m, "name", "")),
            version=str(getattr(m, "version", "")),
            status="loaded",
            plugin_type=_plugin_type_str(getattr(m, "plugin_type", "")),
            origin=str(getattr(entry, "origin", "")),
            tool_count=len(tools),
            manifest_path=str(getattr(entry, "manifest_path", "")),
            description=str(getattr(m, "description", "")),
            capabilities=[str(getattr(t, "name", "")) for t in tools],
            shadowed_count=int(getattr(entry, "shadowed_count", 0) or 0),
        )
    caps = manifest.get("capabilities") or {}
    tools = caps.get("tools") if isinstance(caps, dict) else []
    tools = tools or []
    return PluginSummary(
        name=str(manifest.get("name", "")),
        version=str(manifest.get("version", "")),
        status="loaded",
        plugin_type=_plugin_type_str(manifest.get("plugin_type", "")),
        origin=str(getattr(entry, "origin", "") or ""),
        tool_count=len(tools),
        manifest_path=str(getattr(entry, "manifest_path", "") or ""),
        description=str(manifest.get("description", "")),
        capabilities=[str(t.get("name", "")) for t in tools if isinstance(t, dict)],
        shadowed_count=int(getattr(entry, "shadowed_count", 0) or 0),
    )


def _plugin_type_str(t: Any) -> str:
    if hasattr(t, "as_str"):
        return str(t.as_str())
    if hasattr(t, "value"):
        return str(t.value)
    return str(t)


def _list_registry(registry: Any) -> list[Any]:
    if registry is None:
        return []
    if hasattr(registry, "list"):
        try:
            return list(registry.list())
        except Exception:  # noqa: BLE001
            return []
    return list(registry)


def _get_entry(registry: Any, name: str) -> Any | None:
    if registry is None:
        return None
    if hasattr(registry, "get"):
        return registry.get(name)
    for entry in _list_registry(registry):
        m = getattr(entry, "manifest", None) or {}
        n = m.get("name") if isinstance(m, dict) else getattr(m, "name", None)
        if n == name:
            return entry
    return None


def _manifest_to_dict(manifest: Any) -> dict[str, Any]:
    if isinstance(manifest, dict):
        return dict(manifest)
    if hasattr(manifest, "model_dump"):
        try:
            dumped: dict[str, Any] = manifest.model_dump()
            return dumped
        except Exception:  # noqa: BLE001
            pass
    try:
        return dict(getattr(manifest, "__dict__", {}))
    except Exception:  # noqa: BLE001
        return {}


def _diagnostics_for(registry: Any, name: str) -> list[dict[str, Any]]:
    if registry is None or not hasattr(registry, "diagnostics"):
        return []
    try:
        diags = registry.diagnostics() or []
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for d in diags:
        if isinstance(d, dict):
            if d.get("name") == name or d.get("winner_name") == name:
                out.append(d)
        else:
            n = getattr(d, "name", None) or getattr(d, "winner_name", None)
            if n == name:
                try:
                    out.append(d.__dict__)
                except AttributeError:
                    out.append({"repr": repr(d)})
    return out


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    r = APIRouter(dependencies=[Depends(require_admin)], tags=["admin", "plugins"])

    @r.get("/admin/plugins", response_model=list[PluginSummary])
    async def list_plugins():
        state = get_admin_state()
        rows = [_summary_from_entry(entry) for entry in _list_registry(state.plugins)]
        return rows

    @r.get("/admin/plugins/{name}", response_model=PluginDetail)
    async def get_plugin(name: str):
        state = get_admin_state()
        entry = _get_entry(state.plugins, name)
        if entry is None:
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "resource": "plugin", "id": name},
            )
        return PluginDetail(
            summary=_summary_from_entry(entry),
            manifest=_manifest_to_dict(getattr(entry, "manifest", {}) or {}),
            diagnostics=_diagnostics_for(state.plugins, name),
        )

    @r.post("/admin/plugins/{name}/invoke")
    async def invoke_plugin(name: str, body: InvokeBody):
        state = get_admin_state()
        entry = _get_entry(state.plugins, name)
        if entry is None:
            return JSONResponse(
                status_code=404,
                content={"error": "not_found", "resource": "plugin", "id": name},
            )
        manifest = getattr(entry, "manifest", None)
        if manifest is None:
            return JSONResponse(
                status_code=500,
                content={"error": "no_manifest", "plugin": name},
            )

        # Reject service plugins — admin invoke targets stdio plugins
        # only (matches the Rust route's PluginType::Service short-
        # circuit).
        ptype = (
            manifest.get("plugin_type")
            if isinstance(manifest, dict)
            else getattr(manifest, "plugin_type", None)
        )
        ptype_str = _plugin_type_str(ptype)
        if ptype_str.lower() == "service":
            return JSONResponse(
                status_code=501,
                content={
                    "error": "invoke_unsupported",
                    "message": (
                        "test-invoke for service plugins is not supported;"
                        " use the service's own gRPC surface"
                    ),
                },
            )

        # Verify the tool is declared.
        caps = (
            manifest.get("capabilities")
            if isinstance(manifest, dict)
            else getattr(manifest, "capabilities", None)
        )
        tools = (
            caps.get("tools")
            if isinstance(caps, dict)
            else getattr(caps, "tools", []) if caps is not None else []
        ) or []
        names = [t.get("name") if isinstance(t, dict) else getattr(t, "name", None) for t in tools]
        if body.tool not in names:
            return JSONResponse(
                status_code=400,
                content={"error": "tool_not_declared", "plugin": name, "tool": body.tool},
            )

        timeout_ms = min(body.timeout_ms or 0, 60_000) or None

        # Resolve the *real* plugin executor — the same
        # ``build_registry_invoker`` the chat tool-executor uses (see
        # ``gateway.services.grpc_backend.build_tool_executor``). It owns
        # ``sync`` / ``async`` spawn-per-call dispatch and, when the
        # supervisor / MCP client manager are wired, ``service`` / ``mcp``
        # dispatch too. Lazy-imported so this module's import graph stays
        # narrow at load. A genuinely missing ``corlinman-grpc`` is the
        # only path that can no longer wire an executor.
        try:
            from corlinman_server.gateway.grpc.plugin_invoker import (  # noqa: PLC0415
                build_registry_invoker,
            )
        except ImportError as exc:
            return JSONResponse(
                status_code=501,
                content={
                    "error": "invoke_runtime_unavailable",
                    "message": (
                        "plugin invoker is unavailable in this build "
                        f"(corlinman-grpc import failed: {exc})"
                    ),
                },
            )

        # Thread the supervisor + MCP client manager when present so
        # ``service`` / ``mcp`` plugins test-invoke for real instead of
        # degrading. Both are best-effort: the supervisor rides on
        # ``extras['plugin_supervisor']`` (absent in degraded boots) and
        # the MCP client manager off ``extras['mcp_manager']`` or, failing
        # that, the ``mcp_adapter`` that wraps it.
        supervisor = state.extras.get("plugin_supervisor")
        mcp_manager = state.extras.get("mcp_manager")
        if mcp_manager is None:
            adapter = state.extras.get("mcp_adapter")
            mcp_manager = getattr(adapter, "_manager", None) if adapter else None

        invoker = build_registry_invoker(
            state.plugins, supervisor=supervisor, mcp_manager=mcp_manager
        )
        args_json = json.dumps(body.arguments, ensure_ascii=False).encode("utf-8")

        try:
            if timeout_ms is not None:
                result = await asyncio.wait_for(
                    invoker(name, body.tool, args_json),
                    timeout=timeout_ms / 1000.0,
                )
            else:
                result = await invoker(name, body.tool, args_json)
        except TimeoutError:
            return JSONResponse(
                status_code=504,
                content={
                    "error": "invoke_timeout",
                    "plugin": name,
                    "tool": body.tool,
                    "timeout_ms": timeout_ms,
                },
            )
        except Exception as exc:  # noqa: BLE001 — the invoker shouldn't raise
            return JSONResponse(
                status_code=500,
                content={"error": "invoke_failed", "message": str(exc)},
            )

        return _invocation_to_json(result)

    # MCP lifecycle ----------------------------------------------------------

    @r.post("/admin/plugins/{name}/disable")
    async def disable_mcp(name: str):
        return await _mcp_op("disable_one", name)

    @r.post("/admin/plugins/{name}/enable")
    async def enable_mcp(name: str):
        return await _mcp_op("enable_one", name)

    @r.post("/admin/plugins/{name}/restart")
    async def restart_mcp(name: str):
        return await _mcp_op("restart_one", name)

    async def _mcp_op(method: str, name: str) -> Any:
        state = get_admin_state()
        adapter = state.extras.get("mcp_adapter")
        if adapter is None:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "mcp_adapter_disabled",
                    "message": "no McpAdapter wired into this gateway",
                },
            )
        fn = getattr(adapter, method, None)
        if fn is None:
            return JSONResponse(
                status_code=501,
                content={
                    "error": "mcp_op_unsupported",
                    "method": method,
                },
            )
        try:
            res = fn(name)
            if hasattr(res, "__await__"):
                await res
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                status_code=500,
                content={"error": "mcp_op_failed", "method": method, "message": str(exc)},
            )
        if method == "disable_one":
            return {"name": name, "disabled": True, "stopped": True}
        if method == "enable_one":
            return {"name": name, "disabled": False}
        return {"name": name, "status": "restarted"}

    return r


def _invocation_to_json(out: Any) -> dict[str, Any]:
    """Coerce a :class:`~corlinman_grpc.agent_client.ToolInvocation` to the
    admin-invoke wire shape.

    The invoker always returns a ``ToolInvocation`` (``content`` str,
    ``is_error`` bool, ``duration_ms`` int). On success ``content`` is the
    raw JSON-RPC ``result`` payload; on a tool-level failure it is a JSON
    error envelope (``{"error": ..., "message": ...}`` or
    ``{"error": "plugin_error", "code": ..., "message": ...}``). We parse
    ``content`` so the UI gets structured JSON when possible while always
    preserving the raw text under ``result_raw``.
    """
    content = getattr(out, "content", "") or ""
    is_error = bool(getattr(out, "is_error", False))
    duration_ms = int(getattr(out, "duration_ms", 0) or 0)

    parsed: Any = None
    try:
        parsed = json.loads(content) if content else None
    except (ValueError, TypeError):
        parsed = None

    if is_error:
        envelope = parsed if isinstance(parsed, dict) else {}
        return {
            "status": "error",
            "duration_ms": duration_ms,
            "code": envelope.get("code", envelope.get("error", "unknown")),
            "message": envelope.get("message", content),
        }
    return {
        "status": "success",
        "duration_ms": duration_ms,
        "result": parsed,
        "result_raw": content,
    }
