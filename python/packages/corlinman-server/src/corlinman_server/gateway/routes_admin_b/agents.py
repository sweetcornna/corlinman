"""``/admin/agents/bindings*`` — per-agent model/provider/UI binding.

Wave W-D2 of ``docs/PLAN_PROVIDER_AUTH.md``. The W-D1 wave landed the
backend contract: :class:`corlinman_agent.agents.AgentCard` now carries
optional ``model: str | None`` and ``provider: str | None`` fields
parsed from ``agents/*.yaml`` and the dispatcher in
``agent_servicer.py`` already consumes them (``request.model ||
agent.model || global_default``).

This module exposes the read/write surface so the admin UI can
inventory the parsed bindings and edit them inline. We deliberately
mount under ``/admin/agents/bindings`` (and ``/admin/agents/{name}/
binding``) rather than the bare ``/admin/agents`` paths the plan
sketches: the existing ``routes_admin_a/studio/agents.py`` already owns
``GET /admin/agents`` (lists ``*.md`` files for the Monaco editor)
and admin_a mounts before admin_b on the live FastAPI app, so a bare
``GET /admin/agents`` here would be silently shadowed. Distinct
suffixes keep both surfaces reachable + testable.

Routes:

* ``GET   /admin/agents/bindings`` →
  ``{"agents": [{"name": str, "description": str,
                  "model": str|null, "provider": str|null,
                  "show_action_trace": bool}]}``
  Reads every ``*.yaml`` / ``*.yml`` under ``<data_dir>/agents/``
  through :class:`AgentCardRegistry.load_from_dir`. The card parser is
  the single source of truth for what "binding" means — we never peek
  at the yaml independently for the GET path.

* ``PATCH /admin/agents/{name}/binding`` body
  ``{"model": str | null, "provider": str | null,
     "show_action_trace": bool}`` → ``{"status": "ok", ...}``.
  Writes back to the on-disk yaml via an atomic
  ``tmpfile + os.replace`` swap. Unrecognised top-level yaml keys are
  treated as opaque and round-tripped untouched; field order is
  preserved by walking the original document (PyYAML dicts already
  preserve insertion order under Python 3.7+).

The endpoint never accepts arbitrary new top-level keys via the body —
the binding shape is locked to ``model`` + ``provider`` +
``show_action_trace`` to keep this surface narrow. Operators wanting to
edit other yaml fields go through ``/admin/agents/{name}`` in
routes_admin_a (Monaco editor).

Path-traversal defence mirrors ``routes_admin_a/studio/agents.py``: the
``name`` segment must be a bare stem (no ``/``, ``\\``, or ``..``).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

import yaml  # type: ignore[import-untyped]
from corlinman_agent.agents import AgentCardRegistry
from corlinman_agent.agents.registry import AgentCardLoadError
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    get_admin_state,
    require_admin,
)

# ---------------------------------------------------------------------------
# Wire models
# ---------------------------------------------------------------------------


class AgentBindingOut(BaseModel):
    """One row in ``GET /admin/agents/bindings``."""

    name: str
    description: str
    model: str | None = None
    provider: str | None = None
    show_action_trace: bool = True


class AgentBindingsResponse(BaseModel):
    agents: list[AgentBindingOut]


class AgentBindingPatch(BaseModel):
    """Body for ``PATCH /admin/agents/{name}/binding``.

    Both fields are mandatory in the body schema but their *value* may
    be ``None`` — sending ``{"model": null, "provider": null}`` clears
    the binding, restoring legacy "request-body-driven" routing. We
    intentionally don't allow partial-omit-then-keep semantics: PATCH
    is a *full* binding swap. The two fields are independent slots, not
    a transaction, but the request body carries both so the UI can be
    explicit about what it wants the file to look like after the write.
    """

    model: str | None = None
    provider: str | None = None
    show_action_trace: bool | None = None


class StatusOk(BaseModel):
    status: str = "ok"
    name: str
    model: str | None = None
    provider: str | None = None
    show_action_trace: bool = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _agents_dir_for(state: AdminState) -> Path:
    """Resolve the ``agents/`` directory under the state's data dir.

    Mirrors the routes_admin_a/studio/agents.py helper but lives here so the
    two modules don't share a private symbol. Falls back to ``cwd()``
    when ``data_dir`` is unset (matches the routes_admin_a behaviour
    so deployments without ``AdminState.data_dir`` still surface a
    consistent shape).
    """
    base = state.data_dir if state.data_dir is not None else Path.cwd()
    return Path(base) / "agents"


def _validate_agent_name(name: str) -> None:
    """Reject empty names, path separators, or any ``..`` segment.

    Path-traversal defence identical to ``routes_admin_a/studio/agents.py``.
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


def _resolve_yaml_path(agents_dir: Path, name: str) -> Path:
    """Find the on-disk yaml for ``name`` — accepts ``.yaml`` or ``.yml``.

    Returns the existing path. Raises 404 if neither suffix variant
    exists; the dispatcher / registry treats both equally so we must
    not silently prefer one when the file is on disk under the other
    extension.
    """
    _validate_agent_name(name)
    for suffix in (".yaml", ".yml"):
        candidate = agents_dir / f"{name}{suffix}"
        if candidate.is_file():
            return candidate
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "not_found", "resource": "agent", "id": name},
    )


def _load_yaml_doc(path: Path) -> dict[str, Any]:
    """Parse a yaml file into a top-level mapping or raise 422.

    Empty / non-mapping documents are a configuration error — the
    binding endpoint operates on key/value structure, not a freeform
    yaml stream.
    """
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "yaml_parse_error", "message": str(exc)},
        ) from exc
    if raw is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": "yaml_empty", "message": "agent yaml is empty"},
        )
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "yaml_not_mapping",
                "message": "top-level yaml must be a mapping",
            },
        )
    return raw


def _effective_show_action_trace(doc: dict[str, Any]) -> bool:
    """Return the effective action-trace flag for a raw yaml document.

    The card loader uses the same default: absent means visible. We
    validate here as well so PATCH cannot return a shape the loader
    would later reject.
    """
    value = doc.get("show_action_trace")
    if value is None:
        return True
    if not isinstance(value, bool):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "error": "yaml_invalid_field",
                "field": "show_action_trace",
                "message": "show_action_trace must be a boolean",
            },
        )
    return value


def _apply_binding(
    doc: dict[str, Any],
    model: str | None,
    provider: str | None,
    show_action_trace: bool | None,
) -> dict[str, Any]:
    """Return a new dict with model/provider/action-trace fields updated.

    Field-order preservation rules:

    * If the field already exists in ``doc`` we update it in place
      (same position).
    * If it's being added for the first time, we insert it directly
      after ``description`` (the closest established neighbour); this
      keeps freshly-written files looking like the sample
      ``agents/researcher.yaml`` we ship with.
    * If a model/provider value is ``None`` we drop the key entirely — empty
      strings are unidiomatic for "no binding" and would round-trip as
      ``model: ''`` in yaml.
    * ``show_action_trace`` defaults to ``True``. ``False`` is written
      explicitly; ``True`` removes the key and falls back to the default.

    All other keys are passed through untouched — we explicitly do not
    canonicalise / re-sort / re-flow them.
    """
    # Dicts preserve insertion order under 3.7+, so walking the old doc
    # and building a new one gives us deterministic round-trip.
    out: dict[str, Any] = {}
    pending: list[tuple[str, str | bool | None]] = []
    # We delay inserting newly-added fields until just after
    # ``description`` so they land in a predictable spot.
    if "model" not in doc and model is not None:
        pending.append(("model", model))
    if "provider" not in doc and provider is not None:
        pending.append(("provider", provider))
    if (
        "show_action_trace" not in doc
        and show_action_trace is not None
        and show_action_trace is False
    ):
        pending.append(("show_action_trace", False))

    for key, value in doc.items():
        if key == "model":
            if model is None:
                # Drop the key (skip the write).
                continue
            out[key] = model
            continue
        if key == "provider":
            if provider is None:
                continue
            out[key] = provider
            continue
        if key == "show_action_trace":
            if show_action_trace is None:
                out[key] = value
                continue
            if show_action_trace is True:
                continue
            out[key] = False
            continue
        out[key] = value
        if key == "description" and pending:
            for pk, pv in pending:
                if pv is not None:
                    out[pk] = pv
            pending = []

    # If ``description`` was missing the file simply gets the new
    # fields appended at the end — better than silently dropping them.
    for pk, pv in pending:
        if pv is not None:
            out[pk] = pv

    return out


def _atomic_write_yaml(path: Path, doc: dict[str, Any]) -> None:
    """Write ``doc`` to ``path`` via ``tmpfile + os.replace``.

    ``sort_keys=False`` preserves the order we built in
    :func:`_apply_binding`. ``default_flow_style=False`` keeps the
    block-style output that the existing yaml files use. Multiline
    scalars (e.g. ``system_prompt: |``) are preserved by PyYAML's
    round-trip with the ``allow_unicode=True`` flag.
    """
    serialised = yaml.safe_dump(
        doc,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    tmp = path.with_name(path.name + ".new")
    try:
        tmp.write_text(serialised, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        # Best-effort cleanup of the staged tmpfile.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"error": "write_failed", "message": str(exc)},
        ) from exc


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    """Sub-router for ``/admin/agents/bindings*``.

    Wired into :func:`routes_admin_b.build_router` alongside the rest of
    the admin-B bundle. Auth is enforced via :func:`require_admin`,
    matching the pattern used by ``routes_admin_b/credentials.py``.
    """
    r = APIRouter(
        dependencies=[Depends(require_admin)], tags=["admin", "agents"]
    )

    @r.get(
        "/admin/agent-bindings",
        response_model=AgentBindingsResponse,
        summary="List parsed per-agent model+provider bindings",
    )
    async def list_bindings(
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> AgentBindingsResponse:
        agents_dir = _agents_dir_for(state)
        try:
            registry = AgentCardRegistry.load_from_dir(agents_dir)
        except AgentCardLoadError as exc:
            # One bad file in the dir shouldn't 500 the whole listing.
            # Surface as 422 with the path so the operator can fix the
            # offender. Mirrors how the agent loader itself fails
            # loudly rather than silently skipping.
            return JSONResponse(  # type: ignore[return-value]
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={
                    "error": "agent_load_failed",
                    "path": str(exc.path),
                    "reason": exc.reason,
                },
            )
        rows: list[AgentBindingOut] = []
        for name in registry.names():
            card = registry.get(name)
            if card is None:
                continue
            rows.append(
                AgentBindingOut(
                    name=card.name,
                    description=card.description,
                    model=card.model,
                    provider=card.provider,
                    show_action_trace=card.show_action_trace,
                )
            )
        return AgentBindingsResponse(agents=rows)

    @r.patch(
        "/admin/agent-bindings/{name}",
        response_model=StatusOk,
        summary="Update an agent's model+provider binding",
    )
    async def patch_binding(
        body: AgentBindingPatch,
        state: Annotated[AdminState, Depends(get_admin_state)],
        name: str,
    ) -> StatusOk:
        agents_dir = _agents_dir_for(state)
        path = _resolve_yaml_path(agents_dir, name)

        async with state.admin_write_lock:
            doc = _load_yaml_doc(path)
            # Coerce empty strings to None so a UI that submits ""
            # behaves the same as "clear this slot". Yaml round-trips
            # ``foo: ""`` as the literal empty string which the card
            # parser would later reject ("must be a string" passes but
            # the dispatcher would happily ship an empty model id).
            new_model = body.model if body.model else None
            new_provider = body.provider if body.provider else None
            new_doc = _apply_binding(
                doc,
                new_model,
                new_provider,
                body.show_action_trace,
            )
            effective_show_action_trace = _effective_show_action_trace(new_doc)
            _atomic_write_yaml(path, new_doc)

        return StatusOk(
            name=name,
            model=new_model,
            provider=new_provider,
            show_action_trace=effective_show_action_trace,
        )

    return r


__all__ = [
    "AgentBindingOut",
    "AgentBindingPatch",
    "AgentBindingsResponse",
    "StatusOk",
    "router",
]
