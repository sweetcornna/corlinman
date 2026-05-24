"""``/admin/sessions*`` — operator-facing replay surface.

Python port of ``rust/crates/corlinman-gateway/src/routes/admin/sessions.rs``.

Routes (all behind :func:`require_admin_dependency`):

* ``GET    /admin/sessions``                       — list of sessions for
  the resolved tenant. **Primary source: the per-turn journal at
  ``<data_dir>/agent_journal.sqlite``** — that's where
  ``agent_servicer.py`` now writes chat history. Falls back to the
  legacy ``<data_dir>/tenants/<tenant>/sessions.sqlite`` (or the flat
  ``<data_dir>/sessions.sqlite``) when the journal is unavailable or
  returns zero rows.
* ``POST   /admin/sessions/{session_key}/replay``  — deterministic
  transcript dump. Body ``{ "mode": "transcript" | "rerun" }``;
  defaults to ``"transcript"`` when omitted. ``"rerun"`` ships in
  v1 with **503 ``rerun_disabled``** because the chat-service wiring
  needed to regenerate the assistant turn lives in the parallel
  ``routes_admin_b`` scope.
* ``DELETE /admin/sessions/{session_key}``         — wipe a session's
  journal trail (turns + cascading turn_messages) so the operator can
  start a session fresh. Also attempts to wipe the session's memory
  store entries when the memory host exposes a per-session purge
  surface (``forget_session``). Does NOT clear the inbox or
  blackboard — those are operational state, not chat history.
* ``DELETE /admin/sessions``                       — nuclear "clear
  all" variant of the above; wipes every session_key in the journal.
  Returns ``{"deleted": <count>}``. Logged at WARN for audit.

Disabled gate: when ``state.sessions_disabled = True`` every route
returns **503 ``sessions_disabled``**.

Tenant resolution mirrors :mod:`api_keys`:

1. ``?tenant=`` query string,
2. ``state.default_tenant``,
3. :func:`corlinman_server.tenancy.default_tenant`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from corlinman_replay import (
    ReplayError,
    ReplayMode,
    SessionListRow,
    SessionNotFoundError,
    SqliteSessionStore,
    StoreLoadError,
    StoreOpenError,
    list_sessions as replay_list_sessions,
    replay as replay_fn,
    replay_from_messages,
    sessions_db_path,
)
from corlinman_replay import TenantId as ReplayTenantId

from corlinman_server.gateway.routes_admin_a._auth_shim import (
    require_admin_dependency,
)
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    get_admin_state,
)
from corlinman_server.tenancy import (
    TenantId,
    TenantIdError,
    default_tenant,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Wire shapes
# ---------------------------------------------------------------------------


class SessionSummaryOut(BaseModel):
    """One row in ``GET /admin/sessions``.

    ``last_user_text`` + ``last_status`` are populated when the row was
    sourced from the per-turn journal (the new primary path); they stay
    ``None`` for rows coming from the legacy ``sessions.sqlite``
    fallback so the UI gracefully renders a placeholder.
    """

    session_key: str
    last_message_at: int  # unix milliseconds
    message_count: int
    last_user_text: str | None = None
    last_status: str | None = None


class SessionsListOut(BaseModel):
    """``GET /admin/sessions`` response."""

    sessions: list[SessionSummaryOut] = Field(default_factory=list)


class DeleteAllOut(BaseModel):
    """``DELETE /admin/sessions`` response."""

    deleted: int = 0


class ReplayBody(BaseModel):
    """``POST /admin/sessions/{session_key}/replay`` body."""

    mode: str | None = None  # "transcript" | "rerun" | None → "transcript"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sessions_disabled() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "sessions_disabled"},
    )


def _session_not_found(session_key: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"error": "not_found", "session_key": session_key},
    )


def _storage_error(exc: BaseException) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={"error": "storage_error", "message": str(exc)},
    )


def _rerun_disabled() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"error": "rerun_disabled"},
    )


def _invalid_tenant_slug(slug: str, reason: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail={
            "error": "invalid_tenant_slug",
            "reason": reason,
            "slug": slug,
        },
    )


def _resolve_tenant(state: AdminState, tenant_q: str | None) -> TenantId:
    """Same precedence chain as :mod:`api_keys._resolve_tenant`."""
    if tenant_q:
        try:
            return TenantId.new(tenant_q)
        except TenantIdError as exc:
            raise _invalid_tenant_slug(tenant_q, str(exc)) from exc
    if state.default_tenant is not None:
        return state.default_tenant
    return default_tenant()


def _resolve_data_dir(state: AdminState) -> Path:
    """Mirror the Rust ``resolve_data_dir``: prefer the state override
    (used by tests pinning a tempdir), fall back to ``CORLINMAN_DATA_DIR``,
    finally ``~/.corlinman``."""
    if state.data_dir is not None:
        return Path(state.data_dir)
    env = os.environ.get("CORLINMAN_DATA_DIR")
    if env:
        return Path(env)
    return Path.home() / ".corlinman"


def _should_use_flat_legacy_sessions(
    state: AdminState, tenant: TenantId
) -> bool:
    """Mirror the Rust ``should_use_flat_legacy_sessions``: when the
    operator hasn't opted into multi-tenant AND the resolved tenant is
    the legacy default, read from the flat ``<data_dir>/sessions.sqlite``
    instead of the per-tenant path."""
    return (not state.tenants_enabled) and tenant.is_legacy_default()


def _to_replay_tenant(tenant: TenantId) -> ReplayTenantId:
    """Convert a server :class:`TenantId` into a replay-package
    :class:`ReplayTenantId`. Both use the same slug regex so the cast is
    safe; we re-validate to keep type checkers happy."""
    return ReplayTenantId.new(tenant.as_str())


# --- flat-legacy fallback ---------------------------------------------------


async def _list_flat_legacy_sessions(data_dir: Path) -> list[SessionListRow]:
    """List sessions out of the legacy single-file
    ``<data_dir>/sessions.sqlite``."""
    path = data_dir / "sessions.sqlite"
    store = await SqliteSessionStore.open(path)
    try:
        rows = await store.list_sessions()
    finally:
        await store.close()
    return [SessionListRow.from_summary(s) for s in rows]


async def _replay_flat_legacy_session(
    data_dir: Path, tenant: ReplayTenantId, session_key: str, mode: ReplayMode
) -> Any:
    """Replay a session out of the legacy single-file
    ``<data_dir>/sessions.sqlite``."""
    path = data_dir / "sessions.sqlite"
    store = await SqliteSessionStore.open(path)
    try:
        messages = await store.load(session_key)
    finally:
        await store.close()
    return replay_from_messages(tenant, session_key, mode, messages)


# --- dispatch helpers -------------------------------------------------------


async def _list_sessions_for_request(
    state: AdminState, data_dir: Path, tenant: TenantId
) -> list[SessionListRow]:
    if _should_use_flat_legacy_sessions(state, tenant):
        return await _list_flat_legacy_sessions(data_dir)
    return await replay_list_sessions(data_dir, _to_replay_tenant(tenant))


# --- journal-backed primary path ------------------------------------------


def _journal_path(data_dir: Path) -> Path:
    """Resolve the same on-disk journal path
    ``agent_servicer._get_journal`` uses, so both reader and writer hit
    the same file."""
    return data_dir / "agent_journal.sqlite"


async def _list_from_journal(
    state: AdminState, data_dir: Path
) -> list[SessionSummaryOut] | None:
    """Read the active sessions list from the per-turn journal.

    Returns:

    * ``None`` on any failure (journal missing, schema error, import
      error) — caller falls back to the legacy ``sessions.sqlite``
      listing. Logged at debug so a fresh deployment with no journal
      yet doesn't spam the operator.
    * An empty list when the journal exists but holds no turns yet
      (also triggers fallback — see ``list_handler``).
    * A populated list when at least one session has been journaled.
    """
    try:
        # Lazy import: the journal facade itself is cheap, but we want
        # the import to stay out of the module-load path so a missing
        # ``corlinman_server.agent_journal`` doesn't poison the whole
        # ``routes_admin_a`` import chain.
        from corlinman_server.agent_journal import AgentJournal
    except ImportError as exc:  # pragma: no cover — defensive
        logger.debug("admin.sessions.journal_import_failed", error=str(exc))
        return None

    path = _journal_path(data_dir)
    if not path.exists():
        # The chat path lazily creates the journal on the first turn;
        # before that the file is absent. Treat as "no journal" so the
        # legacy fallback can answer.
        return None

    journal: Any | None = None
    try:
        # We deliberately open + close per request — opening sqlite is
        # cheap (<1ms) and the alternative (shared connection with the
        # servicer) would require plumbing the live journal handle
        # through ``AdminState``, which the bootstrapper does not own.
        journal = await AgentJournal.open(path)
        summaries = await journal.list_session_summaries()
    except Exception as exc:  # noqa: BLE001 — degrade silently to legacy
        logger.debug(
            "admin.sessions.journal_list_failed", error=str(exc), path=str(path)
        )
        return None
    finally:
        if journal is not None:
            try:
                await journal.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass

    return [
        SessionSummaryOut(
            session_key=s.session_key,
            last_message_at=s.last_seen_at_ms,
            message_count=s.message_count,
            last_user_text=s.last_user_text,
            last_status=s.last_status,
        )
        for s in summaries
    ]


async def _delete_from_journal(
    state: AdminState, data_dir: Path, session_key: str
) -> int | None:
    """Delete ``session_key`` from the journal. Returns:

    * ``None`` when the journal is unavailable (route maps to 503).
    * ``0`` when the journal opened cleanly but no turns matched
      (route maps to 404).
    * ``>0`` on success — the number of turn rows deleted.
    """
    try:
        from corlinman_server.agent_journal import AgentJournal
    except ImportError:  # pragma: no cover — defensive
        return None

    path = _journal_path(data_dir)
    if not path.exists():
        return None

    journal: Any | None = None
    try:
        journal = await AgentJournal.open(path)
        return await journal.delete_session(session_key)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "admin.sessions.journal_delete_failed",
            error=str(exc),
            session_key=session_key,
        )
        return None
    finally:
        if journal is not None:
            try:
                await journal.close()
            except Exception:  # noqa: BLE001
                pass


async def _delete_all_from_journal(
    state: AdminState, data_dir: Path
) -> int | None:
    """Wipe every session from the journal. Returns ``None`` on
    unavailable, otherwise the aggregate count of deleted turn rows."""
    try:
        from corlinman_server.agent_journal import AgentJournal
    except ImportError:  # pragma: no cover
        return None

    path = _journal_path(data_dir)
    if not path.exists():
        return 0

    journal: Any | None = None
    try:
        journal = await AgentJournal.open(path)
        summaries = await journal.list_session_summaries(limit=10_000)
        total = 0
        for s in summaries:
            total += await journal.delete_session(s.session_key)
        return total
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "admin.sessions.journal_delete_all_failed", error=str(exc)
        )
        return None
    finally:
        if journal is not None:
            try:
                await journal.close()
            except Exception:  # noqa: BLE001
                pass


async def _wipe_memory_for_session(state: AdminState, session_key: str) -> None:
    """Best-effort: clear the memory store entries for ``session_key``.

    The Python memory host does NOT currently expose a
    ``forget_session`` API; this hook is a forward-compat shim so when
    the host grows that surface (or a tenant-aware
    ``MemoryHost.delete_by_session`` analogue), the delete route picks
    it up without a code change. Until then we log at debug and return.
    """
    host = getattr(state, "memory_host", None)
    if host is None:
        return
    forget = getattr(host, "forget_session", None)
    if forget is None:
        logger.debug(
            "admin.sessions.memory_forget_unavailable",
            session_key=session_key,
        )
        return
    try:
        result = forget(session_key)
        if hasattr(result, "__await__"):
            await result
    except Exception as exc:  # noqa: BLE001 — log + continue
        logger.warning(
            "admin.sessions.memory_forget_failed",
            error=str(exc),
            session_key=session_key,
        )


async def _replay_for_request(
    state: AdminState,
    data_dir: Path,
    tenant: TenantId,
    session_key: str,
    mode: ReplayMode,
) -> Any:
    rep_tenant = _to_replay_tenant(tenant)
    if _should_use_flat_legacy_sessions(state, tenant):
        return await _replay_flat_legacy_session(
            data_dir, rep_tenant, session_key, mode
        )
    return await replay_fn(data_dir, rep_tenant, session_key, mode)


def _parse_mode(raw: str | None) -> ReplayMode:
    """Map the wire ``mode`` field to a :class:`ReplayMode`. ``None`` /
    empty defaults to ``TRANSCRIPT`` (matches the CLI default)."""
    if raw is None:
        return ReplayMode.TRANSCRIPT
    lowered = raw.lower()
    if lowered == "rerun":
        return ReplayMode.RERUN
    return ReplayMode.TRANSCRIPT


def _replay_to_dict(out: Any) -> dict[str, Any]:
    """Serialise a :class:`ReplayOutput` to the same JSON shape the Rust
    side emits."""
    summary = {
        "message_count": out.summary.message_count,
        "tenant_id": out.summary.tenant_id,
    }
    if out.summary.rerun_diff is not None:
        summary["rerun_diff"] = out.summary.rerun_diff
    return {
        "session_key": out.session_key,
        "mode": out.mode,
        "transcript": [
            {"role": m.role, "content": m.content, "ts": m.ts}
            for m in out.transcript
        ],
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    """Sub-router for ``/admin/sessions*``."""
    r = APIRouter(dependencies=[Depends(require_admin_dependency)])

    @r.get(
        "/admin/sessions",
        response_model=SessionsListOut,
        summary="List sessions for the resolved tenant",
    )
    async def list_handler(
        state: Annotated[AdminState, Depends(get_admin_state)],
        tenant: Annotated[str | None, Query()] = None,
    ) -> SessionsListOut:
        if state.sessions_disabled:
            raise _sessions_disabled()
        tenant_id = _resolve_tenant(state, tenant)
        data_dir = _resolve_data_dir(state)

        # Primary path: read from ``agent_journal.sqlite`` — that is
        # where the live ``agent_servicer`` writes chat history. The
        # legacy ``sessions.sqlite`` file is no longer written by any
        # code path so reading from it always returns an empty list,
        # which is why this page looked broken.
        journal_rows = await _list_from_journal(state, data_dir)
        if journal_rows is not None and len(journal_rows) >= 1:
            return SessionsListOut(sessions=journal_rows)

        # Fallback: legacy ``sessions.sqlite`` listing. Kept as a safety
        # net so a deployment that *does* still write there
        # (third-party tooling, old data dirs) still surfaces its
        # rows. When neither source has data the list is empty.
        try:
            rows = await _list_sessions_for_request(state, data_dir, tenant_id)
        except StoreOpenError:
            # No sessions.sqlite for this tenant yet — return an empty
            # list (matches the Rust handler's StoreOpen path).
            rows = []
        except ReplayError as exc:
            raise _storage_error(exc) from exc
        return SessionsListOut(
            sessions=[
                SessionSummaryOut(
                    session_key=r.session_key,
                    last_message_at=r.last_message_at,
                    message_count=r.message_count,
                )
                for r in rows
            ]
        )

    @r.delete(
        "/admin/sessions/{session_key}",
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Wipe a session's journal trail + memory entries",
    )
    async def delete_handler(
        session_key: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> Response:
        if state.sessions_disabled:
            raise _sessions_disabled()
        data_dir = _resolve_data_dir(state)
        deleted = await _delete_from_journal(state, data_dir, session_key)
        if deleted is None:
            # Journal unavailable — operator can't wipe a session we
            # have no read/write surface for. Distinct from "no rows
            # matched" (which is 404 below).
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "journal_unavailable"},
            )
        if deleted == 0:
            raise _session_not_found(session_key)
        # Best-effort memory wipe — does NOT block the 204 if it fails.
        # The inbox + blackboard are operational state and stay intact
        # (see module docstring).
        await _wipe_memory_for_session(state, session_key)
        logger.warning(
            "admin.sessions.deleted",
            session_key=session_key,
            turn_rows=deleted,
        )
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @r.delete(
        "/admin/sessions",
        response_model=DeleteAllOut,
        summary="Wipe every session in the journal (operator nuke)",
    )
    async def delete_all_handler(
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> DeleteAllOut:
        if state.sessions_disabled:
            raise _sessions_disabled()
        data_dir = _resolve_data_dir(state)
        deleted = await _delete_all_from_journal(state, data_dir)
        if deleted is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "journal_unavailable"},
            )
        logger.warning(
            "admin.sessions.cleared_all",
            deleted=deleted,
        )
        return DeleteAllOut(deleted=deleted)

    @r.post(
        "/admin/sessions/{session_key}/replay",
        summary="Deterministic replay of a session",
    )
    async def replay_handler(
        session_key: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
        tenant: Annotated[str | None, Query()] = None,
        body: ReplayBody | None = None,
    ) -> dict[str, Any]:
        if state.sessions_disabled:
            raise _sessions_disabled()

        mode = _parse_mode(body.mode if body is not None else None)
        tenant_id = _resolve_tenant(state, tenant)
        data_dir = _resolve_data_dir(state)

        # Always run the underlying replay in TRANSCRIPT mode — rerun
        # mode is wholly served by the chat-service plumbing in
        # ``routes_admin_b`` which the admin-A slice doesn't own.
        try:
            out = await _replay_for_request(
                state, data_dir, tenant_id, session_key, ReplayMode.TRANSCRIPT
            )
        except (SessionNotFoundError, StoreOpenError) as exc:
            raise _session_not_found(session_key) from exc
        except (StoreLoadError, ReplayError) as exc:
            raise _storage_error(exc) from exc

        if mode == ReplayMode.TRANSCRIPT:
            return _replay_to_dict(out)

        # mode == RERUN — the chat-service handle (Rust: ``replay_chat_service``)
        # is owned by ``routes_admin_b``. Until it's wired we return the
        # same 503 envelope the Rust side emits when the service is
        # missing.
        raise _rerun_disabled()

    return r


__all__ = [
    "DeleteAllOut",
    "ReplayBody",
    "SessionSummaryOut",
    "SessionsListOut",
    "router",
]
