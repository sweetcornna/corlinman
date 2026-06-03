"""Wire models + helpers for ``/admin/curator*`` (extracted from
:mod:`corlinman_server.gateway.routes_admin_b.infra.curator`).

Behaviour-preserving split: this module holds every module-level
pydantic wire shape, helper function, constant, and the shared
``_run_curator_now`` body that the curator route factory uses. The route
file re-imports these names so ``router()`` and its handlers are
unchanged.

Kept free of any import of the ``curator`` route module to avoid an
import cycle.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_b.state import AdminState

if TYPE_CHECKING:
    from corlinman_evolution_store import SignalsRepo

# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class SkillCountsOut(BaseModel):
    """Per-profile skill state histogram returned alongside the curator
    state. ``total`` is the sum across the three lifecycle buckets so the
    UI doesn't have to recompute."""

    active: int = 0
    stale: int = 0
    archived: int = 0
    total: int = 0


class OriginCountsOut(BaseModel):
    """Per-profile origin histogram. Same shape as :class:`SkillCountsOut`
    but bucketed by provenance instead of lifecycle state."""

    bundled: int = 0
    user_requested: int = Field(default=0, alias="user-requested")
    agent_created: int = Field(default=0, alias="agent-created")

    model_config = {"populate_by_name": True}


class ProfileCuratorOut(BaseModel):
    """One row in ``GET /admin/curator/profiles``. Mirrors the
    :class:`corlinman_evolution_store.CuratorState` projection plus the
    skill / origin histograms the UI renders as pills under each profile
    card."""

    slug: str
    paused: bool
    interval_hours: int
    stale_after_days: int
    archive_after_days: int
    last_review_at: str | None = None
    last_review_summary: str | None = None
    run_count: int = 0
    skill_counts: SkillCountsOut = Field(default_factory=SkillCountsOut)
    origin_counts: OriginCountsOut = Field(default_factory=OriginCountsOut)


class CuratorProfilesResponse(BaseModel):
    profiles: list[ProfileCuratorOut]


class TransitionOut(BaseModel):
    """One transition in a :class:`CuratorReport`. Used by both
    preview and real run responses."""

    skill_name: str
    from_state: str
    to_state: str
    reason: str
    days_idle: float


class CuratorReportOut(BaseModel):
    """JSON projection of :class:`gateway.evolution.CuratorReport`.

    Both ``/preview`` and ``/run`` return this exact shape — the only
    difference is whether ``dry_run`` was ``True`` and whether the
    underlying SKILL.md state field was persisted to disk."""

    profile_slug: str
    started_at: str
    finished_at: str
    duration_ms: int
    transitions: list[TransitionOut]
    marked_stale: int
    archived: int
    reactivated: int
    checked: int
    skipped: int


class PauseBody(BaseModel):
    paused: bool


class ThresholdsPatchBody(BaseModel):
    """``PATCH /admin/curator/{slug}/thresholds`` body — every field
    optional so the UI can ship one slider at a time. Validation lives in
    the handler because the cross-field rule (``archive > stale``) doesn't
    fit a single ``Field`` constraint."""

    interval_hours: int | None = Field(default=None, ge=1)
    stale_after_days: int | None = Field(default=None, ge=1)
    archive_after_days: int | None = Field(default=None, ge=1)


class CuratorStateOut(BaseModel):
    """Projection of :class:`CuratorState` used by /pause + /thresholds
    responses. Subset of :class:`ProfileCuratorOut` minus the counts."""

    slug: str
    paused: bool
    interval_hours: int
    stale_after_days: int
    archive_after_days: int
    last_review_at: str | None = None
    last_review_summary: str | None = None
    run_count: int = 0


class SkillSummaryOut(BaseModel):
    """One row in ``GET /admin/curator/{slug}/skills``. Compact shape
    that carries everything the badge-driven list view needs without a
    second fetch."""

    name: str
    description: str
    version: str
    state: str
    origin: str
    pinned: bool
    use_count: int = 0
    last_used_at: str | None = None
    created_at: str | None = None


class SkillsListResponse(BaseModel):
    skills: list[SkillSummaryOut]


class PinBody(BaseModel):
    pinned: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(dt: datetime | None) -> str | None:
    """Render a ``datetime`` as ISO-8601 UTC. ``None`` passes through.

    Mirrors the convention the rest of the admin surface uses (the
    profiles route, evolution history, etc): timezone-aware UTC with a
    ``Z`` suffix, no microseconds. Naive datetimes are assumed UTC.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    # Drop subsecond precision for stable readable strings.
    return dt.astimezone(UTC).isoformat()


def _profile_store(state: AdminState):
    """Return the wired profile store or raise 503. Mirrors the same
    helper in routes_admin_a/studio/profiles.py — kept private here so the
    routes can fail fast with a single readable envelope."""
    store = getattr(state, "profile_store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "profile_store_missing"},
        )
    return store


def _curator_repo(state: AdminState):
    repo = getattr(state, "curator_state_repo", None)
    if repo is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "curator_state_repo_missing"},
        )
    return repo


def _registry_factory(state: AdminState):
    fn = getattr(state, "skill_registry_factory", None)
    if fn is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "skill_registry_factory_missing"},
        )
    return fn


def _ensure_profile(store, slug: str) -> None:
    """Look up ``slug`` on ``store``; raise 404 if missing. Accepts any
    object that exposes a ``.get(slug)`` returning ``None`` for absent
    rows (matches :class:`ProfileStore` and the in-test fakes)."""
    if store.get(slug) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "profile_not_found", "slug": slug},
        )


def _load_registry(state: AdminState, slug: str):
    """Resolve the skill registry for ``slug`` using the factory. Wraps
    any exception thrown by the factory into a 500 ``registry_load_failed``
    envelope so a malformed skills dir doesn't bubble as a raw 500."""
    factory = _registry_factory(state)
    try:
        return factory(slug)
    except Exception as exc:  # noqa: BLE001 — typed envelope below
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "registry_load_failed",
                "slug": slug,
                "message": str(exc),
            },
        ) from exc


def _report_to_out(report: Any) -> CuratorReportOut:
    """Project a :class:`gateway.evolution.CuratorReport` onto the wire
    envelope. Pulls fields via ``getattr`` so a future struct change that
    adds optional fields doesn't break this projection."""
    transitions = [
        TransitionOut(
            skill_name=t.skill_name,
            from_state=t.from_state,
            to_state=t.to_state,
            reason=t.reason,
            days_idle=float(t.days_idle),
        )
        for t in getattr(report, "transitions", []) or []
    ]
    return CuratorReportOut(
        profile_slug=str(getattr(report, "profile_slug", "")),
        started_at=_iso(getattr(report, "started_at", None)) or "",
        finished_at=_iso(getattr(report, "finished_at", None)) or "",
        duration_ms=int(getattr(report, "duration_ms", 0)),
        transitions=transitions,
        marked_stale=int(getattr(report, "marked_stale", 0)),
        archived=int(getattr(report, "archived", 0)),
        reactivated=int(getattr(report, "reactivated", 0)),
        checked=int(getattr(report, "checked", 0)),
        skipped=int(getattr(report, "skipped", 0)),
    )


def _state_to_out(state_row: Any) -> CuratorStateOut:
    """Slim projection of :class:`CuratorState` for /pause + /thresholds
    responses."""
    return CuratorStateOut(
        slug=str(state_row.profile_slug),
        paused=bool(state_row.paused),
        interval_hours=int(state_row.interval_hours),
        stale_after_days=int(state_row.stale_after_days),
        archive_after_days=int(state_row.archive_after_days),
        last_review_at=_iso(state_row.last_review_at),
        last_review_summary=state_row.last_review_summary,
        run_count=int(state_row.run_count),
    )


def _count_skills(registry: Any) -> tuple[SkillCountsOut, OriginCountsOut]:
    """Walk a :class:`SkillRegistry` once and return both the lifecycle
    state histogram and the origin histogram. Done in one pass so the
    /profiles route never iterates the registry twice."""
    states = SkillCountsOut()
    origins = OriginCountsOut()
    for skill in registry:
        s = getattr(skill, "state", "active")
        if s == "active":
            states.active += 1
        elif s == "stale":
            states.stale += 1
        elif s == "archived":
            states.archived += 1
        states.total += 1
        o = getattr(skill, "origin", "user-requested")
        if o == "bundled":
            origins.bundled += 1
        elif o == "user-requested":
            origins.user_requested += 1
        elif o == "agent-created":
            origins.agent_created += 1
    return states, origins


def _all_profile_slugs(store) -> list[str]:
    """Return every profile slug the store knows about, sorted."""
    profiles = store.list()
    return sorted(str(p.slug) for p in profiles)


# DDL-default curator thresholds, mirrored from
# ``corlinman_evolution_store.schema`` so the /profiles route can fill in
# the histogram row for a profile that has no persisted curator_state yet
# WITHOUT firing a per-slug SELECT (PERF-03). These match the column
# DEFAULTs and the private ``_default_curator_state`` in the store repo.
_CURATOR_DEFAULT_INTERVAL_HOURS = 168
_CURATOR_DEFAULT_STALE_AFTER_DAYS = 30
_CURATOR_DEFAULT_ARCHIVE_AFTER_DAYS = 90


def _default_curator_state(slug: str) -> Any | None:
    """Build the default :class:`CuratorState` for a profile with no
    persisted row. Returns ``None`` if the evolution-store package isn't
    importable, so the caller can fall back to ``curator_repo.get``.

    Kept here (not on the repo) so the bulk-list path stays a single query:
    ``list_all`` returns only persisted rows, and unreviewed profiles get
    this synthetic default instead of an extra round trip each."""
    try:
        from corlinman_evolution_store import (  # noqa: PLC0415
            CuratorState,
        )
    except ImportError:
        return None
    return CuratorState(
        profile_slug=slug,
        last_review_at=None,
        last_review_duration_ms=None,
        last_review_summary=None,
        run_count=0,
        paused=False,
        interval_hours=_CURATOR_DEFAULT_INTERVAL_HOURS,
        stale_after_days=_CURATOR_DEFAULT_STALE_AFTER_DAYS,
        archive_after_days=_CURATOR_DEFAULT_ARCHIVE_AFTER_DAYS,
    )


async def _run_curator_now(
    *,
    state: AdminState,
    slug: str,
    dry_run: bool,
) -> CuratorReportOut:
    """Shared body for /preview + /run. ``dry_run`` selects the mode.

    Imports :func:`maybe_run_curator` lazily so this module stays
    importable when the curator package isn't installed (the parent agent
    runs evolution-store + skills-registry as separate package boundaries
    — a partial install must still expose a typed 503).
    """
    store = _profile_store(state)
    _ensure_profile(store, slug)
    curator_repo = _curator_repo(state)
    signals_repo = getattr(state, "signals_repo", None)
    if signals_repo is None:
        # The pure logic still works with a no-op signals sink. Build a
        # tiny in-process stub so we don't have to thread an optional
        # parameter through the engine's helper.
        signals_repo = _NoopSignalsRepo()

    try:
        from corlinman_server.gateway.evolution import (  # noqa: PLC0415
            maybe_run_curator,
        )
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "curator_module_missing",
                "message": str(exc),
            },
        ) from exc

    registry = _load_registry(state, slug)
    try:
        report = await maybe_run_curator(
            profile_slug=slug,
            registry=registry,
            curator_repo=curator_repo,
            # Best-effort handle resolved via getattr; the noop stub
            # duck-types the only method the curator calls (``insert``).
            signals_repo=cast("SignalsRepo", signals_repo),
            force=True,  # the UI invocation always forces a pass
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001 — typed envelope below
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "curator_run_failed",
                "slug": slug,
                "message": str(exc),
            },
        ) from exc

    if report is None:
        # With ``force=True`` and ``paused=False`` we always get a
        # report; the only ``None`` branch is ``paused=True``. The UI
        # surfaces this as a separate state so the operator knows the
        # action was a no-op rather than a silent failure.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "curator_paused", "slug": slug},
        )

    return _report_to_out(report)


class _NoopSignalsRepo:
    """Drop-in stand-in for :class:`corlinman_evolution_store.SignalsRepo`
    used when the gateway hasn't wired the real repo yet. The curator
    only calls ``insert(signal)``; everything else returns sensibly.

    Kept inside this module so the routes_admin_b package doesn't grow a
    public stub class — this is purely a route-level convenience."""

    async def insert(self, signal: Any) -> int:  # noqa: ARG002
        return 0


def _registry_usage(registry: Any, skill_name: str):
    """Pull the :class:`SkillUsage` sidecar for one skill. Tolerates
    registries that don't expose :meth:`usage_for` (the in-test fake) by
    returning ``None`` — the caller already guards against ``None``."""
    fn = getattr(registry, "usage_for", None)
    if fn is None:
        return None
    try:
        return fn(skill_name)
    except Exception:  # noqa: BLE001 — sidecar reads must not raise
        return None
