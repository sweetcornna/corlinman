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
from datetime import datetime, timezone
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

    ``title`` / ``pinned`` / ``archived`` are operator-supplied metadata
    persisted via ``PATCH /admin/sessions/{key}`` (in-app chat MVP).
    Defaults: ``title=None``, ``pinned=False``, ``archived=False`` —
    legacy rows with no ``session_meta`` entry round-trip unchanged.
    """

    session_key: str
    last_message_at: int  # unix milliseconds
    message_count: int
    last_user_text: str | None = None
    last_status: str | None = None
    title: str | None = None
    pinned: bool = False
    archived: bool = False


class SessionPatchBody(BaseModel):
    """``PATCH /admin/sessions/{key}`` body.

    Every field is optional — the route requires at least one to be
    present (returns 422 otherwise via :meth:`_require_nonempty`).
    """

    title: str | None = None
    pinned: bool | None = None
    archived: bool | None = None


class SessionCancelOut(BaseModel):
    """``POST /admin/sessions/{key}/cancel`` response.

    ``status``:
        * ``"cancelled"``   — an active loop was found + cancel fired.
        * ``"not_running"`` — the session exists but has no in-progress turn.
    ``turn_id`` is the id of the cancelled turn (when known), else ``None``.
    """

    status: str
    turn_id: str | None = None


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
            # In-app chat MVP — operator metadata pulled from the
            # ``session_meta`` table via the same LEFT JOIN that powers
            # the pinned-first ordering. Defaults to (None, False,
            # False) when no meta row exists yet.
            title=s.title,
            pinned=s.pinned,
            archived=s.archived,
        )
        for s in summaries
    ]


async def _session_exists_in_journal(
    data_dir: Path, session_key: str
) -> bool:
    """Cheap existence probe — returns ``True`` iff the journal has at
    least one turn for ``session_key``. Used by the cancel + patch
    routes to surface a 404 instead of silently no-opping on a typoed
    key. Returns ``False`` when the journal is unavailable (the route
    layer prefers a 404 to a 503 here — the operator's already lost).
    """
    try:
        from corlinman_server.agent_journal import AgentJournal
    except ImportError:  # pragma: no cover — defensive
        return False
    path = _journal_path(data_dir)
    if not path.exists():
        return False
    journal: Any | None = None
    try:
        journal = await AgentJournal.open(path)
        return await journal.session_exists(session_key)
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "admin.sessions.session_exists_failed",
            error=str(exc),
            session_key=session_key,
        )
        return False
    finally:
        if journal is not None:
            try:
                await journal.close()
            except Exception:  # noqa: BLE001
                pass


async def _update_session_meta_in_journal(
    data_dir: Path,
    session_key: str,
    *,
    title: str | None,
    pinned: bool | None,
    archived: bool | None,
) -> "SessionSummaryOut | None":
    """Upsert ``session_meta`` for ``session_key`` and project the result
    back into a :class:`SessionSummaryOut`.

    Returns ``None`` when the session has no journaled turns OR the
    journal is unavailable — both map to a 404 at the route layer so
    the client gets one consistent error envelope.
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
        summary = await journal.update_session_meta(
            session_key,
            title=title,
            pinned=pinned,
            archived=archived,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "admin.sessions.update_meta_failed",
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
    if summary is None:
        return None
    return SessionSummaryOut(
        session_key=summary.session_key,
        last_message_at=summary.last_seen_at_ms,
        message_count=summary.message_count,
        last_user_text=summary.last_user_text,
        last_status=summary.last_status,
        title=summary.title,
        pinned=summary.pinned,
        archived=summary.archived,
    )


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



async def _replay_from_journal(
    data_dir: Path, tenant: TenantId, session_key: str, mode: ReplayMode
) -> dict[str, Any] | None:
    """Reconstruct a replay-shaped JSON payload from the per-turn journal.

    The legacy ``corlinman_replay`` module reads from
    ``<data_dir>/sessions.sqlite``, which the OpenAI-compat
    ``/v1/chat/completions`` path never writes to — all real chat
    history is in ``agent_journal.sqlite/turn_messages``. This helper
    joins ``turns`` + ``turn_messages`` for the requested ``session_key``
    and produces the same JSON shape ``_replay_to_dict`` would emit,
    so the route handler can use it as a drop-in replacement for the
    legacy replay when the legacy store is empty / missing the key.

    Returns ``None`` on any infrastructure failure (no journal, import
    error, etc.) so the caller can fall back to the legacy path.
    Returns ``{...}`` with an empty ``transcript`` when the session
    really has no messages — the caller decides whether to 404 or
    return an empty dump.
    """
    try:
        from corlinman_server.agent_journal import AgentJournal
    except ImportError as exc:  # pragma: no cover — defensive
        logger.debug("admin.sessions.journal_import_failed", error=str(exc))
        return None

    path = _journal_path(data_dir)
    if not path.exists():
        return None

    journal: Any | None = None
    transcript: list[dict[str, Any]] = []
    try:
        journal = await AgentJournal.open(path)
        # Pull all turns for this session at once so we have per-turn
        # ``started_at_ms`` for synthesising the per-message ts. The
        # facade returns most-recent-first; we replay in chronological
        # order so the UI bubble order is correct.
        turn_rows = await journal.list_session_turns(session_key, limit=500)
        if not turn_rows:
            return None
        for turn_row in reversed(turn_rows):
            try:
                tid = int(turn_row.get("turn_id"))
            except (TypeError, ValueError):
                continue
            started_at_ms = int(turn_row.get("started_at_ms") or 0)
            ts_iso = (
                datetime.fromtimestamp(started_at_ms / 1000.0, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
                if started_at_ms
                else ""
            )
            # ``_load_messages`` is semi-private but stable — it's the
            # facade method ``find_resumable_turn`` uses too. Returns
            # messages in seq order with role + content + tool fields.
            msgs = await journal._load_messages(tid)
            for m in msgs:
                role = str(m.get("role") or "")
                # Only surface conversational roles in the resume
                # transcript; bare ``tool`` result rows are noisy
                # for a chat UI bubble list.
                if role not in {"user", "assistant", "system"}:
                    continue
                content = m.get("content")
                if content is None:
                    # Assistant messages with only tool_calls have
                    # empty content — keep them as empty strings so
                    # the seq is preserved and the bubble renders.
                    content = ""
                transcript.append(
                    {"role": role, "content": str(content), "ts": ts_iso}
                )
    except Exception as exc:  # noqa: BLE001 — degrade silently
        logger.debug(
            "admin.sessions.journal_replay_failed",
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

    if not transcript:
        return None

    return {
        "session_key": session_key,
        "mode": ("rerun" if mode == ReplayMode.RERUN else "transcript"),
        "transcript": transcript,
        "summary": {
            "message_count": len(transcript),
            "tenant_id": tenant.as_str(),
            **(
                {"rerun_diff": "not_implemented_yet"}
                if mode == ReplayMode.RERUN
                else {}
            ),
        },
    }


async def _replay_for_request(
    state: AdminState,
    data_dir: Path,
    tenant: TenantId,
    session_key: str,
    mode: ReplayMode,
) -> Any:
    # Primary path: read from the per-turn journal where the live
    # /v1/chat/completions path actually writes. Falls back to the
    # legacy sessions.sqlite store if the journal has no messages
    # for this key (covers operators with pre-port history still
    # only in the legacy file).
    primary = await _replay_from_journal(data_dir, tenant, session_key, mode)
    if primary is not None:
        return primary

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
    side emits. Dict inputs (from the journal-backed replay path) are
    passed through unchanged — they already match the wire shape."""
    if isinstance(out, dict):
        return out
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
        "/admin/sessions/{session_key}/cancel",
        response_model=SessionCancelOut,
        summary="Cancel the in-progress turn (if any) for a session",
    )
    async def cancel_handler(
        session_key: str,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> SessionCancelOut:
        """Stop the in-flight :class:`ReasoningLoop` for ``session_key``.

        Looks up the loop via the process-level
        :func:`corlinman_server.agent_servicer.cancel_session` registry
        (mirrored from the servicer's instance-level ``_active_loops``
        map at the same insertion/deletion points so admin HTTP doesn't
        need a handle to the servicer instance).

        Response:

        * ``cancelled``     — a loop was found and ``cancel()`` fired.
        * ``not_running``   — the session_key exists but no in-progress
                              turn is registered. Falls through to 200
                              (not an error — the client polled at the
                              wrong instant).
        * 404 ``not_found`` — the session has no journaled turns and no
                              active loop; the key was likely typoed.
        """
        if state.sessions_disabled:
            raise _sessions_disabled()

        # Import lazily so the routes module stays importable in test
        # contexts that stub out the servicer.
        try:
            from corlinman_server.agent_servicer import cancel_session
        except ImportError:  # pragma: no cover — defensive
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"error": "cancel_unavailable"},
            ) from None

        # First, see if there's an active loop — even before the journal
        # check. A live loop with no journaled turns yet (the row is
        # written before the first message append) is still cancellable.
        result, turn_id = cancel_session(session_key, reason="admin_abort")
        if result == "cancelled":
            logger.info(
                "admin.sessions.cancelled",
                session_key=session_key,
                turn_id=turn_id,
            )
            return SessionCancelOut(status="cancelled", turn_id=turn_id)

        # No active loop — distinguish "session exists but is idle"
        # (200 not_running) from "session never existed" (404). We
        # only consult the journal here, so the happy path above
        # avoids a sqlite open per cancel.
        data_dir = _resolve_data_dir(state)
        if await _session_exists_in_journal(data_dir, session_key):
            return SessionCancelOut(status="not_running", turn_id=None)
        raise _session_not_found(session_key)

    @r.patch(
        "/admin/sessions/{session_key}",
        response_model=SessionSummaryOut,
        summary="Update session metadata (title / pinned / archived)",
    )
    async def patch_handler(
        session_key: str,
        body: SessionPatchBody,
        state: Annotated[AdminState, Depends(get_admin_state)],
    ) -> SessionSummaryOut:
        """Upsert operator-supplied metadata for ``session_key``.

        Body is :class:`SessionPatchBody` — every field is optional and
        ``None`` means "leave it alone". Requires at least one field
        present; an all-None body returns 422 ``empty_patch`` so a
        client that forgot to populate the body doesn't silently no-op.
        """
        if state.sessions_disabled:
            raise _sessions_disabled()
        # 422 when the body is technically valid (all Optional fields)
        # but carries no actionable change — surfacing this loudly
        # catches a buggy client.
        if (
            body.title is None
            and body.pinned is None
            and body.archived is None
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "empty_patch",
                    "message": (
                        "at least one of title / pinned / archived is required"
                    ),
                },
            )
        data_dir = _resolve_data_dir(state)
        updated = await _update_session_meta_in_journal(
            data_dir,
            session_key,
            title=body.title,
            pinned=body.pinned,
            archived=body.archived,
        )
        if updated is None:
            raise _session_not_found(session_key)
        logger.info(
            "admin.sessions.meta_updated",
            session_key=session_key,
            title_set=body.title is not None,
            pinned=body.pinned,
            archived=body.archived,
        )
        return updated

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
    "SessionCancelOut",
    "SessionPatchBody",
    "SessionSummaryOut",
    "SessionsListOut",
    "router",
]
