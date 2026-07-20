"""Module-level wire models, constants, and helpers for :mod:`sessions`.

Extracted verbatim from ``sessions.py`` to shrink that god-file. The
``router()`` factory + its handlers live in ``sessions.py`` and re-import
every name from here; nothing in this module imports ``sessions.py`` (no
import cycle). Sibling imports (``...routes_admin_a.state``, ``._auth_shim``,
the ``corlinman_replay`` replay/session stores, and the lazily-imported
``corlinman_server.agent_journal``) mirror what ``sessions.py`` used, lazy
where the original was lazy.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from corlinman_replay import (
    ReplayMode,
    SessionListRow,
    SqliteSessionStore,
    replay_from_messages,
)
from corlinman_replay import TenantId as ReplayTenantId
from corlinman_replay import (
    list_sessions as replay_list_sessions,
)
from corlinman_replay import (
    replay as replay_fn,
)
from fastapi import HTTPException, status
from pydantic import BaseModel, Field

from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
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
    # W5 — pagination over long sessions. ``before_turn_id`` is the
    # exclusive upper cursor (a ``turns.turn_id``); ``limit`` caps the
    # number of TURNS (not messages) per page. ``None`` keeps the
    # legacy newest-500 window.
    before_turn_id: str | None = None
    limit: int | None = None


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


def _resolve_request_tenant(
    state: AdminState, request: Any, tenant_q: str | None
) -> TenantId:
    """W8 — resolve the tenant a session operation is scoped to,
    capped by the authenticated principal.

    Precedence:

    1. A **non-default** principal tenant (``request.state.admin_tenant``,
       stamped by the admin-auth middleware) hard-caps the scope — a
       per-tenant admin can never select another tenant; an explicit
       mismatching ``?tenant=`` is a 403.
    2. Default-tenant principals (the operator) keep the legacy
       behaviour: ``?tenant=`` selects the tenant to view, falling back
       to the deployment default. This matches the per-tenant legacy
       ``sessions.sqlite`` stores the same routes already scope by.
    """
    resolved = _resolve_tenant(state, tenant_q)
    principal = getattr(
        getattr(request, "state", None), "admin_tenant", None
    )
    if principal is None:
        return resolved
    principal_id: TenantId
    if isinstance(principal, TenantId):
        principal_id = principal
    else:
        try:
            principal_id = TenantId.new(str(principal))
        except TenantIdError:
            return resolved
    if principal_id.is_legacy_default():
        # Operator / default-tenant admin — may view any tenant.
        return resolved
    if tenant_q and resolved != principal_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "tenant_forbidden",
                "message": "principal is not scoped to the requested tenant",
            },
        )
    return principal_id


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
    state: AdminState, data_dir: Path, tenant: TenantId
) -> list[SessionSummaryOut] | None:
    """Read the active sessions list from the per-turn journal.

    W8 — scoped to ``tenant``: sessions whose turns belong to another
    tenant are not listed (legacy unattributed rows belong to the
    default tenant).

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
        summaries = await journal.list_session_summaries(
            tenant_id=tenant.as_str()
        )
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
    data_dir: Path, session_key: str, tenant: TenantId | None = None
) -> bool:
    """Cheap existence probe — returns ``True`` iff the journal has at
    least one turn for ``session_key``. Used by the cancel + patch
    routes to surface a 404 instead of silently no-opping on a typoed
    key. Returns ``False`` when the journal is unavailable (the route
    layer prefers a 404 to a 503 here — the operator's already lost).

    W8 — ``tenant`` makes a cross-tenant session read as absent.
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
        return await journal.session_exists(
            session_key,
            tenant_id=tenant.as_str() if tenant is not None else None,
        )
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
    tenant: TenantId | None = None,
) -> SessionSummaryOut | None:
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
            tenant_id=tenant.as_str() if tenant is not None else None,
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
    state: AdminState,
    data_dir: Path,
    session_key: str,
    tenant: TenantId | None = None,
) -> int | None:
    """Delete ``session_key`` from the journal. Returns:

    * ``None`` when the journal is unavailable (route maps to 503).
    * ``0`` when the journal opened cleanly but no turns matched
      (route maps to 404).
    * ``>0`` on success — the number of turn rows deleted.

    W8 — ``tenant`` scopes the delete: a cross-tenant session matches
    nothing (``0`` → 404, indistinguishable from an unknown key).
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
        return await journal.delete_session(
            session_key,
            tenant_id=tenant.as_str() if tenant is not None else None,
        )
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
    state: AdminState, data_dir: Path, tenant: TenantId | None = None
) -> int | None:
    """Wipe every session from the journal. Returns ``None`` on
    unavailable, otherwise the aggregate count of deleted turn rows.

    W8 — ``tenant`` scopes the nuke to that tenant's sessions only;
    other tenants' journal rows survive an operator "clear all" issued
    from a tenant-scoped view.
    """
    try:
        from corlinman_server.agent_journal import AgentJournal
    except ImportError:  # pragma: no cover
        return None

    path = _journal_path(data_dir)
    if not path.exists():
        return 0

    tenant_id = tenant.as_str() if tenant is not None else None
    journal: Any | None = None
    try:
        journal = await AgentJournal.open(path)
        summaries = await journal.list_session_summaries(
            limit=10_000, tenant_id=tenant_id
        )
        total = 0
        for s in summaries:
            total += await journal.delete_session(
                s.session_key, tenant_id=tenant_id
            )
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



#: Default page size (in turns) for journal-backed replay. Pre-W5 this
#: was a silent hard truncation; it is now a cursor-pageable window.
_REPLAY_DEFAULT_TURN_LIMIT = 500


async def _replay_from_journal(
    data_dir: Path,
    tenant: TenantId,
    session_key: str,
    mode: ReplayMode,
    *,
    limit: int | None = None,
    before_turn_id: str | None = None,
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
    # Map a tool_call_id back to (transcript_idx, tool_call_idx) so a
    # later ``role="tool"`` row's content can be folded into the
    # originating assistant message's tool_calls[…].result. Without
    # this the UI shows tool calls without their results on resume.
    tc_lookup: dict[str, tuple[int, int]] = {}
    page_limit = max(1, min(limit or _REPLAY_DEFAULT_TURN_LIMIT, 1000))
    has_more = False
    oldest_turn_id: str | None = None
    try:
        journal = await AgentJournal.open(path)
        # Pull one page of turns (most-recent-first from the facade; we
        # replay in chronological order so bubble order is correct).
        # ``page_limit + 1`` over-fetch: the extra row only tells us
        # whether an older page exists — it is trimmed before replay.
        # W8 — tenant-scoped: a session owned by another tenant returns
        # zero rows here → ``None`` → the caller falls through to the
        # legacy per-tenant stores, which naturally 404.
        turn_rows = await journal.list_session_turns(
            session_key,
            limit=page_limit + 1,
            before_turn_id=before_turn_id,
            tenant_id=tenant.as_str(),
        )
        if not turn_rows:
            return None
        if len(turn_rows) > page_limit:
            has_more = True
            turn_rows = turn_rows[:page_limit]
        raw_oldest = turn_rows[-1].get("turn_id")
        oldest_turn_id = str(raw_oldest) if raw_oldest is not None else None
        # The one turn that can legitimately still be running: the NEWEST
        # turn of the FIRST page (rows are started_at_ms DESC; a session
        # runs one turn at a time, and a ``before_turn_id`` page is by
        # construction older than a newer turn). Everything else that
        # reads ``in_progress`` is a crash artifact.
        newest_raw_turn_id = (
            turn_rows[0].get("turn_id") if before_turn_id is None else None
        )
        for turn_row in reversed(turn_rows):
            raw_turn_id = turn_row.get("turn_id")
            if raw_turn_id is None:
                continue
            try:
                tid = int(raw_turn_id)
            except (TypeError, ValueError):
                continue
            # Skip the still-in-progress LIVE turn: the /chat page renders
            # it via ``resumeInFlight`` (a separate pending bubble that
            # tails the journal). A multi-step agentic turn journals its
            # intermediate assistant/tool message rows AS IT RUNS, so
            # including them in the settled transcript too double-renders
            # the turn — a frozen "已隐藏 N 个工具调用" bubble stacked above
            # the live one. ``finalizeJournalTurn`` invalidates this
            # transcript query when the turn ends, so the completed turn
            # lands here naturally on the refetch.
            #
            # L-103: the skip is scoped to the newest turn only. An OLDER
            # ``in_progress`` row is a crashed turn (never completed, then
            # the user kept chatting) — skipping it silently vanished its
            # user message + partial answer from the thread forever. Those
            # rows are real history now; replay them.
            if (
                str(turn_row.get("status") or "") == "in_progress"
                and raw_turn_id == newest_raw_turn_id
            ):
                continue
            started_at_ms = int(turn_row.get("started_at_ms") or 0)
            ts_iso = (
                datetime.fromtimestamp(started_at_ms / 1000.0, tz=UTC)
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
                if role in {"user", "assistant", "system"}:
                    content = m.get("content")
                    if content is None:
                        # Assistant messages with only tool_calls have
                        # empty content — keep them as empty strings so
                        # the seq is preserved and the bubble renders.
                        content = ""
                    entry: dict[str, Any] = {
                        "role": role,
                        "content": str(content),
                        "ts": ts_iso,
                    }
                    # W3 — attachment metadata journaled with the user
                    # message; the chat UI re-renders image/file cards
                    # from it on session resume.
                    raw_atts = m.get("attachments")
                    if isinstance(raw_atts, list) and raw_atts:
                        entry["attachments"] = [
                            dict(a) for a in raw_atts if isinstance(a, dict)
                        ]
                    raw_tcs = m.get("tool_calls")
                    if role == "assistant" and isinstance(raw_tcs, list) and raw_tcs:
                        # Pass tool_calls through in their OpenAI shape so
                        # the chat UI can rehydrate ToolCallCards on
                        # session resume.
                        normalised: list[dict[str, Any]] = []
                        for tc in raw_tcs:
                            if isinstance(tc, dict):
                                normalised.append(dict(tc))
                        if normalised:
                            entry["tool_calls"] = normalised
                            midx = len(transcript)
                            for j, tc in enumerate(normalised):
                                tcid = tc.get("id")
                                if isinstance(tcid, str) and tcid:
                                    tc_lookup[tcid] = (midx, j)
                    transcript.append(entry)
                elif role == "tool":
                    # Fold the tool result back onto the originating
                    # assistant message's tool_call so the bubble
                    # shows both invocation + result on reload.
                    tcid = m.get("tool_call_id")
                    if isinstance(tcid, str) and tcid in tc_lookup:
                        midx, jidx = tc_lookup[tcid]
                        tcs = transcript[midx].get("tool_calls")
                        if (
                            isinstance(tcs, list)
                            and 0 <= jidx < len(tcs)
                            and isinstance(tcs[jidx], dict)
                        ):
                            res = m.get("content")
                            if res is not None:
                                tcs[jidx]["result"] = str(res)
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

    # NOTE: we deliberately DO NOT early-return ``None`` for an empty
    # transcript here. ``turn_rows`` was non-empty (checked above), so the
    # session EXISTS in the journal — it just has no replayable messages yet
    # (an in-progress turn whose assistant/tool rows aren't journaled until it
    # completes). Returning the empty dump (per this function's docstring) lets
    # the /chat page render a clean empty thread + reattach the live stream,
    # instead of falling through to the write-dead legacy store and 404'ing on
    # every in-progress conversation. A genuinely unknown session has no turn
    # rows at all and still 404s via the ``not turn_rows`` path above.
    return {
        "session_key": session_key,
        "mode": ("rerun" if mode == ReplayMode.RERUN else "transcript"),
        "transcript": transcript,
        # W5 pagination envelope: pass ``oldest_turn_id`` back as
        # ``before_turn_id`` to fetch the next-older page; ``has_more``
        # is false on the final (oldest) page.
        "oldest_turn_id": oldest_turn_id,
        "has_more": has_more,
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
    *,
    limit: int | None = None,
    before_turn_id: str | None = None,
) -> Any:
    # Primary path: read from the per-turn journal where the live
    # /v1/chat/completions path actually writes. Falls back to the
    # legacy sessions.sqlite store if the journal has no messages
    # for this key (covers operators with pre-port history still
    # only in the legacy file). Pagination (W5) is journal-only —
    # the legacy stores never grew sessions long enough to need it.
    primary = await _replay_from_journal(
        data_dir,
        tenant,
        session_key,
        mode,
        limit=limit,
        before_turn_id=before_turn_id,
    )
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
