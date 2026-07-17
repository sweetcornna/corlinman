"""The :class:`MemoryKernel` facade — one handle, three pipelines.

W1 surface (shadow mode): ``observe()`` queues completed turns,
``recall()`` runs scoped BM25 over ``mk_items``, ``add_item()`` /
``invalidate_item()`` are the bi-temporal write primitives the
sleep-time reconcile job (W5) and tests drive. Later waves extend this
class rather than adding parallel entry points.

Rollout gate: :func:`kernel_mode` reads ``CORLINMAN_MEMORY_KERNEL``
(``off`` | ``shadow`` | ``on``, default ``shadow``). The kernel itself
is mode-agnostic — callers decide what to do with recall results.
"""

from __future__ import annotations

import asyncio
import itertools
import os
from pathlib import Path
from typing import Any

import aiosqlite

from corlinman_memory_kernel.ids import new_id, now_ms
from corlinman_memory_kernel.schema import MK_SCHEMA_SQL
from corlinman_memory_kernel.types import KernelScope, MemoryItem, Observation

_MODE_ENV = "CORLINMAN_MEMORY_KERNEL"
_MODES = ("off", "shadow", "on")

# Hot-path text caps: observations are raw material for sleep-time
# extraction, not a transcript store (agent_journal owns that), so long
# turns are truncated rather than stored whole.
_MAX_OBS_TEXT = 4000

# Observation-queue retention. Until the W5 reconcile job ships (and for
# deployments that never enable it), shadow mode must not grow
# mk_observations forever: every _OBS_PRUNE_EVERY-th observe() sweeps
# processed rows older than the TTL and trims the pending backlog to the
# cap (oldest first — the reconcile job wants recent material anyway).
_OBS_PRUNE_EVERY = 256
_OBS_PROCESSED_TTL_MS = 7 * 24 * 3600 * 1000
_OBS_PENDING_CAP = 20_000


def kernel_mode() -> str:
    """Resolve the rollout mode (default ``shadow``).

    ``shadow``: observations accumulate + recall runs for diff telemetry
    only. ``on``: recall results are injected (W3+). ``off``: the kernel
    is never touched. Unknown values fall back to ``shadow`` so a typo'd
    env var cannot silently disable observation accrual.
    """
    raw = os.environ.get(_MODE_ENV, "shadow").strip().lower()
    return raw if raw in _MODES else "shadow"


# CJK unified ideographs + kana; used to slice text into script runs so
# Chinese/Japanese gets trigram phrase units the tokenizer can match.
_CJK_RANGES = (
    (0x3040, 0x30FF),  # hiragana + katakana
    (0x3400, 0x4DBF),  # CJK ext A
    (0x4E00, 0x9FFF),  # CJK unified
    (0xF900, 0xFAFF),  # CJK compat
)

# Cap the number of OR units so a long message can't balloon the MATCH
# expression (BM25 ranking saturates well before this anyway).
_MAX_QUERY_UNITS = 32


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def _query_units(text: str) -> list[str]:
    """Slice free text into match units: words + sliding CJK trigrams."""
    units: list[str] = []
    for token in text.split():
        for is_cjk, chars in itertools.groupby(token, key=_is_cjk):
            run = "".join(chars)
            if is_cjk and len(run) >= 3:
                units.extend(run[i : i + 3] for i in range(len(run) - 2))
            elif run.strip('"'):
                units.append(run)
    return units[:_MAX_QUERY_UNITS]


def _trigram_match_query(units: list[str]) -> str:
    """Build a trigram-tokenizer FTS5 MATCH expression from query units.

    Deliberately NOT named like the legacy host's ``_fts_match_query`` —
    that one produces implicit-AND unicode61 syntax; merging the two
    would silently flip a store's recall semantics.

    Recall queries are chatty natural language, so units are joined with
    ``OR`` and BM25 does the ranking — requiring EVERY word (implicit
    AND, the legacy store's semantics) rejects almost any real message.
    CJK runs arrive pre-sliced into 3-char windows because the trigram
    tokenizer needs 3-char units and whole-sentence phrases would demand
    a verbatim substring match. All units are quoted, so FTS5 operator
    characters in user text match literally. Units shorter than 3 chars
    produce no trigram and are dropped here; :meth:`MemoryKernel.recall`
    falls back to a LIKE scan when nothing survives.
    """
    quoted = [
        '"{}"'.format(u.replace('"', '""'))
        for u in units
        if len(u) >= 3
    ]
    return " OR ".join(quoted)


def _escape_like(unit: str) -> str:
    r"""Escape LIKE wildcards so a unit matches literally (ESCAPE '\')."""
    return (
        unit.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


class MemoryKernel:
    """Async facade over the ``mk_*`` tables in ``memory.sqlite``.

    Single connection guarded by a lock (same discipline as the legacy
    ``LocalSqliteHost`` sharing the file); WAL + busy_timeout keep the
    two handles from blocking each other.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()
        self._observe_count = 0

    @classmethod
    async def open(cls, path: str | Path) -> MemoryKernel:
        conn = await aiosqlite.connect(str(path))
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        # busy_timeout BEFORE journal_mode: if the WAL switch raises
        # (network FS), the timeout safety net must already be in place.
        try:
            await conn.execute("PRAGMA busy_timeout = 5000")
            await conn.execute("PRAGMA journal_mode = WAL")
        except aiosqlite.Error:
            pass
        await conn.executescript(MK_SCHEMA_SQL)
        await conn.commit()
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    # ---- WRITE pipeline (hot path) --------------------------------------

    async def observe(self, obs: Observation) -> str:
        """Queue one completed turn. The ONLY hot-path write."""
        obs_id = new_id(ts_ms=obs.ts_ms)
        async with self._lock:
            await self._conn.execute(
                "INSERT INTO mk_observations("
                "id, tenant_id, session_key, channel, channel_user_id,"
                " scope_user_id, persona_id, user_text, reply_text, ts_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    obs_id,
                    obs.tenant_id,
                    obs.session_key,
                    obs.channel,
                    obs.channel_user_id,
                    obs.scope_user_id,
                    obs.persona_id,
                    obs.user_text[:_MAX_OBS_TEXT],
                    obs.reply_text[:_MAX_OBS_TEXT],
                    obs.ts_ms,
                ),
            )
            self._observe_count += 1
            if self._observe_count % _OBS_PRUNE_EVERY == 0:
                await self._prune_observations_locked()
            await self._conn.commit()
        return obs_id

    async def _prune_observations_locked(self) -> None:
        """Retention sweep (caller holds ``self._lock``; commit is theirs)."""
        await self._conn.execute(
            "DELETE FROM mk_observations WHERE processed_at_ms IS NOT NULL"
            " AND processed_at_ms < ?",
            (now_ms() - _OBS_PROCESSED_TTL_MS,),
        )
        await self._conn.execute(
            "DELETE FROM mk_observations WHERE processed_at_ms IS NULL"
            " AND id NOT IN ("
            "   SELECT id FROM mk_observations WHERE processed_at_ms IS NULL"
            "   ORDER BY ts_ms DESC LIMIT ?"
            " )",
            (_OBS_PENDING_CAP,),
        )

    # ---- item primitives (bi-temporal) -----------------------------------

    async def add_item(
        self,
        scope: KernelScope,
        *,
        text: str,
        kind: str,
        source: str,
        source_ref: str | None = None,
        visibility: str = "private",
        risk: str = "low",
        confidence: float = 0.6,
        importance: float = 0.5,
        trust: float = 0.5,
        valid_from_ms: int | None = None,
        node_id: str | None = None,
    ) -> str:
        item_id = new_id()
        now = now_ms()
        async with self._lock:
            await self._conn.execute(
                "INSERT INTO mk_items("
                "id, tenant_id, scope_user_id, persona_id, visibility, kind,"
                " text, source, source_ref, node_id, risk, confidence,"
                " importance, trust, valid_from_ms, recorded_at_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item_id,
                    scope.tenant_id,
                    scope.scope_user_id,
                    scope.persona_id,
                    visibility,
                    kind,
                    text,
                    source,
                    source_ref,
                    node_id,
                    risk,
                    confidence,
                    importance,
                    trust,
                    valid_from_ms if valid_from_ms is not None else now,
                    now,
                ),
            )
            await self._conn.commit()
        return item_id

    async def invalidate_item(
        self, item_id: str, *, reason: str, by: str | None = None
    ) -> bool:
        """Bi-temporal retirement — sets ``valid_to_ms``, never deletes.

        Returns ``False`` when the item is unknown or already invalid
        (idempotent; a double-invalidate keeps the first cause).
        """
        async with self._lock:
            cur = await self._conn.execute(
                "UPDATE mk_items SET valid_to_ms = ?, invalidated_by = ?,"
                " invalid_reason = ? WHERE id = ? AND valid_to_ms IS NULL",
                (now_ms(), by, reason, item_id),
            )
            await self._conn.commit()
            return cur.rowcount > 0

    # ---- READ pipeline ----------------------------------------------------

    async def recall(
        self, scope: KernelScope, text: str, *, top_k: int = 8
    ) -> list[MemoryItem]:
        """Scoped BM25 recall over currently-valid items.

        Scope filter: same tenant; item's ``scope_user_id`` matches or is
        NULL (agent-scoped memory is visible to everyone in the tenant);
        item's ``persona_id`` matches or is ``''`` (persona-shared).
        Cross-persona ``mk_scope_grants`` and visibility tiers land in W2.

        Queries whose every unit is shorter than 3 chars (two-char CJK
        words like 家乡/名字, "hi"/"ok") produce no trigram token, so
        they fall back to a scope-filtered LIKE scan — bounded by the
        per-scope item count, which is small by design.
        """
        if top_k <= 0:
            return []
        units = _query_units(text)
        match = _trigram_match_query(units)
        if match:
            sql = (
                "SELECT i.*, bm25(mk_items_fts) AS fts_score"
                " FROM mk_items_fts f JOIN mk_items i ON i.rowid = f.rowid"
                " WHERE mk_items_fts MATCH ?"
                "   AND i.tenant_id = ?"
                "   AND (i.scope_user_id IS NULL OR i.scope_user_id = ?)"
                "   AND (i.persona_id = '' OR i.persona_id = ?)"
                "   AND i.valid_to_ms IS NULL"
                " ORDER BY fts_score ASC LIMIT ?"
            )
            params: tuple[Any, ...] = (
                match,
                scope.tenant_id,
                scope.scope_user_id,
                scope.persona_id,
                top_k,
            )
            try:
                async with self._conn.execute(sql, params) as cur:
                    rows = await cur.fetchall()
            except aiosqlite.OperationalError as exc:
                if "fts5" in str(exc).lower() or "malformed" in str(exc).lower():
                    return []
                raise
            return [self._row_to_item(row) for row in rows]

        # LIKE fallback for short-unit-only queries (newest-first; no BM25
        # score is available so hits carry score=0.0).
        like_units = [u for u in units if u][:4]
        if not like_units:
            return []
        like_clause = " OR ".join(
            "i.text LIKE ? ESCAPE '\\'" for _ in like_units
        )
        sql = (
            "SELECT i.*, 0.0 AS fts_score FROM mk_items i"
            " WHERE i.tenant_id = ?"
            "   AND (i.scope_user_id IS NULL OR i.scope_user_id = ?)"
            "   AND (i.persona_id = '' OR i.persona_id = ?)"
            "   AND i.valid_to_ms IS NULL"
            f"   AND ({like_clause})"
            " ORDER BY i.recorded_at_ms DESC LIMIT ?"
        )
        params = (
            scope.tenant_id,
            scope.scope_user_id,
            scope.persona_id,
            *(f"%{_escape_like(u)}%" for u in like_units),
            top_k,
        )
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [self._row_to_item(row) for row in rows]

    # ---- MAINTENANCE surface (drained by later waves) ----------------------

    async def pending_observations(
        self, *, tenant_id: str = "default", limit: int = 200
    ) -> list[Observation]:
        async with self._conn.execute(
            "SELECT * FROM mk_observations"
            " WHERE tenant_id = ? AND processed_at_ms IS NULL"
            " ORDER BY ts_ms ASC LIMIT ?",
            (tenant_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        out: list[Observation] = []
        for row in rows:
            obs = Observation(
                session_key=row["session_key"],
                user_text=row["user_text"],
                reply_text=row["reply_text"],
                ts_ms=int(row["ts_ms"]),
                tenant_id=row["tenant_id"],
                channel=row["channel"],
                channel_user_id=row["channel_user_id"],
                scope_user_id=row["scope_user_id"],
                persona_id=row["persona_id"],
                id=row["id"],
            )
            out.append(obs)
        return out

    async def mark_observations_processed(self, obs_ids: list[str]) -> None:
        if not obs_ids:
            return
        placeholders = ",".join("?" * len(obs_ids))
        async with self._lock:
            await self._conn.execute(
                f"UPDATE mk_observations SET processed_at_ms = ?"
                f" WHERE id IN ({placeholders})",
                (now_ms(), *obs_ids),
            )
            await self._conn.commit()

    async def stats(self) -> dict[str, int]:
        """Row counts for telemetry / the shadow-mode diff log."""
        out: dict[str, int] = {}
        for key, sql in (
            ("items", "SELECT COUNT(*) FROM mk_items WHERE valid_to_ms IS NULL"),
            ("items_invalidated", "SELECT COUNT(*) FROM mk_items WHERE valid_to_ms IS NOT NULL"),
            ("observations_pending", "SELECT COUNT(*) FROM mk_observations WHERE processed_at_ms IS NULL"),
            ("observations_total", "SELECT COUNT(*) FROM mk_observations"),
        ):
            async with self._conn.execute(sql) as cur:
                row = await cur.fetchone()
            out[key] = int(row[0]) if row is not None else 0
        return out

    # ---- helpers ------------------------------------------------------------

    @staticmethod
    def _row_to_item(row: Any) -> MemoryItem:
        return MemoryItem(
            id=row["id"],
            text=row["text"],
            kind=row["kind"],
            source=row["source"],
            scope=KernelScope(
                tenant_id=row["tenant_id"],
                scope_user_id=row["scope_user_id"],
                persona_id=row["persona_id"],
            ),
            visibility=row["visibility"],
            risk=row["risk"],
            confidence=float(row["confidence"]),
            importance=float(row["importance"]),
            trust=float(row["trust"]),
            utility=float(row["utility"]),
            valid_from_ms=int(row["valid_from_ms"]),
            valid_to_ms=(
                int(row["valid_to_ms"]) if row["valid_to_ms"] is not None else None
            ),
            recorded_at_ms=int(row["recorded_at_ms"]),
            # bm25() is lower-is-better; flip to the higher-is-better
            # contract the rest of the codebase uses.
            score=-float(row["fts_score"]),
        )
