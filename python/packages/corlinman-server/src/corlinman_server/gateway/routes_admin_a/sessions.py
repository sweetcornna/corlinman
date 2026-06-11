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

The module-level wire-models, constants, and helpers used by ``router()``
and its handlers live in the sibling :mod:`._sessions_lib` module (extracted
to keep this file small) and are re-imported below.
"""

from __future__ import annotations

from typing import Annotated, Any

from corlinman_replay import (
    ReplayError,
    ReplayMode,
    SessionNotFoundError,
    StoreLoadError,
    StoreOpenError,
)
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from corlinman_server.gateway.routes_admin_a._auth_shim import (
    require_admin_dependency,
)
from corlinman_server.gateway.routes_admin_a._sessions_lib import (
    DeleteAllOut,
    ReplayBody,
    SessionCancelOut,
    SessionPatchBody,
    SessionsListOut,
    SessionSummaryOut,
    _delete_all_from_journal,
    _delete_from_journal,
    _list_from_journal,
    _list_sessions_for_request,
    _parse_mode,
    _replay_for_request,
    _replay_to_dict,
    _rerun_disabled,
    _resolve_data_dir,
    _resolve_tenant,
    _session_exists_in_journal,
    _session_not_found,
    _sessions_disabled,
    _storage_error,
    _update_session_meta_in_journal,
    _wipe_memory_for_session,
    logger,
)
from corlinman_server.gateway.routes_admin_a.state import (
    AdminState,
    get_admin_state,
)

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
                state,
                data_dir,
                tenant_id,
                session_key,
                ReplayMode.TRANSCRIPT,
                limit=body.limit if body is not None else None,
                before_turn_id=(
                    body.before_turn_id if body is not None else None
                ),
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
