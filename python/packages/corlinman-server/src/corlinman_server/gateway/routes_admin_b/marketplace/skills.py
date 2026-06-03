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
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from fastapi import Path as PathParam
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    get_admin_state,
    require_admin,
)

__all__ = ["SkillInstallTaskStore", "router"]


# SSE keepalive cadence — matches the sibling routes (subagents.py,
# system.py) so reverse proxies idle on the same timer everywhere.
_SKILL_INSTALL_SSE_HEARTBEAT_SECONDS: float = 10.0


# Terminal phases for the install task — once observed the SSE stream
# closes and the task row is left intact for poll-after-the-fact reads.
_TERMINAL_PHASES: frozenset[str] = frozenset({"installed", "failed"})


# Conservative slug shape for hub installs. ClawHub publishes
# ``[a-z0-9-]+`` slugs; we reject anything else with a typed 400 rather
# than passing through to httpx where it'd 404 (or worse, traverse).
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}[a-z0-9]$")


# Module-level singleton for the pin POST body's default. FastAPI's
# Body(default_factory=...) trips B008 when used inline as a default
# argument; hoisting the call out of the signature avoids the warning
# without changing the wire shape.
_PIN_BODY_DEFAULT = Body(default_factory=dict)


# Module-level set holding background install task references so they
# don't get garbage-collected mid-flight. asyncio doesn't keep strong
# references to tasks created by ``create_task``; without this the
# task could be cancelled while still running. The task removes itself
# on completion via :func:`Task.add_done_callback`.
_INSTALL_BG_TASKS: set[asyncio.Task[Any]] = set()


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class InstalledSkillOut(BaseModel):
    """One row in ``GET /admin/skills``.

    Mirrors the curator's :class:`SkillSummaryOut` plus an ``origin``
    field that distinguishes hub-sourced skills from bundled / user
    edits. The curator surface already exposes a coarse three-bucket
    ``origin`` (``bundled`` / ``user-requested`` / ``agent-created``)
    on the underlying :class:`Skill`; we layer the hub provenance on
    top by reading the sidecar ``.openclaw-meta.json`` written by the
    installer.
    """

    name: str
    description: str
    version: str
    state: str
    origin: str
    pinned: bool
    use_count: int = 0
    last_used_at: str | None = None
    created_at: str | None = None
    # Editor-facing fields — populated when the registry factory is wired
    # (the disk-only fallback can't parse the SKILL.md body cheaply, so it
    # leaves these at their empty defaults). These mirror the writable keys
    # on :class:`SkillUpdateBody` so the UI's edit drawer can round-trip a
    # row through ``PUT /admin/skills/{name}`` without a second fetch.
    body_markdown: str = ""
    when_to_use: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    disable_model_invocation: bool = False


class SkillsListResponse(BaseModel):
    profile: str
    rows: list[InstalledSkillOut] = Field(default_factory=list)


class SkillUpdateBody(BaseModel):
    """Body for ``PUT /admin/skills/{name}``.

    Every field is optional so the editor can submit a partial patch — only
    the keys present in the payload are written back. These five fields are
    runtime-consumed (``registry.py`` parses them off the SKILL.md
    frontmatter / body and the context assembler honours
    ``disable_model_invocation`` + ``allowed_tools`` + ``when_to_use``), so a
    write here changes how the model selects + injects the skill on its next
    turn. ``body_markdown`` is the prose the assembler injects verbatim.
    """

    description: str | None = None
    body_markdown: str | None = None
    disable_model_invocation: bool | None = None
    allowed_tools: list[str] | None = None
    when_to_use: str | None = None


class HubSkillRowOut(BaseModel):
    """Compact shape returned by hub search / featured.

    Maps :class:`HubSkillSummary` 1:1 — keyed loosely so the upstream
    DTO can grow new optional fields without forcing a wire bump.
    """

    slug: str
    name: str
    description: str = ""
    version: str = ""
    author: str | None = None
    stars: int = 0
    downloads: int = 0
    updated_at: str | None = None
    tags: list[str] = Field(default_factory=list)


class HubListResponse(BaseModel):
    """Envelope for hub list/search.

    ``offline`` flips to ``True`` when the upstream call surfaces a
    :class:`HubUnavailableError`; the UI uses it to render the banner +
    Retry button. Even in the offline case we keep HTTP 200 (so the
    fetch promise resolves), with an explicit ``error`` machine code.

    The pagination cursor field is named ``next_cursor`` to match the
    W1.4 wire contract; ``cursor`` is the *input* (passed as a query
    string), and the response surfaces the *next* page handle.
    """

    rows: list[HubSkillRowOut] = Field(default_factory=list)
    next_cursor: str | None = None
    offline: bool = False
    error: str | None = None


class HubSkillDetailOut(BaseModel):
    """Full skill detail returned by ``GET /admin/skills/hub/skills/{slug}``.

    The upstream DTO carries a richer shape (versions list, README,
    security scan summary, etc.) — we project it loosely so the route
    file doesn't have to track every upstream addition.
    """

    slug: str
    name: str
    description: str = ""
    version: str = ""
    versions: list[str] = Field(default_factory=list)
    readme: str | None = None
    author: str | None = None
    stars: int = 0
    downloads: int = 0
    updated_at: str | None = None
    tags: list[str] = Field(default_factory=list)
    license: str | None = None
    homepage: str | None = None


class HubInstallBody(BaseModel):
    """Body for ``POST /admin/skills/hub/install``."""

    slug: str
    version: str = "latest"
    profile: str = "default"
    force: bool = False


class HubInstallAcceptedOut(BaseModel):
    """202-style acknowledgement returned from
    ``POST /admin/skills/hub/install``."""

    request_id: str
    slug: str
    version: str
    profile: str
    state: str = "queued"


class HubInstallStatusOut(BaseModel):
    """Polled status for ``GET /admin/skills/hub/install/{request_id}``."""

    request_id: str
    slug: str
    version: str
    profile: str
    state: str
    phase: str
    started_at: int | None = None
    finished_at: int | None = None
    name: str | None = None
    error: str | None = None
    message: str | None = None


# ---------------------------------------------------------------------------
# In-process install task store
# ---------------------------------------------------------------------------


@dataclass
class _SkillInstallTask:
    """One row in :class:`SkillInstallTaskStore`.

    Mirrors :class:`~corlinman_server.system.subagent.SubagentStatus`
    loosely so the SSE shape stays consistent across W1.3 surfaces.
    """

    request_id: str
    slug: str
    version: str
    profile: str
    state: str = "queued"
    phase: str = "queued"
    started_at: int | None = None
    finished_at: int | None = None
    name: str | None = None
    error: str | None = None
    message: str | None = None
    # `asyncio.Event` set every time `state`/`phase` changes — the SSE
    # generator awaits this to push frames without busy-polling. Excluded
    # from serialisation; rebuilt on every set.
    _changed: asyncio.Event = field(default_factory=asyncio.Event)

    def to_status(self) -> HubInstallStatusOut:
        return HubInstallStatusOut(
            request_id=self.request_id,
            slug=self.slug,
            version=self.version,
            profile=self.profile,
            state=self.state,
            phase=self.phase,
            started_at=self.started_at,
            finished_at=self.finished_at,
            name=self.name,
            error=self.error,
            message=self.message,
        )

    def is_terminal(self) -> bool:
        return self.phase in _TERMINAL_PHASES


class SkillInstallTaskStore:
    """Tiny in-memory store for hub-install background tasks.

    Sized for the v1.5 hub install flow — one entry per install request,
    no persistence across gateway restarts (an interrupted install is
    safe to re-issue because the installer rejects partially-extracted
    targets unless ``force=True``). Kept small + private to this module
    on purpose: lifting it into a sibling package would require designing
    a durable persistence path, which the plan defers.

    Async-locked so concurrent updates from the background task and the
    SSE reader stay consistent.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._rows: dict[str, _SkillInstallTask] = {}

    async def create(
        self,
        *,
        slug: str,
        version: str,
        profile: str,
    ) -> _SkillInstallTask:
        request_id = uuid.uuid4().hex
        async with self._lock:
            row = _SkillInstallTask(
                request_id=request_id,
                slug=slug,
                version=version,
                profile=profile,
            )
            self._rows[request_id] = row
            return row

    async def get(self, request_id: str) -> _SkillInstallTask | None:
        async with self._lock:
            return self._rows.get(request_id)

    async def update(
        self,
        request_id: str,
        **fields: Any,
    ) -> _SkillInstallTask | None:
        async with self._lock:
            row = self._rows.get(request_id)
            if row is None:
                return None
            for key, value in fields.items():
                setattr(row, key, value)
            # Wake any SSE generators waiting on this row.
            row._changed.set()
            row._changed = asyncio.Event()
            return row


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _error(
    status_code: int,
    error: str,
    message: str,
    **extra: Any,
) -> JSONResponse:
    body: dict[str, Any] = {"error": error, "message": message}
    body.update(extra)
    return JSONResponse(status_code=status_code, content=body)


def _resolve_hub_client(state: AdminState) -> Any | None:
    """Read the :class:`ClawHubClient` handle off AdminState.

    Duck-typed so tests can swap in a fake exposing ``search`` /
    ``list_skills`` / ``get_skill``. Tries two attribute names — the
    lifecycle wires ``clawhub_client``, but the W1-TESTS fixture uses
    ``skill_hub_client``; we accept either. Returning ``None`` makes
    the proxy handlers degrade to the offline envelope rather than
    503 — matches the resolved decision in ``docs/PLAN_SKILL_HUB.md``
    (banner + Retry, not 503).
    """
    for attr in ("clawhub_client", "skill_hub_client"):
        client = getattr(state, attr, None)
        if client is None:
            continue
        if hasattr(client, "search") or hasattr(client, "list_skills"):
            return client
    # Also check ``state.extras`` as a final fallback (some bootstraps
    # park optional handles there rather than growing the dataclass).
    extras = getattr(state, "extras", None)
    if isinstance(extras, dict):
        client = extras.get("skill_hub_client") or extras.get(
            "clawhub_client"
        )
        if client is not None and (
            hasattr(client, "search") or hasattr(client, "list_skills")
        ):
            return client
    return None


# Process-global fallback store. When the lifecycle hasn't wired one
# onto AdminState (a degraded boot, or the test path that runs the
# router without the entrypoint), the install route lazily mints a
# single shared store so the request_id round trip still works. The
# store is per-process; install state doesn't survive a restart, which
# matches the contract spelled out in :class:`SkillInstallTaskStore`.
_FALLBACK_INSTALL_STORE: SkillInstallTaskStore | None = None


def _resolve_install_store(state: AdminState) -> SkillInstallTaskStore:
    store = getattr(state, "skill_install_store", None)
    if isinstance(store, SkillInstallTaskStore):
        return store
    if store is not None and hasattr(store, "create") and hasattr(store, "get"):
        return store  # type: ignore[no-any-return]
    global _FALLBACK_INSTALL_STORE
    if _FALLBACK_INSTALL_STORE is None:
        _FALLBACK_INSTALL_STORE = SkillInstallTaskStore()
    return _FALLBACK_INSTALL_STORE


def _resolve_data_dir(state: AdminState) -> Path | None:
    raw = getattr(state, "data_dir", None)
    if raw is None:
        return None
    return Path(raw)


def _resolve_profile_skills_dir(state: AdminState, slug: str) -> Path | None:
    """Resolve ``<data_dir>/profiles/<slug>/skills`` without dragging
    the ``corlinman_server.profiles`` import to module load time."""
    data_dir = _resolve_data_dir(state)
    if data_dir is None:
        return None
    try:
        from corlinman_server.profiles import profile_skills_dir
    except ImportError:
        # Fall back to the documented layout so tests that don't bundle
        # the profiles package can still exercise the install handlers
        # by setting ``state.data_dir`` directly.
        return data_dir / "profiles" / slug / "skills"
    return profile_skills_dir(data_dir, slug)


def _bundled_skill_filenames() -> frozenset[str]:
    """Return the set of ``*.md`` filenames in ``bundled_skills/``.

    Cached after first call — the bundle ships with the wheel and is
    immutable across the process lifetime, so a single fs walk is fine.
    A degraded boot (no bundle directory) collapses to an empty set,
    which simply means *no* skill renders with ``origin == "bundled"``.
    """
    global _CACHED_BUNDLED
    if _CACHED_BUNDLED is not None:
        return _CACHED_BUNDLED
    try:
        from corlinman_server.gateway.lifecycle.starter_skills import (
            bundled_skills_root,
        )

        root = bundled_skills_root()
    except Exception:
        root = None
    if root is None or not root.is_dir():
        _CACHED_BUNDLED = frozenset()
        return _CACHED_BUNDLED
    names = {p.name for p in root.glob("*.md") if p.is_file()}
    _CACHED_BUNDLED = frozenset(names)
    return _CACHED_BUNDLED


_CACHED_BUNDLED: frozenset[str] | None = None


def _load_hub_meta(skills_dir: Path, skill_name: str) -> dict[str, Any] | None:
    """Read the ``.openclaw-meta.json`` sidecar for one installed skill.

    The installer writes this next to each hub-sourced skill (either at
    ``<skills_dir>/<name>.openclaw-meta.json`` for single-file skills or
    inside the skill dir for directory bundles). We try both shapes and
    return ``None`` on any read error so a malformed sidecar doesn't
    break the listing.
    """
    candidates = [
        skills_dir / f"{skill_name}.openclaw-meta.json",
        skills_dir / skill_name / ".openclaw-meta.json",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            data: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if isinstance(data, dict):
            return data
        return None
    return None


def _derive_origin(
    skill: Any,
    skills_dir: Path,
    bundled_names: frozenset[str],
) -> str:
    """Resolve the UI-facing origin tag for one installed skill.

    Precedence:

    1. ``.openclaw-meta.json`` sidecar — surface as ``hub:<slug>@<ver>``.
    2. Filename matches a bundled skill AND no sidecar — ``bundled``.
    3. Everything else — ``user``.

    The third bucket folds the curator's ``user-requested`` and
    ``agent-created`` together on purpose: the v1.5 UI only needs three
    badge colours and the operator distinguishes operator-edited vs
    agent-spawned via the curator page, not the skill library.
    """
    meta = _load_hub_meta(skills_dir, skill.name)
    if meta is not None:
        slug = str(meta.get("slug") or skill.name)
        version = str(meta.get("version") or "")
        if version:
            return f"hub:{slug}@{version}"
        return f"hub:{slug}"

    # Determine on-disk filename for bundled match. SKILL_md files under
    # ``bundled_skills/`` are named ``<skill>.md``; per-profile skills
    # are loaded by name too.
    source = getattr(skill, "source_path", None)
    if source is not None:
        try:
            disk_name = Path(source).name
        except TypeError:
            disk_name = f"{skill.name}.md"
    else:
        disk_name = f"{skill.name}.md"
    if disk_name in bundled_names:
        return "bundled"
    return "user"


def _iso(dt: Any) -> str | None:
    """Best-effort ISO render — same fallback path the curator uses."""
    if dt is None:
        return None
    try:
        rendered = dt.isoformat()
    except AttributeError:
        return str(dt)
    return str(rendered)


def _derive_origin_from_disk(
    *,
    disk_path: Path,
    skills_dir: Path,
    bundled: frozenset[str],
) -> str:
    """Disk-only origin derivation for the bare filesystem mode.

    ``disk_path`` is the SKILL.md / flat .md the row points to.

    Precedence:

    1. A ``.openclaw-meta.json`` sidecar inside the same directory →
       ``hub:<slug>@<ver>``.
    2. Flat ``<name>.md`` at the top of skills_dir AND filename matches
       a bundled skill → ``bundled``.
    3. Everything else → ``user``.
    """
    parent = disk_path.parent
    # Hub-installed skills always live in their own subdirectory with a
    # sidecar at ``<dir>/.openclaw-meta.json``.
    sidecar = parent / ".openclaw-meta.json"
    if sidecar.is_file():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            slug = str(data.get("slug") or parent.name)
            version = str(data.get("version") or "")
            if version:
                return f"hub:{slug}@{version}"
            return f"hub:{slug}"
        return "user"

    # Bundled skills land as flat ``<name>.md`` at the top of skills_dir
    # (the seeder copies them straight in). A directory-with-SKILL.md
    # is never bundled — it'd have come from the installer or the
    # operator's editor.
    if disk_path.parent == skills_dir and disk_path.name in bundled:
        return "bundled"
    return "user"


def _build_row(
    *,
    name: str,
    disk_path: Path,
    skills_dir: Path,
    bundled: frozenset[str],
    registry: Any | None,
) -> InstalledSkillOut:
    """Build one ``InstalledSkillOut`` from a disk entry, enriching with
    registry metadata when available.

    Kept module-level (rather than a closure inside :func:`router`) so
    the helper is unit-testable and the route body stays scannable.
    """
    origin = _derive_origin_from_disk(
        disk_path=disk_path, skills_dir=skills_dir, bundled=bundled
    )

    description = ""
    version = "1.0.0"
    state_str = "active"
    pinned = False
    created_at: str | None = None
    use_count = 0
    last_used_at: str | None = None
    body_markdown = ""
    when_to_use: str | None = None
    allowed_tools: list[str] = []
    disable_model_invocation = False

    if registry is not None:
        skill = registry.get(name) if hasattr(registry, "get") else None
        if skill is not None:
            description = str(getattr(skill, "description", ""))
            version = str(getattr(skill, "version", version))
            state_str = str(getattr(skill, "state", state_str))
            pinned = bool(getattr(skill, "pinned", pinned))
            created_at = _iso(getattr(skill, "created_at", None))
            body_markdown = str(getattr(skill, "body_markdown", "") or "")
            wtu = getattr(skill, "when_to_use", None)
            when_to_use = str(wtu) if wtu else None
            allowed_tools = [
                str(t) for t in (getattr(skill, "allowed_tools", None) or [])
            ]
            disable_model_invocation = bool(
                getattr(skill, "disable_model_invocation", False)
            )
            usage = _registry_usage(registry, name)
            if usage is not None:
                use_count = int(getattr(usage, "use_count", 0) or 0)
                last_used_at = _iso(getattr(usage, "last_used_at", None))

    return InstalledSkillOut(
        name=name,
        description=description,
        version=version,
        state=state_str,
        origin=origin,
        pinned=pinned,
        use_count=use_count,
        last_used_at=last_used_at,
        created_at=created_at,
        body_markdown=body_markdown,
        when_to_use=when_to_use,
        allowed_tools=allowed_tools,
        disable_model_invocation=disable_model_invocation,
    )


def _skill_to_out(skill: Any, *, origin: str, usage: Any | None) -> InstalledSkillOut:
    """Project a registry ``Skill`` onto the wire envelope.

    Shared by :func:`pin_skill` / :func:`update_skill` / the registry pass in
    :func:`list_skills` so the editor-facing fields (body / when_to_use /
    allowed_tools / disable_model_invocation) stay in lockstep with
    :func:`_build_row` without re-typing the ``getattr`` ladder four times.
    """
    wtu = getattr(skill, "when_to_use", None)
    return InstalledSkillOut(
        name=str(skill.name),
        description=str(getattr(skill, "description", "")),
        version=str(getattr(skill, "version", "1.0.0")),
        state=str(getattr(skill, "state", "active")),
        origin=origin,
        pinned=bool(getattr(skill, "pinned", False)),
        use_count=int(usage.use_count if usage else 0),
        last_used_at=_iso(usage.last_used_at if usage else None),
        created_at=_iso(getattr(skill, "created_at", None)),
        body_markdown=str(getattr(skill, "body_markdown", "") or ""),
        when_to_use=str(wtu) if wtu else None,
        allowed_tools=[
            str(t) for t in (getattr(skill, "allowed_tools", None) or [])
        ],
        disable_model_invocation=bool(
            getattr(skill, "disable_model_invocation", False)
        ),
    )


def _registry_usage(registry: Any, skill_name: str) -> Any | None:
    fn = getattr(registry, "usage_for", None)
    if fn is None:
        return None
    try:
        return fn(skill_name)
    except Exception:
        return None


def _load_registry(state: AdminState, slug: str) -> Any:
    """Resolve the skill registry for ``slug``. Mirrors the curator's
    helper so behaviour stays consistent across both surfaces."""
    factory = getattr(state, "skill_registry_factory", None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "skill_registry_factory_missing"},
        )
    try:
        return factory(slug)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "error": "registry_load_failed",
                "slug": slug,
                "message": str(exc),
            },
        ) from exc


def _validate_slug(slug: str) -> None:
    if not _SLUG_RE.match(slug):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_slug",
                "message": "slug must match [a-z0-9._-]",
                "slug": slug,
            },
        )


def _summary_to_row(summary: Any) -> HubSkillRowOut:
    """Project a :class:`HubSkillSummary` onto the wire envelope.

    Tolerates either an attribute-shaped dataclass or a plain dict —
    tests + the real client are free to choose either.
    """
    getter = (
        summary.get  # type: ignore[union-attr]
        if isinstance(summary, dict)
        else lambda k, d=None: getattr(summary, k, d)
    )
    tags = getter("tags") or []
    return HubSkillRowOut(
        slug=str(getter("slug") or ""),
        name=str(getter("name") or getter("slug") or ""),
        description=str(getter("description") or ""),
        version=str(getter("version") or ""),
        author=getter("author"),
        stars=int(getter("stars") or 0),
        downloads=int(getter("downloads") or 0),
        updated_at=_iso(getter("updated_at")),
        tags=[str(t) for t in tags],
    )


def _detail_to_out(detail: Any) -> HubSkillDetailOut:
    getter = (
        detail.get  # type: ignore[union-attr]
        if isinstance(detail, dict)
        else lambda k, d=None: getattr(detail, k, d)
    )
    versions = getter("versions") or []
    tags = getter("tags") or []
    return HubSkillDetailOut(
        slug=str(getter("slug") or ""),
        name=str(getter("name") or getter("slug") or ""),
        description=str(getter("description") or ""),
        version=str(getter("version") or ""),
        versions=[str(v) for v in versions],
        readme=getter("readme"),
        author=getter("author"),
        stars=int(getter("stars") or 0),
        downloads=int(getter("downloads") or 0),
        updated_at=_iso(getter("updated_at")),
        tags=[str(t) for t in tags],
        license=getter("license"),
        homepage=getter("homepage"),
    )


# ---------------------------------------------------------------------------
# Background install runner
# ---------------------------------------------------------------------------


async def _run_install_task(
    *,
    store: SkillInstallTaskStore,
    request_id: str,
    profile_skills_dir: Path,
    client: Any,
    slug: str,
    version: str,
    force: bool,
    audit_log: Any | None,
) -> None:
    """Drive one install: download → extract → done.

    Updates the task row at every phase boundary so the SSE stream
    emits frames without polling. All exceptions are caught + recorded
    on the row — a failed install must never bubble out of the
    background task (FastAPI would log it but the user wouldn't see
    the error).
    """
    try:
        from corlinman_server.system.skill_hub import (
            SkillAlreadyInstalledError,
            SkillInstallError,
            UnsafeTarballError,
            install_skill,
        )
    except ImportError as exc:
        await store.update(
            request_id,
            state="failed",
            phase="failed",
            error="installer_missing",
            message=str(exc),
            finished_at=int(time.time() * 1000),
        )
        return

    await store.update(
        request_id,
        state="running",
        phase="download.started",
        started_at=int(time.time() * 1000),
    )

    try:
        report = await install_skill(
            profile_skills_dir=profile_skills_dir,
            client=client,
            slug=slug,
            version=version,
            force=force,
            audit_log=audit_log,
        )
    except SkillAlreadyInstalledError as exc:
        await store.update(
            request_id,
            state="failed",
            phase="failed",
            error="already_installed",
            message=str(exc),
            finished_at=int(time.time() * 1000),
        )
        return
    except UnsafeTarballError as exc:
        await store.update(
            request_id,
            state="failed",
            phase="failed",
            error="unsafe_tarball",
            message=str(exc),
            finished_at=int(time.time() * 1000),
        )
        return
    except SkillInstallError as exc:
        await store.update(
            request_id,
            state="failed",
            phase="failed",
            error="install_failed",
            message=str(exc),
            finished_at=int(time.time() * 1000),
        )
        return
    except Exception as exc:
        await store.update(
            request_id,
            state="failed",
            phase="failed",
            error="unexpected",
            message=str(exc),
            finished_at=int(time.time() * 1000),
        )
        return

    # Drive an explicit ``extract.started`` notch even though the real
    # install ran serially above — the SSE consumer expects to see the
    # extract phase before the terminal frame so the UI can render a
    # two-stage progress bar without coordinating with the installer.
    await store.update(
        request_id,
        phase="extract.started",
    )

    # The W1.4 wire contract uses ``state == "installed"`` on success
    # (rather than the generic ``succeeded`` used for subagent runs) so
    # the UI can render the install-specific terminal message without
    # an extra phase round-trip. ``phase`` mirrors so SSE consumers
    # keying off either field both close the stream.
    installed_name = (
        getattr(report, "name", None)
        or getattr(report, "slug", None)
        or slug
    )
    await store.update(
        request_id,
        state="installed",
        phase="installed",
        name=str(installed_name),
        finished_at=int(time.time() * 1000),
        message=f"installed {installed_name}",
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


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


def _error_offline(exc: Exception) -> HubListResponse:
    """Map a client-side fetch exception onto the offline envelope.

    Centralised so the search/featured handlers share the same machine
    code surface.
    """
    err = "hub_unreachable"
    msg = str(exc).lower()
    # HubRateLimitedError carries a hint — surface a distinct code so
    # the UI can render "ClawHub rate-limited, try again in N seconds"
    # rather than a generic banner.
    if "rate" in msg and "limit" in msg:
        err = "hub_rate_limited"
    return HubListResponse(rows=[], offline=True, error=err)
