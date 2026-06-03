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

import datetime as _dt
import os
import re
from pathlib import Path
from typing import Annotated, Literal, cast

from corlinman_agent.agents import AgentCardRegistry
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_a._auth_shim import (
    require_admin_dependency,
)
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    get_admin_state,
)

# Mirrors the Claude Code stem rule + the existing Rust path traversal
# defence: lowercase ASCII start, alnum + ``_`` + ``-`` allowed.
_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")


# ---------------------------------------------------------------------------
# Wire shapes — mirror the Rust ``AgentSummaryOut`` / ``AgentContent``.
# ---------------------------------------------------------------------------


class AgentSummaryOut(BaseModel):
    """One row in ``GET /admin/agents``.

    W1.2 adds ``source`` + ``description`` so the UI can render the
    overlay tier (built-in / user / project) and a per-row tooltip
    without round-tripping through ``GET /admin/agents/{name}``.
    """

    name: str
    file_path: str
    bytes: int
    last_modified: str | None = None
    # W1.2: tier the registry resolved this card from. ``"built-in"``
    # rows are immutable from the API surface.
    source: Literal["built-in", "user", "project", "inline"] | None = None
    # W1.2: copy of the card's ``description`` field (or ``None`` if the
    # file is a raw scan that we couldn't parse).
    description: str | None = None


class AgentContent(BaseModel):
    """Full body for ``GET /admin/agents/{name}``."""

    name: str
    file_path: str
    bytes: int
    last_modified: str | None
    content: str


class SaveAgentBody(BaseModel):
    """``POST /admin/agents/{name}`` body — full replacement content."""

    content: str


class CreateAgentBody(BaseModel):
    """``POST /admin/agents`` body — create a new user-overlay card.

    ``format`` decides the on-disk extension. ``force`` lets operators
    create a card whose name shadows a built-in (a deliberate override
    they have to opt into so they don't accidentally shadow defaults).
    """

    name: str = Field(..., description="Lowercase agent stem.")
    format: Literal["yaml", "md"] = "md"
    body: str = Field(..., description="Raw file contents.")
    force: bool = False


class CreatedAgentResponse(BaseModel):
    """``POST /admin/agents`` success envelope.

    Mirrors :class:`AgentSummaryOut` plus an explicit ``status=ok`` flag
    so the UI can show a clear confirmation without inspecting the
    HTTP status code.
    """

    status: Literal["ok"] = "ok"
    name: str
    file_path: str
    bytes: int
    source: Literal["user"] = "user"
    last_modified: str | None = None


class ReloadAgentsResponse(BaseModel):
    """``POST /admin/agents/reload`` envelope."""

    status: Literal["ok"] = "ok"
    count: int
    names: list[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _system_time_to_rfc3339(mtime_seconds: float) -> str | None:
    """Mirror Rust ``system_time_to_rfc3339`` — produce an RFC-3339 / ISO-8601
    string in UTC, or ``None`` on overflow."""
    try:
        return (
            _dt.datetime.fromtimestamp(mtime_seconds, tz=_dt.UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return None


def _rel_path_str(base: Path, full: Path) -> str:
    """Render ``full`` as ``agents/<rel>`` when possible, falling back
    to the absolute path. Matches Rust ``rel_path_str``."""
    try:
        rel = full.relative_to(base)
        return str(Path("agents") / rel)
    except ValueError:
        return str(full)


def _validate_agent_name(name: str) -> None:
    """Reject empty names, path separators, or any ``..`` segment.

    Raises ``HTTPException(400, invalid_name)`` mirroring Rust
    ``agent_path_or_build``.
    """
    if not name or "/" in name or "\\" in name or ".." in name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_name",
                "message": (
                    "agent name must be a bare stem without path "
                    "separators or '..'"
                ),
            },
        )


def _agent_path_or_build(agents_dir: Path, name: str) -> Path:
    """Construct ``<agents_dir>/<name>.md`` after validation."""
    _validate_agent_name(name)
    return agents_dir / f"{name}.md"


def _resolve_agent_path(agents_dir: Path, name: str) -> Path:
    """Like :func:`_agent_path_or_build` but also asserts the file exists."""
    path = _agent_path_or_build(agents_dir, name)
    if not path.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "not_found", "resource": "agent", "id": name},
        )
    return path


def _scan_agents(
    agents_dir: Path,
    *,
    registry: AgentCardRegistry | None = None,
) -> list[AgentSummaryOut]:
    """Return the registry-resolved view of ``agents_dir`` as a sorted
    list of summary rows.

    W1.2: when ``registry`` is supplied, the rows merge in the resolved
    per-card ``source`` (built-in / user / project) and ``description``
    from the parsed card. Files that fail to parse — and built-in
    cards living outside ``agents_dir`` — still surface via the
    registry so the UI can show every entry the dispatcher would see.

    Without a registry (legacy callers) we keep the pre-W1.2
    behaviour: a raw scan of ``*.md`` files under ``agents_dir``.
    """
    rows: list[AgentSummaryOut] = []
    seen: set[str] = set()

    if registry is not None:
        for card in registry.cards():
            seen.add(card.name)
            src_path = card.source_path
            bytes_ = 0
            mtime_str: str | None = None
            if src_path is not None:
                try:
                    st = src_path.stat()
                    bytes_ = st.st_size
                    mtime_str = _system_time_to_rfc3339(st.st_mtime)
                except OSError:
                    pass
            rows.append(
                AgentSummaryOut(
                    name=card.name,
                    file_path=(
                        _rel_path_str(agents_dir, src_path)
                        if src_path is not None
                        else f"agents/{card.name}"
                    ),
                    bytes=bytes_,
                    last_modified=mtime_str,
                    source=card.source,
                    description=card.description or None,
                )
            )

    # Legacy raw scan — picks up files the registry couldn't parse so
    # operators can still see and edit them from the Monaco editor.
    if agents_dir.is_dir():
        for entry in agents_dir.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix not in (".md", ".yaml", ".yml"):
                continue
            if entry.stem in seen:
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            rows.append(
                AgentSummaryOut(
                    name=entry.stem,
                    file_path=_rel_path_str(agents_dir, entry),
                    bytes=st.st_size,
                    last_modified=_system_time_to_rfc3339(st.st_mtime),
                    source="user",
                    description=None,
                )
            )

    # Stable sort by name so the UI's table doesn't shuffle.
    rows.sort(key=lambda r: r.name)
    return rows


def _agents_dir_for(state: AdminState) -> Path:
    """Resolve the ``agents/`` directory under the state's data dir."""
    base = state.data_dir if state.data_dir is not None else Path.cwd()
    return Path(base) / "agents"


def _validate_create_name(name: str) -> None:
    """Reject creation names that don't match the strict slug regex.

    Distinct from :func:`_validate_agent_name` (which mirrors the
    Rust route's path-traversal check) because creation has a stricter
    contract — operator-typed names should never need uppercase or
    non-ASCII characters and we'd rather refuse them up front than
    have the filesystem normalise the case behind our backs.
    """
    if not _NAME_RE.match(name):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_name",
                "message": (
                    "agent name must match ^[a-z][a-z0-9_-]*$ "
                    "(lowercase letters, digits, underscore, hyphen)"
                ),
            },
        )


async def _reload_registry(state: AdminState) -> AgentCardRegistry | None:
    """Invoke the wired reload helper (if any) and refresh the registry
    handle on the state. Falls back to ``None`` when no reload helper
    was wired — write-time staleness is the operator's problem then."""
    reloader = state.agent_registry_reload
    if reloader is None:
        return cast(AgentCardRegistry | None, state.agent_registry)
    new_registry = await reloader()
    if new_registry is not None:
        state.agent_registry = new_registry
    return cast(AgentCardRegistry | None, new_registry)


def _find_overlay_path(agents_dir: Path, name: str) -> Path | None:
    """Locate the user-overlay file for ``name`` regardless of extension.

    Returns the first matching ``<agents_dir>/<name>.{md,yaml,yml}``
    that exists; ``None`` if none do. Used by the DELETE path to find
    the operator's file without forcing them to remember which suffix
    they used when they created it.
    """
    for ext in (".md", ".yaml", ".yml"):
        candidate = agents_dir / f"{name}{ext}"
        if candidate.is_file():
            return candidate
    return None


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
