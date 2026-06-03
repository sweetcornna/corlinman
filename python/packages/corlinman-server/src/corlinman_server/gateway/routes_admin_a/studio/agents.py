"""``/admin/agents*`` — filesystem scan + edit of ``<data_dir>/agents/*.md``.

Python port of ``rust/crates/corlinman-gateway/src/routes/admin/agents.rs``.

Routes:

* ``GET    /admin/agents``              — list ``*.md`` + ``*.yaml`` files
                                          (built-in / user / project)
* ``GET    /admin/agents/{name}``       — read one file (UTF-8 body)
* ``POST   /admin/agents/{name}``       — atomic write of a file body
                                          (Monaco editor — preserved)
* ``POST   /admin/agents``              — create a new user-overlay card
* ``DELETE /admin/agents/{name}``       — delete a user/project overlay
                                          (built-ins are immutable)
* ``POST   /admin/agents/reload``       — re-scan the dir stack

Path-traversal defence is identical to the Rust version: the ``name``
segment must be a bare stem (no ``/``, ``\\`` or ``..``). The
``.new``-then-rename atomic write mirrors the Rust handler verbatim.

W1.2 extends the list shape with per-row ``source`` so the UI can
flag built-ins (immutable) vs operator overlays.
"""

from __future__ import annotations

import os
from typing import Annotated, cast

from corlinman_agent.agents import AgentCardRegistry
from fastapi import APIRouter, Depends, HTTPException, status

from corlinman_server.gateway.routes_admin_a._auth_shim import (
    require_admin_dependency,
)
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    get_admin_state,
)

# God-file internal split: the module-level wire models + helpers now live in
# the sibling ``_agents_lib`` module. Re-imported here so the route core below
# is byte-for-byte unchanged and so ``__all__`` re-exports keep working for any
# ``from ...studio.agents import NAME`` consumer.
from corlinman_server.gateway.routes_admin_a.studio._agents_lib import (
    AgentContent,
    AgentSummaryOut,
    CreateAgentBody,
    CreatedAgentResponse,
    ReloadAgentsResponse,
    SaveAgentBody,
    _agent_path_or_build,
    _agents_dir_for,
    _find_overlay_path,
    _rel_path_str,
    _reload_registry,
    _resolve_agent_path,
    _scan_agents,
    _system_time_to_rfc3339,
    _validate_agent_name,
    _validate_create_name,
)

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    """Sub-router for ``/admin/agents*``. Mounted by the parent
    :func:`corlinman_server.gateway.routes_admin_a.router` helper."""
    r = APIRouter(dependencies=[Depends(require_admin_dependency)])

    @r.get(
        "/admin/agents",
        response_model=list[AgentSummaryOut],
        summary="List agent files (built-in + user + project)",
    )
    async def list_agents(  # noqa: D401 — wired as FastAPI handler
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> list[AgentSummaryOut]:
        registry = cast(AgentCardRegistry | None, state.agent_registry)
        return _scan_agents(_agents_dir_for(state), registry=registry)

    # ------------------------------------------------------------------
    # W1.2: static-path routes registered before ``/{name}`` so the
    # FastAPI matcher doesn't try to read ``reload`` as an agent name.
    # ------------------------------------------------------------------

    @r.post(
        "/admin/agents/reload",
        response_model=ReloadAgentsResponse,
        summary="Re-scan the agent dir stack",
    )
    async def reload_agents(
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> ReloadAgentsResponse:
        registry = await _reload_registry(state)
        if registry is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "registry_unavailable"},
            )
        return ReloadAgentsResponse(
            status="ok",
            count=len(registry),
            names=registry.names(),
        )

    @r.post(
        "/admin/agents",
        status_code=status.HTTP_201_CREATED,
        response_model=CreatedAgentResponse,
        summary="Create a new user-overlay agent card",
    )
    async def create_agent(
        body: CreateAgentBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> CreatedAgentResponse:
        _validate_create_name(body.name)
        agents_dir = _agents_dir_for(state)
        try:
            agents_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "mkdir_failed", "message": str(exc)},
            ) from exc

        # Collision check 1: an existing user/project overlay file —
        # always rejected with 400; operators must delete first to keep
        # writes idempotent and avoid silent overwrites of operator
        # work.
        existing = _find_overlay_path(agents_dir, body.name)
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "error": "agent_exists",
                    "message": (
                        f"agent {body.name!r} already exists at "
                        f"{_rel_path_str(agents_dir, existing)}; "
                        "delete it first"
                    ),
                },
            )

        # Collision check 2: shadowing a built-in. The registry's
        # source field is authoritative — we don't need to know which
        # repo dir built-ins live in.
        registry = cast(AgentCardRegistry | None, state.agent_registry)
        if registry is not None and not body.force:
            existing_card = registry.get(body.name)
            if existing_card is not None and existing_card.source == "built-in":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error": "shadows_builtin",
                        "message": (
                            f"agent {body.name!r} is a built-in; "
                            "pass force=true to override with a "
                            "user-overlay card"
                        ),
                    },
                )

        ext = ".md" if body.format == "md" else ".yaml"
        path = agents_dir / f"{body.name}{ext}"
        tmp = path.with_name(path.name + ".new")
        try:
            tmp.write_bytes(body.body.encode("utf-8"))
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "write_failed", "message": str(exc)},
            ) from exc
        try:
            os.replace(tmp, path)
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "rename_failed", "message": str(exc)},
            ) from exc

        await _reload_registry(state)

        st = path.stat()
        return CreatedAgentResponse(
            status="ok",
            name=body.name,
            file_path=_rel_path_str(agents_dir, path),
            bytes=st.st_size,
            source="user",
            last_modified=_system_time_to_rfc3339(st.st_mtime),
        )

    # ------------------------------------------------------------------
    # Parameterised ``/{name}`` routes — kept after the static routes
    # above so FastAPI's matcher picks ``/reload`` correctly.
    # ------------------------------------------------------------------

    @r.get(
        "/admin/agents/{name}",
        response_model=AgentContent,
        summary="Read one agent markdown file",
    )
    async def get_agent(
        name: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> AgentContent:
        agents_dir = _agents_dir_for(state)
        path = _resolve_agent_path(agents_dir, name)
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "resource": "agent", "id": name},
            ) from exc
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "read_failed", "message": str(exc)},
            ) from exc
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={"error": "not_utf8", "message": str(exc)},
            ) from exc
        st = path.stat()
        return AgentContent(
            name=name,
            file_path=_rel_path_str(agents_dir, path),
            bytes=st.st_size,
            last_modified=_system_time_to_rfc3339(st.st_mtime),
            content=content,
        )

    @r.post(
        "/admin/agents/{name}",
        summary="Atomic save of an agent markdown file",
    )
    async def save_agent(
        name: str,
        body: SaveAgentBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> dict[str, object]:
        agents_dir = _agents_dir_for(state)
        path = _agent_path_or_build(agents_dir, name)
        try:
            agents_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "mkdir_failed", "message": str(exc)},
            ) from exc
        tmp = path.with_name(path.name + ".new")
        try:
            tmp.write_bytes(body.content.encode("utf-8"))
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "write_failed", "message": str(exc)},
            ) from exc
        try:
            os.replace(tmp, path)
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "rename_failed", "message": str(exc)},
            ) from exc
        await _reload_registry(state)
        st = path.stat()
        return {
            "status": "ok",
            "name": name,
            "file_path": _rel_path_str(agents_dir, path),
            "bytes": st.st_size,
            "last_modified": _system_time_to_rfc3339(st.st_mtime),
        }

    @r.delete(
        "/admin/agents/{name}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Delete a user/project overlay agent file",
    )
    async def delete_agent(
        name: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> None:
        _validate_agent_name(name)

        # Built-in immutability check first — refuses the delete even if
        # the operator never wrote a shadowing file (a quick way to
        # double-check the source of a card from the UI).
        registry = cast(AgentCardRegistry | None, state.agent_registry)
        if registry is not None:
            card = registry.get(name)
            if card is not None and card.source == "built-in":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error": "builtin_immutable",
                        "message": (
                            f"agent {name!r} is a built-in; built-ins "
                            "cannot be deleted from the API"
                        ),
                    },
                )

        agents_dir = _agents_dir_for(state)
        existing = _find_overlay_path(agents_dir, name)
        if existing is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"error": "not_found", "resource": "agent", "id": name},
            )
        try:
            existing.unlink()
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={"error": "unlink_failed", "message": str(exc)},
            ) from exc
        await _reload_registry(state)
        return None

    return r


__all__ = [
    "AgentContent",
    "AgentSummaryOut",
    "CreateAgentBody",
    "CreatedAgentResponse",
    "ReloadAgentsResponse",
    "SaveAgentBody",
    "router",
]
