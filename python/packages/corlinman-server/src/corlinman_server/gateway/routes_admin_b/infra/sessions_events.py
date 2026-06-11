"""``/admin/sessions/{key}/events*`` — W1.3 task-observability surface.

Three endpoints, sharing one ``EventEnvelope`` wire format for the SSE +
JSON event surfaces and a separate per-turn-metadata shape for the
past-turns navigator:

* ``GET /admin/sessions/{key}/events/live`` — Server-Sent Events feed.
  Each frame is one envelope. Supports ``Last-Event-ID`` /
  ``?last_event_id=`` for resumable catch-up (the source of truth being
  the per-turn journal). Ten-second SSE comment heartbeat to keep
  intermediaries from idling the connection out.

* ``GET /admin/sessions/{key}/turns/{turn_id}/events`` — JSON replay.
  Paginates over ``turn_events`` for a single turn so the per-turn
  drill-down page (W2.2) can render the same timeline as the live
  stream.

* ``GET /admin/sessions/{key}/turns`` — past-turns listing for the
  session-detail page's pill row. Cursor-paginated via
  ``before_turn_id``; each row carries the W1.2 aggregate columns
  (elapsed_ms, tool_call_count, estimated_cost_usd, cost_status,
  reasoning_token_count) plus a truncated user-text preview.

Auth: all three routes mount behind :func:`require_admin` — same cookie /
HTTP-Basic guard the rest of routes_admin_b uses.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from corlinman_server.gateway.routes_admin_b.state import (
    AdminState,
    get_admin_state,
    require_admin,
)

# SSE heartbeat cadence — same 10s opencode uses for its ``/event``
# stream. Keeps proxies / load balancers from idling the connection
# while the agent is silent between turns.
_log = logging.getLogger("corlinman.gateway.admin.sessions_events")

SSE_HEARTBEAT_SECONDS: float = 10.0

# JSON replay default + cap. Default is the "show me the whole turn"
# size most UIs need on first load; the cap protects against a
# pathological request from materialising a 100k-row turn into a single
# response.
REPLAY_DEFAULT_LIMIT: int = 500
REPLAY_MAX_LIMIT: int = 5000

# Page size for the live-SSE catch-up replay. The backlog is delivered
# in chunks of this many events so at most this many are held in memory
# at once (paging delivers the whole range — no events are dropped),
# and each page's journal cursor is drained fully before any frame is
# yielded so it always closes cleanly.
CATCH_UP_PAGE_SIZE: int = 1000

# Past-turns listing default + cap. 50 matches the default pill-row
# the session-detail page renders on first load; the 200 cap keeps a
# pathological request from materialising every turn of a long-lived
# session into one response (the navigator paginates via
# ``before_turn_id`` past that).
TURNS_LIST_DEFAULT_LIMIT: int = 50
TURNS_LIST_MAX_LIMIT: int = 200


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _disabled_503(reason: str, message: str) -> JSONResponse:
    """Typed disabled-503 envelope matching the rest of routes_admin_b."""
    return JSONResponse(
        status_code=503,
        content={"error": reason, "message": message},
    )


def _parse_last_event_id(raw: str | None) -> tuple[str | None, int]:
    """Parse a ``Last-Event-ID`` / ``last_event_id`` value.

    Accepts two shapes:

    * ``"<turn_id>:<sequence>"`` — composite id emitted by the live SSE
      writer below. Returns ``(turn_id, sequence)``.
    * ``"<sequence>"`` — bare integer. Returns ``(None, sequence)``; the
      live route then catches up against the *latest* turn for the
      session (best-effort).

    Returns ``(None, -1)`` on a missing or malformed value (no
    catch-up — start streaming live envelopes only).
    """
    if not raw:
        return None, -1
    raw = raw.strip()
    if not raw:
        return None, -1
    if ":" in raw:
        turn_part, _, seq_part = raw.partition(":")
        try:
            return (turn_part or None), int(seq_part)
        except ValueError:
            return None, -1
    try:
        return None, int(raw)
    except ValueError:
        return None, -1


def _format_sse_frame(envelope_dict: dict[str, Any]) -> bytes:
    """Render an envelope dict (already in the W1.1 wire shape) as one
    SSE frame.

    The ``id:`` carries the composite ``<turn_id>:<sequence>`` so a
    reconnecting client can pass it back via ``Last-Event-ID`` and
    resume exactly. ``event:`` carries the discriminator tag for
    client-side dispatch. ``data:`` is the full envelope JSON.
    """
    turn_id = envelope_dict.get("turn_id", "")
    sequence = envelope_dict.get("sequence", 0)
    event_type = str(envelope_dict.get("event_type", "Event"))
    data = json.dumps(envelope_dict, default=str)
    frame = (
        f"id: {turn_id}:{sequence}\n"
        f"event: {event_type}\n"
        f"data: {data}\n\n"
    )
    return frame.encode("utf-8")


async def _resolve_latest_turn_id(journal: Any, session_key: str) -> str | None:
    """Return the most-recent turn id for ``session_key`` as a string
    (the storage key in ``turn_events.turn_id`` is TEXT). ``None`` when
    the session has no turns yet — the live route just streams from now
    on without a catch-up replay.
    """
    try:
        turn_ids = await journal.get_session_turn_ids(session_key, limit=1)
    except Exception:  # noqa: BLE001 — best-effort
        return None
    if not turn_ids:
        return None
    return str(turn_ids[0])


# ---------------------------------------------------------------------------
# SSE generator
# ---------------------------------------------------------------------------


async def _sse_stream(
    state: AdminState,
    session_key: str,
    catch_up_turn_id: str | None,
    catch_up_sequence: int,
) -> Any:
    """Generator yielding SSE frames for ``session_key``.

    Subscribes to the live emitter *first* (so any envelope emitted
    after this point lands in the queue), then drains a catch-up replay
    from the journal, then loops on the queue. A 10s heartbeat ``:``
    comment frame fires whenever no event has been seen for the
    interval.
    """
    emitter = state.event_emitter
    journal = state.journal
    if emitter is None or journal is None:
        # Shouldn't happen — the route checked before entering. Yield a
        # terminal SSE error frame so the client sees *something* rather
        # than an empty stream.
        yield b": observability_disabled\n\n"
        return

    queue, unsubscribe = await emitter.subscribe(session_key)
    try:
        # ---------- catch-up ----------
        # Resolve the turn to catch up against. If the client passed a
        # composite Last-Event-ID we use that turn; otherwise we fall
        # back to the latest turn for the session.
        resume_turn = catch_up_turn_id
        if resume_turn is None and catch_up_sequence >= 0:
            resume_turn = await _resolve_latest_turn_id(journal, session_key)

        if resume_turn is not None and catch_up_sequence >= 0:
            # Replay the catch-up backlog in bounded PAGES, never yielding
            # while a journal cursor is open. Two hazards are designed out:
            #
            # 1) Deadlock (the CI 6-hour py-test hang): the old code
            #    yielded from inside ``async for … iter_events(...)``; a
            #    client disconnect (GeneratorExit at the yield) tore down
            #    the aiosqlite cursor mid-iteration → cross-thread cleanup
            #    that can wedge the DB worker. Here each page is drained in
            #    a SHIELDED child task, so a disconnect lets that drain run
            #    to completion (cursor closes normally) instead of being
            #    cancelled mid-iteration, and we only ``yield`` once the
            #    cursor is already closed.
            #
            # 2) Lost events: paging (rather than a single capped buffer)
            #    delivers the WHOLE ``sequence > last_event_id`` range with
            #    bounded memory — a reconnect far behind a long turn never
            #    silently skips the gap past a cap.
            seq_cursor = catch_up_sequence

            async def _drain_page(start: int) -> list[Any]:
                out: list[Any] = []
                async for ev in journal.iter_events(
                    resume_turn, start_sequence=start
                ):
                    out.append(ev)
                    if len(out) >= CATCH_UP_PAGE_SIZE:
                        break
                return out

            while True:
                try:
                    page = await asyncio.shield(
                        asyncio.ensure_future(_drain_page(seq_cursor))
                    )
                except Exception:  # noqa: BLE001 — best-effort catch-up
                    # A partial replay beats tearing the connection down;
                    # the live loop below still picks up fresh envelopes.
                    break
                if not page:
                    break
                for ev in page:
                    yield _format_sse_frame(ev)
                last_seq = page[-1].get("sequence")
                # Stop on the final (partial) page, or if the sequence did
                # not advance (defensive — avoids an infinite re-query).
                if (
                    len(page) < CATCH_UP_PAGE_SIZE
                    or not isinstance(last_seq, int)
                    or last_seq <= seq_cursor
                ):
                    break
                seq_cursor = last_seq

        # ---------- live ----------
        while True:
            try:
                envelope = await asyncio.wait_for(
                    queue.get(), timeout=SSE_HEARTBEAT_SECONDS
                )
            except TimeoutError:
                # SSE comment frame — standard keepalive. Comment lines
                # are silently ignored by ``EventSource`` clients.
                yield b": keepalive\n\n"
                continue
            yield _format_sse_frame(envelope.to_json())
    except asyncio.CancelledError:
        # Client disconnect — propagate after cleanup.
        raise
    finally:
        # Cleanup must never wedge the connection slot (or a test
        # driver's ``aclose()``): bound it and swallow stragglers.
        try:
            await asyncio.wait_for(unsubscribe(), timeout=2.0)
        except Exception:  # noqa: BLE001 — best-effort teardown
            pass


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    r = APIRouter(
        dependencies=[Depends(require_admin)],
        tags=["admin", "sessions", "observability"],
    )

    # ------------------------------------------------------------------
    # GET /admin/sessions/{key}/events/live
    # ------------------------------------------------------------------

    @r.get("/admin/sessions/{key}/events/live")
    async def stream_session_events(
        request: Request,
        key: str = Path(..., description="Session key to subscribe to."),
        last_event_id_query: str | None = Query(
            None,
            alias="last_event_id",
            description=(
                "Optional ``<turn_id>:<sequence>`` (or bare ``<sequence>`` "
                "against the latest turn) to resume from. Overrides the "
                "``Last-Event-ID`` header when both are set."
            ),
        ),
        last_event_id_header: str | None = Header(
            None,
            alias="Last-Event-ID",
            description=(
                "Standard EventSource resume header — same shape as "
                "``last_event_id`` query."
            ),
        ),
    ) -> Any:
        state = get_admin_state()
        if state.event_emitter is None or state.journal is None:
            return _disabled_503(
                "observability_disabled",
                "task observability is not wired on this gateway",
            )

        raw = last_event_id_query or last_event_id_header
        catch_up_turn_id, catch_up_sequence = _parse_last_event_id(raw)

        return StreamingResponse(
            _sse_stream(state, key, catch_up_turn_id, catch_up_sequence),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                # nginx reverse-proxy hint: do not buffer this body.
                "X-Accel-Buffering": "no",
                # Hint to EventSource clients about the resume id shape.
                "X-Event-Id-Format": "turn_id:sequence",
            },
        )

    # ------------------------------------------------------------------
    # GET /admin/sessions/{key}/turns/{turn_id}/events
    # ------------------------------------------------------------------

    @r.get("/admin/sessions/{key}/turns/{turn_id}/events")
    async def get_turn_events(
        key: str = Path(..., description="Session key (informational; not filtered)."),
        turn_id: str = Path(..., description="Turn id to replay."),
        after_sequence: int = Query(
            -1,
            ge=-1,
            description=(
                "Return events with ``sequence > after_sequence``. "
                "``-1`` (default) returns every event."
            ),
        ),
        limit: int = Query(
            REPLAY_DEFAULT_LIMIT,
            ge=1,
            le=REPLAY_MAX_LIMIT,
            description="Max events to return in one response.",
        ),
    ) -> dict[str, Any]:
        state = get_admin_state()
        journal = state.journal
        if journal is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "observability_disabled",
                    "message": "journal is not wired on this gateway",
                },
            )

        # ``iter_events`` is the streaming variant — use it so we don't
        # buffer the entire turn into memory when ``limit`` is small.
        events: list[dict[str, Any]] = []
        try:
            async for ev in journal.iter_events(
                turn_id, start_sequence=after_sequence
            ):
                events.append(ev)
                if len(events) >= limit:
                    break
        except Exception as exc:  # noqa: BLE001 — degrade to empty
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "replay_failed",
                    "message": str(exc),
                },
            ) from exc

        next_cursor: int | None = None
        if events and len(events) >= limit:
            # Probe one past the cap so the UI knows whether to ask for
            # more. We don't actually return the probe — it gets
            # re-fetched as the first event of the next page.
            last_seq = int(events[-1]["sequence"])
            try:
                async for _ in journal.iter_events(
                    turn_id, start_sequence=last_seq
                ):
                    next_cursor = last_seq
                    break
            except Exception:  # noqa: BLE001 — best-effort
                next_cursor = None

        # Session key is informational on this route: the journal's
        # primary key is (turn_id, sequence), so we return what we got
        # regardless of which session owns the turn. The UI passes
        # ``key`` through for breadcrumbing; we echo it back.
        return {
            "session_key": key,
            "turn_id": turn_id,
            "events": events,
            "next_cursor": next_cursor,
        }

    # ------------------------------------------------------------------
    # GET /admin/sessions/{key}/turns
    # ------------------------------------------------------------------

    @r.get("/admin/sessions/{key}/turns")
    async def list_session_turns(
        key: str = Path(..., description="Session key to list turns for."),
        limit: int = Query(
            TURNS_LIST_DEFAULT_LIMIT,
            ge=1,
            le=TURNS_LIST_MAX_LIMIT,
            description=(
                "Max turns to return per page. Server-side clamped to "
                f"[1, {TURNS_LIST_MAX_LIMIT}]."
            ),
        ),
        before_turn_id: str | None = Query(
            None,
            description=(
                "Cursor: return turns whose ``started_at_ms`` is strictly "
                "less than the cursor turn's. Walk the navigator with "
                "the previous response's ``next_cursor``."
            ),
        ),
    ) -> dict[str, Any]:
        """Past-turns listing for the session-detail pill row (W1.2 UI).

        Powers the past-turns navigator: each row carries the W1.2
        aggregate columns (elapsed_ms, tool_call_count, cost, etc.) plus
        a truncated user-text preview so the UI can render rich pills
        without a per-turn round trip.

        Ordered ``started_at_ms DESC``. ``next_cursor`` is the trailing
        turn id when the page filled to ``limit`` (callers should pass
        it back as ``before_turn_id`` to fetch the next page); ``None``
        when the page was short, signalling the end of the listing.

        503 ``observability_disabled`` when the journal isn't wired —
        matches the neighbouring SSE / replay routes' degradation shape.
        """
        state = get_admin_state()
        journal = state.journal
        if journal is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "observability_disabled",
                    "message": "journal is not wired on this gateway",
                },
            )
        turns = await journal.list_session_turns(
            key, limit=limit, before_turn_id=before_turn_id
        )
        # ``next_cursor`` is only set when the page filled to ``limit``
        # — otherwise we've hit the end and the UI should stop walking.
        next_cursor: str | None = None
        if len(turns) >= limit and turns:
            next_cursor = str(turns[-1]["turn_id"])
        return {
            "session_key": key,
            "turns": turns,
            "next_cursor": next_cursor,
        }

    return r


__all__ = [
    "REPLAY_DEFAULT_LIMIT",
    "REPLAY_MAX_LIMIT",
    "SSE_HEARTBEAT_SECONDS",
    "TURNS_LIST_DEFAULT_LIMIT",
    "TURNS_LIST_MAX_LIMIT",
    "router",
]
