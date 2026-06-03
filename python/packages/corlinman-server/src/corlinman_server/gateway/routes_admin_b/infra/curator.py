"""``/admin/curator*`` — Wave 4.6 curator UI surface.

End-to-end backend for the new evolution / curator page in the admin UI.
Lets operators:

* preview the curator's deterministic lifecycle pass (dry-run)
* run the pass for real (persists transitions to SKILL.md + curator_state)
* pause / resume the per-profile curator loop
* tune the three thresholds (interval / stale / archive)
* list skills with state + origin + pin badges, filterable
* pin / unpin individual skills

All routes mount behind :func:`require_admin` and gate on three handles
on :class:`AdminState`:

* :attr:`AdminState.profile_store` — confirms the profile exists; 404
  ``profile_not_found`` otherwise.
* :attr:`AdminState.curator_state_repo` — the async
  :class:`corlinman_evolution_store.CuratorStateRepo`. Missing → 503
  ``curator_state_repo_missing``.
* :attr:`AdminState.skill_registry_factory` — synchronous
  ``(slug) -> SkillRegistry`` callable so each request loads a fresh
  view of the profile's skills. Missing → 503
  ``skill_registry_factory_missing``.

The ``signals_repo`` handle is best-effort: when wired, run/preview emit
the same ``EVENT_*`` rows the scheduler-driven curator does; when not
wired, the routes still succeed and just skip signal emission.

Mirrors the Rust pattern from ``routes_admin_b/infra/evolution.py`` — typed
pydantic v2 request/response shapes, error envelopes via ``HTTPException``
detail dicts, deferred imports of the optional store packages so a
partially-installed gateway still imports this module.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

from corlinman_server.gateway.routes_admin_b.infra._curator_lib import (
    CuratorProfilesResponse,
    CuratorReportOut,
    CuratorStateOut,
    OriginCountsOut,
    PauseBody,
    PinBody,
    ProfileCuratorOut,
    SkillCountsOut,
    SkillsListResponse,
    SkillSummaryOut,
    ThresholdsPatchBody,
    _all_profile_slugs,
    _count_skills,
    _curator_repo,
    _default_curator_state,
    _ensure_profile,
    _iso,
    _load_registry,
    _profile_store,
    _registry_usage,
    _run_curator_now,
    _state_to_out,
)
from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    get_admin_state,
    require_admin,
)

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:  # noqa: C901 — single APIRouter factory, mirrors siblings
    """Sub-router for ``/admin/curator*``. Mounted by
    :func:`corlinman_server.gateway.routes_admin_b.build_router`."""
    r = APIRouter(
        dependencies=[Depends(require_admin)], tags=["admin", "curator"]
    )

    @r.get("/admin/curator/profiles", response_model=CuratorProfilesResponse)
    async def list_profiles(
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> CuratorProfilesResponse:
        """One row per profile — curator state + skill/origin histograms.

        Walks the ProfileStore for the slug list, then for each slug
        fetches the CuratorState + loads the registry to count skills.
        The histogram pass is O(n) per profile so even a few hundred
        skills stays cheap; the UI polls this every few seconds so the
        cost matters.
        """
        store = _profile_store(admin_state)
        curator_repo = _curator_repo(admin_state)

        slugs = _all_profile_slugs(store)
        # PERF-03: fetch every profile's curator_state in ONE query instead
        # of an ``await curator_repo.get(slug)`` per slug. ``list_all`` only
        # returns slugs that have a persisted row; profiles that have never
        # been reviewed are missing from the map and get the same DDL-default
        # struct ``CuratorStateRepo.get`` would synthesise — built locally so
        # an unreviewed profile costs zero extra SELECTs.
        list_all = getattr(curator_repo, "list_all", None)
        if callable(list_all):
            state_rows = await list_all()
            state_by_slug = {row.profile_slug: row for row in state_rows}
        else:
            # Older/fake repos without ``list_all`` degrade to per-slug
            # fetches rather than 500.
            state_by_slug = {}
        rows: list[ProfileCuratorOut] = []
        for slug in slugs:
            state_row = state_by_slug.get(slug)
            if state_row is None:
                # No persisted row yet — synthesise the default in-process
                # (matches ``CuratorStateRepo.get``'s never-None contract)
                # rather than firing a per-slug SELECT. Falls back to
                # ``curator_repo.get`` only if the default struct can't be
                # built (e.g. evolution-store not importable).
                state_row = _default_curator_state(slug)
                if state_row is None:
                    state_row = await curator_repo.get(slug)
            # Registry load is best-effort — if a profile has no skills
            # dir yet we still want to surface its curator state.
            try:
                registry = _load_registry(admin_state, slug)
                skill_counts, origin_counts = _count_skills(registry)
            except HTTPException:
                # Re-raise — the factory missing is a hard 503.
                raise
            except Exception:  # noqa: BLE001 — surface empty counts
                skill_counts = SkillCountsOut()
                origin_counts = OriginCountsOut()
            rows.append(
                ProfileCuratorOut(
                    slug=slug,
                    paused=bool(state_row.paused),
                    interval_hours=int(state_row.interval_hours),
                    stale_after_days=int(state_row.stale_after_days),
                    archive_after_days=int(state_row.archive_after_days),
                    last_review_at=_iso(state_row.last_review_at),
                    last_review_summary=state_row.last_review_summary,
                    run_count=int(state_row.run_count),
                    skill_counts=skill_counts,
                    origin_counts=origin_counts,
                )
            )
        return CuratorProfilesResponse(profiles=rows)

    @r.post(
        "/admin/curator/{slug}/preview",
        response_model=CuratorReportOut,
    )
    async def preview_curator_run(
        slug: str,
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> CuratorReportOut:
        """Dry-run: returns the would-be transitions without writing back
        to disk or bumping ``curator_state.last_review_at``. Force-runs
        regardless of the interval window so the UI's "Preview" button
        always returns a meaningful payload."""
        return await _run_curator_now(
            state=admin_state, slug=slug, dry_run=True
        )

    @r.post(
        "/admin/curator/{slug}/run",
        response_model=CuratorReportOut,
    )
    async def run_curator_now(
        slug: str,
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> CuratorReportOut:
        """Real run: persists each transition to SKILL.md, emits signals,
        and bumps ``curator_state.last_review_at`` so the next scheduled
        pass starts from this run. Same envelope as /preview."""
        return await _run_curator_now(
            state=admin_state, slug=slug, dry_run=False
        )

    @r.post(
        "/admin/curator/{slug}/pause",
        response_model=CuratorStateOut,
    )
    async def pause_curator(
        slug: str,
        body: PauseBody,
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> CuratorStateOut:
        """Flip the per-profile pause flag. The flag short-circuits
        :func:`maybe_run_curator` *before* any signal emission, so a
        paused profile costs zero work even on the scheduler tick.

        Returns the post-update :class:`CuratorState` so the UI doesn't
        have to refetch."""
        store = _profile_store(admin_state)
        _ensure_profile(store, slug)
        curator_repo = _curator_repo(admin_state)

        try:
            from corlinman_evolution_store import (  # noqa: PLC0415
                CuratorState,
            )
        except ImportError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "evolution_store_missing",
                    "message": str(exc),
                },
            ) from exc

        existing = await curator_repo.get(slug)
        updated = CuratorState(
            profile_slug=existing.profile_slug,
            last_review_at=existing.last_review_at,
            last_review_duration_ms=existing.last_review_duration_ms,
            last_review_summary=existing.last_review_summary,
            run_count=existing.run_count,
            paused=bool(body.paused),
            interval_hours=existing.interval_hours,
            stale_after_days=existing.stale_after_days,
            archive_after_days=existing.archive_after_days,
            tenant_id=existing.tenant_id,
        )
        await curator_repo.upsert(updated)
        return _state_to_out(updated)

    @r.patch(
        "/admin/curator/{slug}/thresholds",
        response_model=CuratorStateOut,
    )
    async def update_thresholds(
        slug: str,
        body: ThresholdsPatchBody,
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> CuratorStateOut:
        """Update any subset of the three thresholds. Validation:

        * ``interval_hours`` ≥ 1 (handled by pydantic ``Field(ge=1)``)
        * ``stale_after_days`` ≥ 1
        * ``archive_after_days`` > the effective stale threshold

        The cross-field rule uses the *effective* values (incoming
        override stacked on top of the existing row), so a PATCH that
        only changes ``archive_after_days`` is still checked against the
        currently-persisted ``stale_after_days``."""
        store = _profile_store(admin_state)
        _ensure_profile(store, slug)
        curator_repo = _curator_repo(admin_state)

        try:
            from corlinman_evolution_store import (  # noqa: PLC0415
                CuratorState,
            )
        except ImportError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error": "evolution_store_missing",
                    "message": str(exc),
                },
            ) from exc

        existing = await curator_repo.get(slug)
        next_interval = (
            body.interval_hours
            if body.interval_hours is not None
            else existing.interval_hours
        )
        next_stale = (
            body.stale_after_days
            if body.stale_after_days is not None
            else existing.stale_after_days
        )
        next_archive = (
            body.archive_after_days
            if body.archive_after_days is not None
            else existing.archive_after_days
        )

        if next_archive <= next_stale:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "invalid_thresholds",
                    "message": (
                        "archive_after_days must be greater than "
                        "stale_after_days"
                    ),
                    "stale_after_days": int(next_stale),
                    "archive_after_days": int(next_archive),
                },
            )

        updated = CuratorState(
            profile_slug=existing.profile_slug,
            last_review_at=existing.last_review_at,
            last_review_duration_ms=existing.last_review_duration_ms,
            last_review_summary=existing.last_review_summary,
            run_count=existing.run_count,
            paused=existing.paused,
            interval_hours=int(next_interval),
            stale_after_days=int(next_stale),
            archive_after_days=int(next_archive),
            tenant_id=existing.tenant_id,
        )
        await curator_repo.upsert(updated)
        return _state_to_out(updated)

    @r.get(
        "/admin/curator/{slug}/skills",
        response_model=SkillsListResponse,
    )
    async def list_profile_skills(
        slug: str,
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
        state_filter: Annotated[
            Literal["active", "stale", "archived"] | None,
            Query(alias="state"),
        ] = None,
        origin_filter: Annotated[
            Literal["bundled", "user-requested", "agent-created"] | None,
            Query(alias="origin"),
        ] = None,
        search: Annotated[str | None, Query()] = None,
    ) -> SkillsListResponse:
        """List every skill for ``slug`` with the badge metadata the UI
        needs in one round trip.

        Filters compose: state and origin are exact-match; ``search`` is
        a case-insensitive substring on ``name`` + ``description`` so an
        operator can type "code" and surface every skill with that
        substring across either field."""
        store = _profile_store(admin_state)
        _ensure_profile(store, slug)
        registry = _load_registry(admin_state, slug)

        needle = (search or "").strip().lower()
        rows: list[SkillSummaryOut] = []
        for skill in registry:
            if state_filter is not None and skill.state != state_filter:
                continue
            if origin_filter is not None and skill.origin != origin_filter:
                continue
            if needle:
                hay = f"{skill.name}\n{skill.description}".lower()
                if needle not in hay:
                    continue
            usage = _registry_usage(registry, skill.name)
            rows.append(
                SkillSummaryOut(
                    name=str(skill.name),
                    description=str(skill.description),
                    version=str(getattr(skill, "version", "1.0.0")),
                    state=str(skill.state),
                    origin=str(skill.origin),
                    pinned=bool(skill.pinned),
                    use_count=int(usage.use_count if usage else 0),
                    last_used_at=_iso(
                        usage.last_used_at if usage else None
                    ),
                    created_at=_iso(getattr(skill, "created_at", None)),
                )
            )
        rows.sort(key=lambda row: row.name)
        return SkillsListResponse(skills=rows)

    @r.post(
        "/admin/curator/{slug}/skills/{name}/pin",
        response_model=SkillSummaryOut,
    )
    async def pin_skill(
        slug: str,
        name: str,
        body: PinBody,
        admin_state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> SkillSummaryOut:
        """Toggle :attr:`Skill.pinned` for one skill in ``slug``'s
        registry. Round-trips the new value back to SKILL.md so the
        next registry load picks it up — without this writeback the pin
        would silently revert on every gateway restart.

        Returns the post-update summary (matches /skills row shape)."""
        store = _profile_store(admin_state)
        _ensure_profile(store, slug)
        registry = _load_registry(admin_state, slug)

        skill = registry.get(name)
        if skill is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "error": "skill_not_found",
                    "slug": slug,
                    "skill": name,
                },
            )

        skill.pinned = bool(body.pinned)
        try:
            from corlinman_skills_registry import write_skill_md  # noqa: PLC0415
            from corlinman_skills_registry.parse import (  # noqa: PLC0415
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

        # Preserve the markdown body verbatim — re-read it off disk so we
        # don't accidentally drop edits made by a sibling writer between
        # registry load and this write. Same approach the curator's
        # transition writeback uses.
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
                    "slug": slug,
                    "skill": name,
                    "message": str(exc),
                },
            ) from exc

        usage = _registry_usage(registry, skill.name)
        return SkillSummaryOut(
            name=str(skill.name),
            description=str(skill.description),
            version=str(getattr(skill, "version", "1.0.0")),
            state=str(skill.state),
            origin=str(skill.origin),
            pinned=bool(skill.pinned),
            use_count=int(usage.use_count if usage else 0),
            last_used_at=_iso(usage.last_used_at if usage else None),
            created_at=_iso(getattr(skill, "created_at", None)),
        )

    return r
