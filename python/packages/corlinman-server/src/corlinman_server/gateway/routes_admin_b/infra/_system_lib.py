"""Internal helpers extracted from :mod:`...infra.system`.

This module holds the module-level mass (pydantic wire shapes, constants,
the defensive upgrader-exception placeholders, and the helper functions)
that ``system.router()`` and its request handlers depend on. It was split
out of ``system.py`` as a behavior-preserving refactor.

It MUST NOT import the route module (``...infra.system``) — that would
create an import cycle. The route module re-imports the public names from
here instead.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
)
from corlinman_server.system import SystemAuditLog, UpdateChecker, UpdateStatus
from corlinman_server.system.audit import AuditEntry

# Defensive import of the upgrader module — W1.1/W1.2 may not be landed
# when this module is first imported. We bind a sentinel + the
# UpgradeAlreadyRunning exception type (or a placeholder) so the
# handlers can pattern-match on it without conditional imports inside
# each request path.
try:
    from corlinman_server.system.upgrader import (  # type: ignore[import-not-found]
        UpgradeAlreadyRunning,
        UpgraderUnavailable,
    )
except ImportError:  # pragma: no cover — pre-W1.1/W1.2 boot

    class UpgradeAlreadyRunning(Exception):  # type: ignore[no-redef]
        """Placeholder — upgrader module not yet on the import path.

        The real ``UpgraderProtocol.start`` raises the canonical type
        from ``corlinman_server.system.upgrader``. We re-export a
        same-shape sentinel so the route can pattern-match on the
        symbol it imported at module-load time even if W1.1 hasn't
        landed yet; the duck-typed ``except (UpgradeAlreadyRunning,
        ...)`` catch tolerates both.
        """

        def __init__(self, in_flight: Any = None) -> None:
            super().__init__("upgrade already running")
            self.in_flight = in_flight

    class UpgraderUnavailable(Exception):  # type: ignore[no-redef]
        """Placeholder — see :class:`UpgradeAlreadyRunning`."""


# 1 call / minute / process. Multi-instance dedup is out of scope for
# MVP (W3 deferred); a per-process counter is plenty given the UI's only
# refresh button is behind admin auth.
_CHECK_UPDATES_MIN_INTERVAL_SECONDS = 60.0


# ---------------------------------------------------------------------------
# Pydantic wire shapes
# ---------------------------------------------------------------------------


class SystemInfoResponse(BaseModel):
    """JSON shape of :class:`UpdateStatus` — pydantic mirror.

    Field names are deliberately snake_case to match the dataclass
    (admin UI consumes the same names).
    """

    current: str
    latest: str | None = None
    available: bool = False
    release_url: str | None = None
    release_notes_md: str | None = None
    published_at: int | None = None
    last_checked_at: int
    prerelease_seen: list[str] = []
    # Additive (v1.28): recent release history for the rollback picker —
    # ``[{"tag", "published_at", "prerelease"}]``, newest first.
    recent_releases: list[dict[str, Any]] = []


class UpgradeCommandsResponse(BaseModel):
    """Three command strings, one per supported deploy mode."""

    native: str
    docker: str
    docker_with_qq: str


# ---------------------------------------------------------------------------
# W1.3 — one-click upgrade wire shapes
# ---------------------------------------------------------------------------


class UpgradeStartBody(BaseModel):
    """Request body for ``POST /admin/system/upgrade``.

    ``typed_confirmation`` MUST equal ``tag`` exactly — this is the
    user-friction gate (the UI surfaces a text input with the tag as
    placeholder; we enforce the equality server-side too so a scripted
    client can't bypass the typed-confirmation guard).

    ``allow_downgrade`` defaults to ``False``; setting it ``True``
    overrides the ``Version(target) > Version(current)`` check so an
    operator can roll back to a known-good earlier tag.
    """

    tag: str = Field(..., description="Target release tag, e.g. ``v1.2.1``.")
    typed_confirmation: str | None = Field(
        None,
        description=(
            "OPTIONAL since v1.28 (the UI moved to a one-click confirm "
            "dialog, sub2api-style; the audit log records who clicked). "
            "When present it must still equal ``tag`` exactly — kept for "
            "older clients/scripts that relied on the typed gate."
        ),
    )
    allow_downgrade: bool = Field(
        False,
        description=(
            "When ``True``, bypass the no-downgrade guard. Use only for "
            "rollback. Default refuses any tag <= current version."
        ),
    )


class UpgradeStartResponse(BaseModel):
    """``202 Accepted`` payload for ``POST /admin/system/upgrade``."""

    request_id: str
    state: str
    mode: str
    tag: str


class UpgradeStatusResponse(BaseModel):
    """Pydantic mirror of :class:`UpgradeStatus` from the upgrader module.

    Matches the W1.1 dataclass field set (epoch-ms timestamps, phase as
    a non-null short label, log_excerpt as a rolling 4 kB tail). The
    OpenAPI doc reflects this contract.
    """

    request_id: str
    tag: str
    state: str
    phase: str | None = None
    started_at: int | None = None
    finished_at: int | None = None
    log_excerpt: str = ""
    error: str | None = None
    # Additive (v1.28) — restart-window / rollback context. ``target_tag``
    # is the normalized (no leading ``v``) form of ``tag`` so the UI can
    # compare it against ``/health``'s ``version`` verbatim. The three
    # booleans are tri-state: ``None`` = unknown (legacy records).
    target_tag: str | None = None
    before_version: str | None = None
    version_verified: bool | None = None
    rolled_back: bool | None = None


class RollbackVersionEntry(BaseModel):
    """One candidate in the rollback picker."""

    tag: str
    published_at: int | None = None
    prerelease: bool = False
    # ``True`` when this tag matches the kept previous version (docker:
    # the ``corlinman-previous`` container) — restoring it needs no
    # download. Advisory: the helper re-verifies the slot exists.
    instant: bool = False


class RollbackVersionsResponse(BaseModel):
    """``GET /admin/system/rollback-versions`` payload."""

    current: str
    versions: list[RollbackVersionEntry] = []


class RollbackStartBody(BaseModel):
    """``POST /admin/system/rollback`` body.

    ``tag`` omitted/None = roll back to the version the last successful
    upgrade replaced (the instant slot when available).
    """

    tag: str | None = None


class UpgradeCancelResponse(BaseModel):
    """``POST /admin/system/upgrade/{id}/cancel`` payload."""

    request_id: str
    state: str = "cancelled"


class AuditEntryResponse(BaseModel):
    """One row in the audit-tail JSON response."""

    ts: str
    event: str
    request_id: str | None = None
    tag: str | None = None
    actor: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class AuditTailResponse(BaseModel):
    """Paginated audit-log response.

    ``next_before_ts`` is the ``ts`` of the oldest entry in the page —
    callers walk pages by passing it back as ``?before_ts=...``. It is
    ``None`` when the page was short (end of history).
    """

    entries: list[AuditEntryResponse]
    next_before_ts: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _status_to_response(status: UpdateStatus) -> SystemInfoResponse:
    """Coerce the dataclass into the pydantic mirror."""
    return SystemInfoResponse(
        current=status.current,
        latest=status.latest,
        available=status.available,
        release_url=status.release_url,
        release_notes_md=status.release_notes_md,
        published_at=status.published_at,
        last_checked_at=status.last_checked_at,
        prerelease_seen=list(status.prerelease_seen),
        # getattr: tolerate duck-typed checker doubles predating the field.
        recent_releases=[
            dict(entry)
            for entry in (getattr(status, "recent_releases", None) or [])
            if isinstance(entry, dict)
        ],
    )


def _disabled_503() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "error": "update_checker_disabled",
            "message": "update checker is not wired on this gateway",
        },
    )


def _too_many_requests(retry_after_seconds: float) -> JSONResponse:
    """Standard 429 with both header + body retry-after hint."""
    retry_after = max(1, int(retry_after_seconds))
    return JSONResponse(
        status_code=429,
        content={
            "error": "rate_limited",
            "message": "Updates can only be checked once per minute.",
            "retry_after": retry_after,
        },
        headers={"Retry-After": str(retry_after)},
    )


# ---------------------------------------------------------------------------
# W1.3 — upgrade helpers
# ---------------------------------------------------------------------------

# Defensive semver regex for the fallback whitelist branch (when the
# update_checker is unavailable we still won't accept arbitrary tags —
# they must at least *look* like a semver release).
import re as _re

_SEMVER_RE = _re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$")

# SSE keepalive cadence — matches the 10s sessions_events.py uses so
# proxies/reverse-proxies idle out on the same timer everywhere.
_UPGRADE_SSE_HEARTBEAT_SECONDS: float = 10.0

# Terminal upgrade states — the SSE loop exits the moment one of these
# is emitted so the EventSource client knows the stream is done.
_TERMINAL_UPGRADE_STATES: frozenset[str] = frozenset(
    {"succeeded", "failed", "stalled", "cancelled"}
)


def _strip_v(tag: str) -> str:
    """Strip a leading ``v``/``V`` from a release tag. ``v1.2.1`` -> ``1.2.1``."""
    if len(tag) >= 2 and tag[0] in ("v", "V"):
        return tag[1:]
    return tag


def _upgrade_error(
    status_code: int,
    error: str,
    message: str,
    **extra: Any,
) -> JSONResponse:
    """Typed error envelope matching the rest of routes_admin_b."""
    body: dict[str, Any] = {"error": error, "message": message}
    body.update(extra)
    return JSONResponse(status_code=status_code, content=body)


def _resolve_actor(request: Request) -> str:
    """Best-effort extract a username from the auth context on ``request``.

    The auth-shim populates ``request.state.admin_user`` (string) for
    HTTP-Basic flows and ``request.state.admin_session`` (object with
    ``.user`` / ``.username``) for cookie sessions. Falls back to
    ``"admin"`` when neither is set — audit-log readability hinges on a
    non-empty string here.
    """
    user = getattr(request.state, "admin_user", None)
    if isinstance(user, str) and user:
        return user
    session = getattr(request.state, "admin_session", None)
    if session is not None:
        username = getattr(session, "username", None) or getattr(
            session, "user", None
        )
        if isinstance(username, str) and username:
            return username
    return "admin"


def _upgrade_status_to_response(status: Any, *, fallback_tag: str = "") -> UpgradeStatusResponse:
    """Coerce an :class:`UpgradeStatus`-shaped object to the wire mirror.

    The upgrader module's dataclass field set is locked by the W1.1
    contract; we read each attribute via :func:`getattr` so a future
    additive field doesn't crash this serialiser, and so duck-typed
    test doubles can omit optional fields cleanly.
    """
    tag = str(getattr(status, "tag", fallback_tag) or fallback_tag)
    return UpgradeStatusResponse(
        request_id=str(getattr(status, "request_id", "")),
        tag=tag,
        state=str(getattr(status, "state", "unknown")),
        phase=_as_optional_str(getattr(status, "phase", None)),
        started_at=_as_optional_int(getattr(status, "started_at", None)),
        finished_at=_as_optional_int(getattr(status, "finished_at", None)),
        log_excerpt=str(getattr(status, "log_excerpt", "") or ""),
        error=_as_optional_str(getattr(status, "error", None)),
        target_tag=_strip_v(tag) if tag else None,
        before_version=_as_optional_str(
            getattr(status, "before_version", None)
        ),
        version_verified=_as_optional_bool(
            getattr(status, "version_verified", None)
        ),
        rolled_back=_as_optional_bool(getattr(status, "rolled_back", None)),
    )


def _as_optional_bool(raw: Any) -> bool | None:
    return raw if isinstance(raw, bool) else None


def _as_optional_str(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    return str(raw)


def _as_optional_int(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        # bool is a subclass of int — explicitly drop so a stray True
        # doesn't serialise as 1.
        return None
    if isinstance(raw, int):
        return raw
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _resolve_upgrader(state: AdminState) -> Any | None:
    """Best-effort extract the upgrader handle from AdminState."""
    upgrader = getattr(state, "upgrader", None)
    if upgrader is None:
        return None
    # Duck-typed acceptance — the route only ever calls ``is_available``,
    # ``start``, ``status`` and ``progress`` so a doubles-providing test
    # double doesn't need to be the canonical concrete class.
    if hasattr(upgrader, "is_available") or hasattr(upgrader, "start"):
        return upgrader
    return None


def _resolve_audit_log(state: AdminState) -> SystemAuditLog | None:
    """Read the audit-log handle off AdminState (loose-typed)."""
    log = getattr(state, "audit_log", None)
    if isinstance(log, SystemAuditLog):
        return log
    if log is not None and hasattr(log, "append") and hasattr(log, "tail"):
        # Duck-typed acceptance for test doubles.
        return log  # type: ignore[no-any-return,return-value]
    return None


async def _validate_tag_against_releases(
    state: AdminState, target_tag: str
) -> bool:
    """Confirm ``target_tag`` shows up in the GitHub releases the checker
    has observed (latest + cached prereleases).

    Falls back to a semver regex check when the update_checker is not
    wired — looser but still refuses arbitrary garbage that a scripted
    client might post.
    """
    checker = _resolve_checker(state)
    target_stripped = _strip_v(target_tag)
    if checker is None:
        # No checker → loose validation against semver.
        return bool(_SEMVER_RE.match(target_tag))
    try:
        status = await checker.poll(force=False)
    except Exception:  # noqa: BLE001 — best-effort
        return bool(_SEMVER_RE.match(target_tag))
    accepted: set[str] = set()
    latest = getattr(status, "latest", None)
    if isinstance(latest, str) and latest:
        accepted.add(_strip_v(latest))
    for pre in getattr(status, "prerelease_seen", []) or []:
        if isinstance(pre, str):
            accepted.add(_strip_v(pre))
    if target_stripped in accepted:
        return True
    # Last-resort permissive check — operators sometimes pin a release
    # the checker hasn't seen yet (just-published, ETag cached). A
    # well-formed semver string is acceptable; the upgrader script
    # re-validates against GitHub before doing anything destructive.
    return bool(_SEMVER_RE.match(target_tag))


def _is_downgrade(current: str, target_tag: str) -> bool:
    """``True`` iff ``Version(target) <= Version(current)`` per PEP 440."""
    try:
        return Version(_strip_v(target_tag)) <= Version(_strip_v(current))
    except InvalidVersion:
        # Malformed — refuse to make a comparison-based call here; the
        # tag-validation step already gated on shape. Treating it as a
        # downgrade here would surface a confusing error; treating it
        # as a non-downgrade would let it slip past. Choose the safer
        # branch: report a downgrade so the caller surfaces a clear
        # 400 to the operator.
        return True


async def _safe_audit(
    audit_log: SystemAuditLog | None, entry: AuditEntry
) -> None:
    """Append an entry if the log is wired; swallow any error.

    The audit log's writer never raises (best-effort by contract); this
    helper layers on a second safety net so a degraded boot still
    serves the rest of the upgrade flow.
    """
    if audit_log is None:
        return
    try:
        await audit_log.append(entry)
    except Exception:  # noqa: BLE001 — best-effort
        return


def _upgrade_commands(status: UpdateStatus) -> UpgradeCommandsResponse:
    """Format the three install-script invocations.

    When ``status.latest`` is ``None`` (no release ever observed) we
    target ``main`` instead of a tag so the operator can still trigger
    an upgrade against the rolling branch.
    """
    if status.latest:
        version_arg = f"v{status.latest}"
    else:
        version_arg = "main"
    native = f"bash deploy/install.sh --upgrade --version {version_arg}"
    docker = f"bash deploy/install.sh --upgrade --mode docker --version {version_arg}"
    docker_with_qq = (
        f"bash deploy/install.sh --upgrade --mode docker --version {version_arg} --with-qq"
    )
    return UpgradeCommandsResponse(
        native=native, docker=docker, docker_with_qq=docker_with_qq
    )


async def _start_upgrader(
    upgrader: Any,
    tag: str,
    actor: str,
    *,
    allow_downgrade: bool = False,
    action: str = "upgrade",
) -> Any:
    """Call ``upgrader.start`` with the v1.28 kwargs, degrading to the
    legacy positional signature for older impls / test doubles (which
    predate ``allow_downgrade``/``action`` — they also predate rollback,
    so nothing is lost on that path)."""
    try:
        return await upgrader.start(
            tag, actor, allow_downgrade=allow_downgrade, action=action
        )
    except TypeError:
        return await upgrader.start(tag, actor)


async def _rollback_slot(upgrader: Any) -> str | None:
    """``before_version`` of the instant-restore slot, if one plausibly
    exists.

    The slot (docker: the kept ``corlinman-previous`` container) is only
    minted by a SUCCEEDED upgrade and is consumed/cleared by any LATER
    attempt (a failed upgrade auto-restores by renaming the slot back;
    the next upgrade removes it up front). So the slot is only valid
    when the NEWEST terminal record overall is that succeeded upgrade —
    a newer failed/stalled/cancelled record means the slot is gone or
    unknowable, and advertising it would hard-wire an instant swap that
    can only die with ``rollback_slot_missing``. Advisory either way:
    the helper re-verifies at execution time.

    Duck-typed store access (same convention as the status endpoint):
    both concrete upgraders hold the shared ``UpgradeStateStore`` as
    ``self._store``. Returns ``None`` when unavailable.
    """
    store = getattr(upgrader, "_store", None) or getattr(
        upgrader, "store", None
    )
    list_fn = getattr(store, "list_statuses", None)
    if not callable(list_fn):
        return None
    try:
        statuses = await list_fn()
    except Exception:  # noqa: BLE001 — advisory data only
        return None
    newest: Any = None
    for status in statuses:
        state = getattr(status, "state", None)
        if state in ("queued", "running"):
            continue  # in-flight — irrelevant to slot history
        newest_key = (getattr(newest, "finished_at", None) or 0) if newest else -1
        if (getattr(status, "finished_at", None) or 0) >= newest_key:
            newest = status
    if newest is None or getattr(newest, "state", None) != "succeeded":
        return None
    before = getattr(newest, "before_version", None)
    if not isinstance(before, str) or not before:
        return None
    return _strip_v(before)


def _resolve_checker(state: AdminState) -> UpdateChecker | None:
    """Best-effort extractor that keeps the routes mypy-clean.

    We use a dynamic attribute (``Any``) on AdminState; this helper
    narrows the type for the handlers + lets a future test substitute a
    duck-typed double.
    """
    checker = getattr(state, "update_checker", None)
    if checker is None:
        return None
    if isinstance(checker, UpdateChecker):
        return checker
    # Permit duck-typed doubles (e.g. tests passing a stub with the
    # same ``poll(force=...)`` signature). The handlers only call
    # ``.poll(...)`` so an interface check is enough.
    if hasattr(checker, "poll"):
        return checker  # type: ignore[no-any-return,return-value]
    return None
