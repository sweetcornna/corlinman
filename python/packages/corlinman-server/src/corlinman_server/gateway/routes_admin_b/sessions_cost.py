"""``GET /admin/sessions/{key}/cost`` — W2.3 cost aggregate.

Aggregates the W1.2 per-turn cost / elapsed / tool-call columns into one
session-level rollup the admin UI's sticky footer + session list both
consume. The query runs straight against the journal's ``turns`` table
so the route stays O(1) regardless of how many ``turn_events`` a
session has accumulated.

Pre-W1.2 fallback: for any completed turn whose ``cost_status`` column
is still ``NULL`` (turn started before the migration column was
populated), the route scans that turn's ``turn_events`` for a
``TurnComplete`` envelope and reads ``payload.usage`` from there. Best
effort — a turn that pre-dates *both* the column and the event journal
just doesn't contribute cost (still counts toward ``turn_count``).
"""

from __future__ import annotations

from typing import Any, cast

import structlog
from fastapi import APIRouter, Depends, HTTPException, Path

from corlinman_server.gateway.routes_admin_b.state import (
    get_admin_state,
    require_admin,
)

logger = structlog.get_logger(__name__)


# Rough OpenAI-style usage → cost coefficients. The agent's ``_CostMeter``
# stores raw token totals; for the pre-W1.2 fallback path we don't know
# which model produced them so we use a deliberately *low* coefficient
# (cheapest Claude Haiku rate, ~2026 pricing) to avoid over-stating the
# bill. The UI flags this with ``cost_status_breakdown.unknown`` so an
# operator who cares about accuracy can see how much of the total comes
# from the estimate.
_FALLBACK_INPUT_USD_PER_1K_TOKEN = 0.00025
_FALLBACK_OUTPUT_USD_PER_1K_TOKEN = 0.00125


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _aggregate_via_sqlite(
    conn: Any, session_key: str
) -> dict[str, Any] | None:
    """Single-query aggregate against the SQLite ``turns`` table.

    Returns ``None`` when the SELECT fails (caller falls through to a
    per-turn scan). The result dict carries:

    * ``turn_count`` — total completed turns
    * ``total_elapsed_ms`` — SUM(elapsed_ms), nullable
    * ``total_cost_usd`` — SUM(estimated_cost_usd), nullable
    * ``total_tool_calls`` — SUM(tool_call_count)
    * ``last_turn_at_ms`` — MAX(ended_at_ms)
    * ``known_cost_turn_count`` — turns with cost_status NOT NULL
    * ``billed_count`` / ``estimated_count`` / ``unknown_count``
    """
    try:
        cur = await conn.execute(
            "SELECT "
            "  COUNT(*), "
            "  SUM(COALESCE(elapsed_ms, 0)), "
            "  SUM(COALESCE(estimated_cost_usd, 0.0)), "
            "  SUM(COALESCE(tool_call_count, 0)), "
            "  MAX(COALESCE(ended_at_ms, started_at_ms)), "
            "  SUM(CASE WHEN cost_status = 'billed' THEN 1 ELSE 0 END), "
            "  SUM(CASE WHEN cost_status = 'estimated' THEN 1 ELSE 0 END), "
            "  SUM(CASE WHEN cost_status IS NULL THEN 1 ELSE 0 END) "
            "FROM turns "
            "WHERE session_key = ? AND status = 'completed'",
            (session_key,),
        )
        row = await cur.fetchone()
        await cur.close()
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning(
            "sessions_cost.aggregate_failed",
            session_key=session_key,
            error=str(exc),
        )
        return None
    if row is None:
        return None
    return {
        "turn_count": int(row[0] or 0),
        "total_elapsed_ms": int(row[1] or 0),
        "total_cost_usd": float(row[2] or 0.0),
        "total_tool_calls": int(row[3] or 0),
        "last_turn_at_ms": int(row[4] or 0) or None,
        "billed_count": int(row[5] or 0),
        "estimated_count": int(row[6] or 0),
        "unknown_count": int(row[7] or 0),
    }


async def _list_completed_turn_ids(
    conn: Any, session_key: str
) -> list[int]:
    """Return ``turn_id`` for every ``completed`` turn in ``session_key``
    where ``cost_status IS NULL`` — these are the pre-W1.2 rows the
    fallback path needs to scan for ``TurnComplete`` events.
    """
    try:
        cur = await conn.execute(
            "SELECT turn_id FROM turns "
            "WHERE session_key = ? AND status = 'completed' "
            "AND cost_status IS NULL",
            (session_key,),
        )
        rows = await cur.fetchall()
        await cur.close()
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning(
            "sessions_cost.list_unknown_failed",
            session_key=session_key,
            error=str(exc),
        )
        return []
    return [int(r[0]) for r in rows]


async def _fallback_cost_from_turn_events(
    journal: Any, turn_ids: list[int]
) -> float:
    """Walk each turn's ``turn_events`` for a ``TurnComplete`` payload
    and extract ``usage`` token totals; sum a coefficient-based cost.

    Best-effort: a missing TurnComplete event, an unparseable payload,
    or a usage shape without ``input_tokens`` / ``output_tokens`` keys
    each contribute 0 (still bumped via the ``unknown_count`` from the
    SQL aggregate so the operator sees the gap).
    """
    if not turn_ids:
        return 0.0
    total_cost = 0.0
    for tid in turn_ids:
        try:
            async for ev in journal.iter_events(tid, start_sequence=-1):
                if ev.get("event_type") != "TurnComplete":
                    continue
                payload = ev.get("payload") or {}
                if not isinstance(payload, dict):
                    continue
                # Two shapes the agent may serialise:
                #
                # * ``payload.estimated_cost_usd`` — already-computed by
                #   the agent's ``_CostMeter``. Prefer when present.
                # * ``payload.usage`` — raw token bucket; we coefficient
                #   it ourselves with the fallback rates.
                est = payload.get("estimated_cost_usd")
                if isinstance(est, (int, float)) and est > 0:
                    total_cost += float(est)
                    break
                usage = payload.get("usage") or {}
                if not isinstance(usage, dict):
                    continue
                try:
                    inp = int(usage.get("input_tokens", 0) or 0)
                    out = int(usage.get("output_tokens", 0) or 0)
                except (TypeError, ValueError):
                    continue
                total_cost += (
                    inp / 1000.0 * _FALLBACK_INPUT_USD_PER_1K_TOKEN
                    + out / 1000.0 * _FALLBACK_OUTPUT_USD_PER_1K_TOKEN
                )
                break
        except Exception as exc:  # noqa: BLE001 — best-effort per turn
            logger.warning(
                "sessions_cost.fallback_scan_failed",
                turn_id=tid,
                error=str(exc),
            )
            continue
    return total_cost


def _resolve_conn(journal: Any) -> Any | None:
    """Reach into the journal's backend for the SQLite connection.

    Read-only — the route never mutates the journal. We accept any
    backend that exposes a ``_c`` aiosqlite-shaped connection (the
    default :class:`SqliteJournalBackend` does). Other backends (the
    Postgres stub) return ``None`` here and the route degrades to the
    journal-facade-only path.
    """
    backend = getattr(journal, "backend", None)
    if backend is None:
        return None
    # ``_c`` is a property that raises if the backend isn't open; we
    # swallow because a partial-port deployment may legitimately
    # construct an unopened backend.
    try:
        return backend._c  # type: ignore[attr-defined]
    except (AttributeError, RuntimeError):
        return None


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def router() -> APIRouter:
    r = APIRouter(
        dependencies=[Depends(require_admin)],
        tags=["admin", "sessions", "observability"],
    )

    @r.get("/admin/sessions/{key}/cost")
    async def get_session_cost(
        key: str = Path(..., description="Session key to aggregate."),
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

        conn = _resolve_conn(journal)
        agg: dict[str, Any] | None = None
        if conn is not None:
            agg = await _aggregate_via_sqlite(conn, key)

        if agg is None:
            # Degraded path — journal is non-SQLite or the aggregate
            # SQL failed. We still return a typed envelope so the UI
            # doesn't have to special-case 500s.
            return {
                "session_key": key,
                "turn_count": 0,
                "total_elapsed_ms": 0,
                "total_cost_usd": 0.0,
                "cost_status_breakdown": {
                    "estimated": 0,
                    "billed": 0,
                    "unknown": 0,
                },
                "total_tool_calls": 0,
                "last_turn_at_ms": None,
                "avg_turn_ms": 0,
            }

        # Pre-W1.2 fallback: turns with cost_status IS NULL get their
        # cost back-filled from ``TurnComplete`` event payloads.
        fallback_cost = 0.0
        if agg["unknown_count"] > 0 and conn is not None:
            unknown_ids = await _list_completed_turn_ids(conn, key)
            fallback_cost = await _fallback_cost_from_turn_events(
                journal, unknown_ids
            )

        total_cost_usd = float(agg["total_cost_usd"]) + fallback_cost
        turn_count = int(agg["turn_count"])
        total_elapsed_ms = int(agg["total_elapsed_ms"])
        avg_turn_ms = (
            int(total_elapsed_ms / turn_count) if turn_count > 0 else 0
        )

        return cast(
            "dict[str, Any]",
            {
                "session_key": key,
                "turn_count": turn_count,
                "total_elapsed_ms": total_elapsed_ms,
                "total_cost_usd": round(total_cost_usd, 6),
                "cost_status_breakdown": {
                    "estimated": int(agg["estimated_count"]),
                    "billed": int(agg["billed_count"]),
                    "unknown": int(agg["unknown_count"]),
                },
                "total_tool_calls": int(agg["total_tool_calls"]),
                "last_turn_at_ms": agg["last_turn_at_ms"],
                "avg_turn_ms": avg_turn_ms,
            },
        )

    return r


__all__ = ["router"]
