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
    # Map a tool_call_id back to (transcript_idx, tool_call_idx) so a
    # later ``role="tool"`` row's content can be folded into the
    # originating assistant message's tool_calls[…].result. Without
    # this the UI shows tool calls without their results on resume.
    tc_lookup: dict[str, tuple[int, int]] = {}
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
            raw_turn_id = turn_row.get("turn_id")
            if raw_turn_id is None:
                continue
            try:
                tid = int(raw_turn_id)
            except (TypeError, ValueError):
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
