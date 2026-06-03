"""``/admin/skills*`` — Skill library + ClawHub browse/install surface.

W1.3 of ``docs/PLAN_SKILL_HUB.md``. Wires the admin UI's Skill library to:

* the existing per-profile :class:`SkillRegistry` (read of installed skills)
* the new :class:`ClawHubClient` (search / featured / detail browse)
* the new :func:`install_skill` / :func:`uninstall_skill` helpers
  (untar a downloaded bundle into ``<data_dir>/profiles/<slug>/skills/<name>/``;
  rm-rf the dir on uninstall, gated on origin so bundled skills can't be
  wiped from the UI).

Two surfaces:

* **Installed tab** — ``GET /admin/skills`` + ``POST .../pin`` +
  ``DELETE .../{name}``. Wraps the curator pin handler + the registry
  walk the curator already exposes at ``/admin/curator/{slug}/skills``,
  but flattens the wire to the active profile (default = ``"default"``)
  and adds an ``origin`` derivation that's hub-aware.

* **Browse Hub tab** — ``GET .../hub/{search,featured,skills/{slug}}``
  proxies to :class:`ClawHubClient`. On :class:`HubUnavailableError`
  the proxy collapses to a typed *offline envelope*
  (``{rows: [], offline: true, error: "hub_unreachable"}`` HTTP 200) —
  the UI's banner handles it; we never bubble 503 so the page itself
  still renders.

  Installs run as an asyncio background task with progress observable
  via two routes:

  * ``POST .../hub/install`` mints a ``request_id`` and registers a row
    in a small in-process :class:`_SkillInstallTaskStore`. The actual
    download + untar runs as :func:`asyncio.create_task` so the HTTP
    response returns immediately.
  * ``GET .../hub/install/{request_id}`` polls the row.
  * ``GET .../hub/install/{request_id}/events/live`` SSE-streams the row's
    state transitions — ``download.started`` → ``extract.started`` →
    ``installed`` / ``failed``.

Auth: every route mounts behind :func:`require_admin` via the
``dependencies=[Depends(require_admin)]`` router-level guard the sibling
``subagents.py`` uses. The handlers are dependency-inject-friendly —
the :class:`ClawHubClient` + installer functions are resolved off
:class:`AdminState` so tests swap in fakes without monkey-patching
modules.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse, StreamingResponse

from corlinman_server.gateway.routes_admin_b.marketplace._skills_lib import (
    _INSTALL_BG_TASKS,
    _PIN_BODY_DEFAULT,
    _SKILL_INSTALL_SSE_HEARTBEAT_SECONDS,
    HubInstallAcceptedOut,
    HubInstallBody,
    HubInstallStatusOut,
    HubListResponse,
    HubSkillDetailOut,
    InstalledSkillOut,
    SkillInstallTaskStore,
    SkillsListResponse,
    SkillUpdateBody,
    _build_row,
    _bundled_skill_filenames,
    _derive_origin,
    _derive_origin_from_disk,
    _detail_to_out,
    _error,
    _error_offline,
    _load_registry,
    _registry_usage,
    _resolve_hub_client,
    _resolve_install_store,
    _resolve_profile_skills_dir,
    _run_install_task,
    _skill_to_out,
    _summary_to_row,
    _validate_slug,
)
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    get_admin_state,
    require_admin,
)

__all__ = ["SkillInstallTaskStore", "router"]


def router() -> APIRouter:
    r = APIRouter(
        dependencies=[Depends(require_admin)], tags=["admin", "skills"]
    )

    # ------------------------------------------------------------------
    # GET /admin/skills
    # ------------------------------------------------------------------

    @r.get("/admin/skills", response_model=SkillsListResponse)
    async def list_skills(
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
        profile: Annotated[str, Query()] = "default",
    ) -> SkillsListResponse:
        """List the active profile's skills with origin badges.

        Scans ``<data_dir>/profiles/<slug>/skills/`` directly so the
        endpoint works even without a wired ``skill_registry_factory``
        (which the v1.5 contract intentionally keeps optional — the
        Skill library page must not 503 just because the curator's
        registry plumbing isn't installed). When the factory *is*
        wired, we cross-reference the registry for richer metadata
        (description, version, pin state, usage); when it isn't, we
        fall back to bare filesystem-derived rows.

        Each row carries an ``origin`` badge:

        * ``bundled`` — flat ``<name>.md`` matching a file in the
          in-wheel ``corlinman_server.bundled_skills`` directory and
          no sidecar.
        * ``hub:<slug>@<ver>`` — directory with ``.openclaw-meta.json``
          (the installer's sidecar).
        * ``user`` — anything else (operator-authored skills).
        """
        skills_dir = _resolve_profile_skills_dir(admin_state, profile)
        bundled = _bundled_skill_filenames()

        # Registry is best-effort — when the factory isn't wired we
        # still want to render rows by walking the directory.
        registry: Any | None = None
        try:
            factory = getattr(admin_state, "skill_registry_factory", None)
            if factory is not None:
                registry = factory(profile)
        except Exception:
            registry = None

        rows: list[InstalledSkillOut] = []
        seen: set[str] = set()

        # 1. Disk pass — every ``*.md`` at the top of skills_dir and
        # every subdirectory with a ``SKILL.md`` inside is a candidate.
        if skills_dir is not None and skills_dir.is_dir():
            for entry in sorted(skills_dir.iterdir()):
                if entry.name.startswith("."):
                    continue
                if entry.is_file() and entry.suffix == ".md":
                    name = entry.stem
                    rows.append(
                        _build_row(
                            name=name,
                            disk_path=entry,
                            skills_dir=skills_dir,
                            bundled=bundled,
                            registry=registry,
                        )
                    )
                    seen.add(name)
                elif entry.is_dir():
                    skill_md = entry / "SKILL.md"
                    if not skill_md.is_file():
                        continue
                    name = entry.name
                    rows.append(
                        _build_row(
                            name=name,
                            disk_path=skill_md,
                            skills_dir=skills_dir,
                            bundled=bundled,
                            registry=registry,
                        )
                    )
                    seen.add(name)

        # 2. Registry pass — pick up any rows the registry knows about
        # that we didn't see on disk (synthetic/in-memory skills used
        # by the curator's tests). Rare in practice; included for
        # parity with the curator's listing.
        if registry is not None:
            for skill in registry:
                name = str(getattr(skill, "name", ""))
                if not name or name in seen:
                    continue
                origin = (
                    _derive_origin(skill, skills_dir, bundled)
                    if skills_dir is not None
                    else str(getattr(skill, "origin", "user"))
                )
                usage = _registry_usage(registry, name)
                rows.append(
                    _skill_to_out(skill, origin=origin, usage=usage)
                )

        rows.sort(key=lambda row: row.name)
        return SkillsListResponse(profile=profile, rows=rows)

    # ------------------------------------------------------------------
    # POST /admin/skills/{name}/pin
    # ------------------------------------------------------------------

    @r.post(
        "/admin/skills/{name}/pin",
        response_model=InstalledSkillOut,
    )
    async def pin_skill(
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
        name: str = PathParam(..., description="Skill name."),
        body: dict[str, Any] = _PIN_BODY_DEFAULT,
        profile: Annotated[str, Query()] = "default",
    ) -> InstalledSkillOut:
        """Toggle :attr:`Skill.pinned` for one skill in the active profile.

        Thin proxy over the curator pin handler — implemented inline
        rather than via HTTP-internal-call so we keep the auth context
        and don't double-handle errors. Same writeback semantics
        (write SKILL.md back so the pin survives a restart).
        """
        pinned_val = bool(body.get("pinned", True))
        registry = _load_registry(admin_state, profile)
        skill = registry.get(name)
        if skill is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "skill_not_found",
                    "profile": profile,
                    "skill": name,
                },
            )

        skill.pinned = pinned_val
        try:
            from corlinman_skills_registry import (
                write_skill_md,
            )
            from corlinman_skills_registry.parse import (
                split_frontmatter,
            )
        except ImportError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "skills_registry_missing",
                    "message": str(exc),
                },
            ) from exc

        source = skill.source_path
        try:
            raw = source.read_text(encoding="utf-8")
            split = split_frontmatter(raw)
            body_md = split[1] if split is not None else raw
            write_skill_md(source, skill, body_md)
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "error": "skill_write_failed",
                    "profile": profile,
                    "skill": name,
                    "message": str(exc),
                },
            ) from exc

        usage = _registry_usage(registry, skill.name)
        skills_dir = _resolve_profile_skills_dir(admin_state, profile)
        bundled = _bundled_skill_filenames()
        origin = (
            _derive_origin(skill, skills_dir, bundled)
            if skills_dir is not None
            else str(getattr(skill, "origin", "user"))
        )
        return _skill_to_out(skill, origin=origin, usage=usage)

    # ------------------------------------------------------------------
    # PUT /admin/skills/{name}
    # ------------------------------------------------------------------

    @r.put(
        "/admin/skills/{name}",
        response_model=InstalledSkillOut,
    )
    async def update_skill(
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
        body: SkillUpdateBody,
        name: str = PathParam(..., description="Skill name."),
        profile: Annotated[str, Query()] = "default",
    ) -> InstalledSkillOut:
        """Edit one skill's runtime-consumed body + metadata in place.

        Loads the skill off the profile registry, applies the subset of
        fields present in ``body`` (description / body_markdown /
        disable_model_invocation / allowed_tools / when_to_use), then writes
        the SKILL.md back to disk so the edit survives a restart. All five
        fields are read by :mod:`corlinman_skills_registry` on the next load
        and honoured by the context assembler — this is the operator-facing
        edit surface (the curator's autonomous lifecycle is a separate
        path). Same writeback semantics as :func:`pin_skill`.
        """
        registry = _load_registry(admin_state, profile)
        skill = registry.get(name)
        if skill is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "skill_not_found",
                    "profile": profile,
                    "skill": name,
                },
            )

        # Apply only the fields the editor actually sent — a partial patch
        # must not blank an unedited field. ``exclude_unset`` distinguishes
        # "not in payload" from "explicitly set to null/empty".
        patch = body.model_dump(exclude_unset=True)
        new_body: str | None = None
        if "description" in patch and patch["description"] is not None:
            skill.description = str(patch["description"])
        if "disable_model_invocation" in patch:
            skill.disable_model_invocation = bool(
                patch["disable_model_invocation"]
            )
        if "allowed_tools" in patch and patch["allowed_tools"] is not None:
            skill.allowed_tools = [str(t) for t in patch["allowed_tools"]]
        if "when_to_use" in patch:
            wtu = patch["when_to_use"]
            # Empty string clears the hint back to "absent" so the
            # round-tripped frontmatter drops the key entirely.
            skill.when_to_use = (
                str(wtu) if isinstance(wtu, str) and wtu.strip() else None
            )
        if "body_markdown" in patch and patch["body_markdown"] is not None:
            new_body = str(patch["body_markdown"])
            skill.body_markdown = new_body

        try:
            from corlinman_skills_registry import (
                write_skill_md,
            )
        except ImportError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "skills_registry_missing",
                    "message": str(exc),
                },
            ) from exc

        source = skill.source_path
        try:
            # ``write_skill_md`` defaults ``body`` to ``skill.body_markdown``
            # when not passed; we already mutated it above, so the explicit
            # arg is only needed to be unambiguous about the edited body.
            write_skill_md(source, skill, new_body)
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "error": "skill_write_failed",
                    "profile": profile,
                    "skill": name,
                    "message": str(exc),
                },
            ) from exc

        # Best-effort audit row so operator edits show up alongside hub
        # installs + pins on the /admin/system Audit card.
        audit_log = getattr(admin_state, "audit_log", None)
        if audit_log is not None:
            with contextlib.suppress(Exception):
                await audit_log.append(
                    event="skill.edited",
                    details={
                        "name": name,
                        "profile": profile,
                        "fields": sorted(patch.keys()),
                    },
                )

        usage = _registry_usage(registry, skill.name)
        skills_dir = _resolve_profile_skills_dir(admin_state, profile)
        bundled = _bundled_skill_filenames()
        origin = (
            _derive_origin(skill, skills_dir, bundled)
            if skills_dir is not None
            else str(getattr(skill, "origin", "user"))
        )
        return _skill_to_out(skill, origin=origin, usage=usage)

    # ------------------------------------------------------------------
    # DELETE /admin/skills/{name}
    # ------------------------------------------------------------------

    @r.delete("/admin/skills/{name}")
    async def uninstall(
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
        name: str = PathParam(..., description="Skill name."),
        profile: Annotated[str, Query()] = "default",
    ) -> Any:
        """Delete one skill from the profile's skills dir.

        Bundled skills are gated: the bundle ships with the wheel and
        an operator who wants to "remove" one should disable it via
        the curator (or edit the body) — silently rm-rf'ing the file
        on disk would just have first-boot seeding re-create it.
        """
        skills_dir = _resolve_profile_skills_dir(admin_state, profile)
        if skills_dir is None:
            return _error(
                503,
                "data_dir_unset",
                "gateway booted without a data dir; cannot resolve "
                "the profile skills directory",
            )

        # Disk-resolve the skill so we don't require a registry factory
        # (the v1.5 plan keeps that optional). Two layouts are valid:
        # a flat ``<name>.md`` (the bundled path) or a directory with
        # ``SKILL.md`` (hub installs + operator-authored dirs).
        flat_path = skills_dir / f"{name}.md"
        dir_path = skills_dir / name
        disk_path: Path | None = None
        if flat_path.is_file():
            disk_path = flat_path
        elif (dir_path / "SKILL.md").is_file():
            disk_path = dir_path / "SKILL.md"
        if disk_path is None:
            return _error(
                404,
                "skill_not_found",
                f"no skill named {name!r} in profile {profile!r}",
                profile=profile,
                skill=name,
            )

        # Origin check — refuse with 409 ``bundled_protected`` when the
        # skill came from the in-wheel bundle.
        bundled = _bundled_skill_filenames()
        origin = _derive_origin_from_disk(
            disk_path=disk_path, skills_dir=skills_dir, bundled=bundled
        )
        if origin == "bundled":
            return _error(
                409,
                "bundled_protected",
                (
                    f"skill {name!r} ships with corlinman; edit the "
                    f"profile copy instead of uninstalling"
                ),
                profile=profile,
                skill=name,
            )

        # Dispatch to the installer's uninstall helper for hub skills
        # (it writes an audit-log row + double-checks the sidecar). For
        # user skills the helper would refuse on the missing-sidecar
        # check, so we rm-rf directly. Either way we never delete a
        # bundled flat .md — the origin gate above stops us.
        audit_log = getattr(admin_state, "audit_log", None)
        if origin.startswith("hub:"):
            try:
                from corlinman_server.system.skill_hub import (
                    uninstall_skill,
                )
            except ImportError as exc:
                return _error(
                    503,
                    "installer_missing",
                    f"skill_hub installer is not available: {exc}",
                )
            try:
                await uninstall_skill(
                    profile_skills_dir=skills_dir,
                    name=name,
                    audit_log=audit_log,
                )
            except FileNotFoundError:
                return _error(
                    404,
                    "skill_not_found",
                    f"no skill named {name!r} in profile {profile!r}",
                    profile=profile,
                    skill=name,
                )
            except Exception as exc:
                return _error(
                    500,
                    "uninstall_failed",
                    str(exc),
                    profile=profile,
                    skill=name,
                )
        else:
            # User skill — rm-rf the directory (or unlink the flat file).
            try:
                if flat_path.is_file():
                    flat_path.unlink()
                elif dir_path.is_dir():
                    import shutil

                    shutil.rmtree(dir_path)
            except OSError as exc:
                return _error(
                    500,
                    "uninstall_failed",
                    str(exc),
                    profile=profile,
                    skill=name,
                )
            # Best-effort audit row so the operator log shows user
            # deletions alongside hub uninstalls. Suppressed-Exception
            # keeps the path single-failure-tolerant — an audit-log
            # write must never block a successful disk delete.
            if audit_log is not None:
                with contextlib.suppress(Exception):
                    await audit_log.append(
                        event="skill.uninstalled",
                        details={
                            "name": name,
                            "profile": profile,
                            "origin": origin,
                        },
                    )

        # Return 200 with a small confirmation envelope so the UI's
        # success toast can read ``name`` / ``origin`` back without an
        # extra round trip.
        return JSONResponse(
            status_code=200,
            content={
                "ok": True,
                "name": name,
                "profile": profile,
                "origin": origin,
            },
        )

    # ------------------------------------------------------------------
    # GET /admin/skills/hub/search
    # ------------------------------------------------------------------

    @r.get("/admin/skills/hub/search", response_model=HubListResponse)
    async def hub_search(
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
        q: Annotated[str, Query()] = "",
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> HubListResponse:
        """Proxy to :meth:`ClawHubClient.search`.

        Empty / whitespace-only queries collapse to ``list_skills`` with
        the default sort so the UI's "type to search" affordance still
        returns sensible rows when the field is cleared.
        """
        client = _resolve_hub_client(admin_state)
        if client is None:
            return HubListResponse(rows=[], offline=True, error="hub_unreachable")

        needle = (q or "").strip()

        try:
            if needle:
                summaries = await client.search(needle, limit=limit)
                next_cursor: str | None = None
            else:
                tup = await client.list_skills(
                    sort="trending", cursor=None, limit=limit
                )
                # Normalise to a (rows, cursor) shape regardless of whether
                # the client returns a bare list or a (list, cursor) tuple.
                if isinstance(tup, tuple):
                    summaries, next_cursor = (
                        tup[0],
                        tup[1] if len(tup) > 1 else None,
                    )
                else:
                    summaries, next_cursor = tup, None
        except Exception as exc:
            # Treat any client-side failure (HubUnavailableError,
            # HubRateLimitedError, transport errors) as offline so the
            # UI shows the banner + Retry button.
            return _error_offline(exc)

        return HubListResponse(
            rows=[_summary_to_row(s) for s in summaries],
            next_cursor=next_cursor,
        )

    # ------------------------------------------------------------------
    # GET /admin/skills/hub/featured
    # ------------------------------------------------------------------

    @r.get("/admin/skills/hub/featured", response_model=HubListResponse)
    async def hub_featured(
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
        sort: Annotated[
            Literal["trending", "downloads", "stars", "updated", "createdAt"],
            Query(),
        ] = "trending",
        cursor: Annotated[str | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 25,
    ) -> HubListResponse:
        """Proxy to :meth:`ClawHubClient.list_skills`. Same offline-collapse
        pattern as :func:`hub_search`."""
        client = _resolve_hub_client(admin_state)
        if client is None:
            return HubListResponse(rows=[], offline=True, error="hub_unreachable")

        try:
            tup = await client.list_skills(
                sort=sort, cursor=cursor, limit=limit
            )
            if isinstance(tup, tuple):
                summaries, next_cursor = (
                    tup[0],
                    tup[1] if len(tup) > 1 else None,
                )
            else:
                summaries, next_cursor = tup, None
        except Exception as exc:
            return _error_offline(exc)

        return HubListResponse(
            rows=[_summary_to_row(s) for s in summaries],
            next_cursor=next_cursor,
        )

    # ------------------------------------------------------------------
    # GET /admin/skills/hub/skills/{slug}
    # ------------------------------------------------------------------

    @r.get(
        "/admin/skills/hub/skills/{slug}",
        response_model=HubSkillDetailOut,
    )
    async def hub_detail(
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
        slug: str = PathParam(..., description="ClawHub skill slug."),
    ) -> HubSkillDetailOut | JSONResponse:
        _validate_slug(slug)
        client = _resolve_hub_client(admin_state)
        if client is None:
            return _error(
                503,
                "hub_unreachable",
                "the ClawHub client is not wired on this gateway",
            )
        try:
            detail = await client.get_skill(slug)
        except Exception as exc:
            # Distinguish 404 from generic offline so the UI can pick
            # between "skill removed" and "try again later".
            msg = str(exc).lower()
            if "not found" in msg or getattr(exc, "status_code", None) == 404:
                return _error(
                    404,
                    "skill_not_found",
                    f"no hub skill with slug {slug!r}",
                    slug=slug,
                )
            return _error(
                502,
                "hub_unreachable",
                f"hub fetch failed: {exc}",
                slug=slug,
            )
        return _detail_to_out(detail)

    # ------------------------------------------------------------------
    # POST /admin/skills/hub/install
    # ------------------------------------------------------------------

    @r.post(
        "/admin/skills/hub/install",
        response_model=HubInstallAcceptedOut,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def hub_install(
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
        body: HubInstallBody,
    ) -> HubInstallAcceptedOut | JSONResponse:
        _validate_slug(body.slug)
        client = _resolve_hub_client(admin_state)
        if client is None:
            return _error(
                503,
                "hub_unreachable",
                "the ClawHub client is not wired on this gateway",
            )
        store = _resolve_install_store(admin_state)
        skills_dir = _resolve_profile_skills_dir(admin_state, body.profile)
        if skills_dir is None:
            return _error(
                503,
                "data_dir_unset",
                "gateway booted without a data dir; cannot resolve "
                "the profile skills directory",
            )

        row = await store.create(
            slug=body.slug,
            version=body.version,
            profile=body.profile,
        )
        audit_log = getattr(admin_state, "audit_log", None)
        # Fire the background task without awaiting — the response
        # returns immediately with the request_id. The task itself owns
        # all error handling. Hold a strong reference in the module-level
        # set so asyncio doesn't GC the task mid-run; the done callback
        # removes the entry once the install resolves.
        task = asyncio.create_task(
            _run_install_task(
                store=store,
                request_id=row.request_id,
                profile_skills_dir=skills_dir,
                client=client,
                slug=body.slug,
                version=body.version,
                force=body.force,
                audit_log=audit_log,
            )
        )
        _INSTALL_BG_TASKS.add(task)
        task.add_done_callback(_INSTALL_BG_TASKS.discard)
        return HubInstallAcceptedOut(
            request_id=row.request_id,
            slug=row.slug,
            version=row.version,
            profile=row.profile,
            state=row.state,
        )

    # ------------------------------------------------------------------
    # GET /admin/skills/hub/install/{request_id}
    # ------------------------------------------------------------------

    @r.get(
        "/admin/skills/hub/install/{request_id}",
        response_model=HubInstallStatusOut,
    )
    async def hub_install_status(
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
        request_id: str = PathParam(..., description="Install request id."),
    ) -> HubInstallStatusOut | JSONResponse:
        store = _resolve_install_store(admin_state)
        row = await store.get(request_id)
        if row is None:
            return _error(
                404,
                "install_request_not_found",
                f"no install request with id {request_id!r}",
            )
        return row.to_status()

    # ------------------------------------------------------------------
    # GET /admin/skills/hub/install/{request_id}/events/live
    # ------------------------------------------------------------------

    @r.get("/admin/skills/hub/install/{request_id}/events/live")
    async def hub_install_events(
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
        request_id: str = PathParam(..., description="Install request id."),
    ) -> Any:
        """SSE stream of install state transitions.

        Emits one ``event: status`` frame per state/phase change the
        background task records, plus a ``: keepalive`` comment every
        :data:`_SKILL_INSTALL_SSE_HEARTBEAT_SECONDS` so reverse-proxies
        don't idle out. Closes the first time it sees a terminal phase
        (``installed`` / ``failed``).
        """
        store = _resolve_install_store(admin_state)
        row = await store.get(request_id)
        if row is None:
            return _error(
                404,
                "install_request_not_found",
                f"no install request with id {request_id!r}",
            )

        async def _generate() -> AsyncIterator[bytes]:
            seq = 0
            # Emit the initial frame so the EventSource client sees the
            # current state immediately without waiting for a transition.
            payload = json.dumps(row.to_status().model_dump(), default=str)
            yield (
                f"id: {request_id}:{seq}\n"
                f"event: status\n"
                f"data: {payload}\n\n"
            ).encode()
            seq += 1

            if row.is_terminal():
                return

            try:
                while True:
                    current = await store.get(request_id)
                    if current is None:
                        # Row vanished (test cleanup / future GC) — close.
                        break
                    changed = current._changed
                    try:
                        await asyncio.wait_for(
                            changed.wait(),
                            timeout=_SKILL_INSTALL_SSE_HEARTBEAT_SECONDS,
                        )
                    except TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    current = await store.get(request_id)
                    if current is None:
                        break
                    payload = json.dumps(
                        current.to_status().model_dump(), default=str
                    )
                    yield (
                        f"id: {request_id}:{seq}\n"
                        f"event: status\n"
                        f"data: {payload}\n\n"
                    ).encode()
                    seq += 1
                    if current.is_terminal():
                        break
            except asyncio.CancelledError:
                raise

        return StreamingResponse(
            _generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-Event-Id-Format": "request_id:sequence",
            },
        )

    return r
