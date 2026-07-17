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
import math
import os
from pathlib import Path
from typing import Any

import aiosqlite

from corlinman_memory_kernel.ids import new_id, now_ms
from corlinman_memory_kernel.schema import MK_SCHEMA_SQL
from corlinman_memory_kernel.types import (
    KernelScope,
    LedgerEntry,
    MemoryItem,
    Observation,
)

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
        self,
        scope: KernelScope,
        text: str,
        *,
        top_k: int = 8,
        exclude_high_risk: bool = False,
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
        risk_pred = " AND i.risk != 'high'" if exclude_high_risk else ""
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
                f"{risk_pred}"
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
            f"{risk_pred}"
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

    async def recall_ranked(
        self,
        scope: KernelScope,
        text: str,
        *,
        top_k: int = 4,
        candidates: int = 32,
        weights: dict[str, float] | None = None,
        query_vector: list[float] | None = None,
        mood: tuple[float, float, float] | None = None,
    ) -> list[MemoryItem]:
        """Ranked recall: hybrid candidate fetch + unified re-rank.

        Candidates come from the scoped FTS branch (and, when a
        ``query_vector`` is supplied and items carry embeddings, a
        cosine branch RRF-merged with it). Final ordering is the
        generative-agents-style blend::

            score = w_rel·rrf + w_rec·exp(-age/τ) + w_imp·importance
                    + w_tr·(trust·utility) + w_aff·resonance(mood, item)

        The affect term (W6) only engages when a ``mood`` is supplied
        AND ``w_aff`` > 0 AND the item carries a stamped affect vector —
        salience gates it, and the mood-repair bias inside
        :func:`corlinman_memory_kernel.affect.resonance` prevents
        negative-mood spirals. ``risk='high'`` items are excluded
        entirely — channel-level provenance that would let them surface
        safely lands with the W5 reconcile pipeline.
        """
        w = {
            "w_rel": 1.0,
            "w_rec": 0.3,
            "w_imp": 0.3,
            "w_tr": 0.2,
            "w_aff": 0.0,
            "half_life_days": 30.0,
        }
        if weights:
            for key, value in weights.items():
                # bool is an int subclass — reject it like the config
                # sanitisers do (TOML `true` must not become weight 1.0).
                if (
                    key in w
                    and isinstance(value, (int, float))
                    and not isinstance(value, bool)
                ):
                    w[key] = float(value)

        # Risk filtering happens IN the candidate queries so high-risk
        # rows can't starve the candidate pool for legitimate matches.
        fts_hits = await self.recall(
            scope, text, top_k=candidates, exclude_high_risk=True
        )
        # RRF rank positions per branch (1-indexed).
        rrf: dict[str, float] = {}
        by_id: dict[str, MemoryItem] = {}
        for rank, item in enumerate(fts_hits, start=1):
            rrf[item.id] = rrf.get(item.id, 0.0) + 1.0 / (60.0 + rank)
            by_id[item.id] = item
        if query_vector:
            vec_ranked = await self._vector_candidates(
                scope, query_vector, candidates
            )
            for rank, item in enumerate(vec_ranked, start=1):
                if item.risk == "high":
                    continue
                rrf[item.id] = rrf.get(item.id, 0.0) + 1.0 / (60.0 + rank)
                by_id.setdefault(item.id, item)
        if not by_id:
            return []

        now = now_ms()
        max_rrf = max(rrf.values())
        affect_on = mood is not None and w["w_aff"] > 0.0
        if affect_on:
            from corlinman_memory_kernel.affect import resonance

        scored: list[tuple[float, MemoryItem]] = []
        for item_id, item in by_id.items():
            age_days = max(0.0, (now - item.recorded_at_ms) / 86_400_000.0)
            recency = math.exp(-age_days / max(w["half_life_days"], 0.01))
            score = (
                w["w_rel"] * (rrf[item_id] / max_rrf)
                + w["w_rec"] * recency
                + w["w_imp"] * item.importance
                + w["w_tr"] * (item.trust * item.utility)
            )
            if affect_on:
                assert mood is not None  # narrowed by affect_on
                score += w["w_aff"] * resonance(
                    mood,
                    (
                        item.affect_e,
                        item.affect_p,
                        item.affect_a,
                        item.affect_salience,
                    ),
                )
            item.score = score
            scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for (_, item) in scored[:top_k]]

    async def _vector_candidates(
        self, scope: KernelScope, query_vector: list[float], top_k: int
    ) -> list[MemoryItem]:
        """Cosine branch over the scope's embedded, currently-valid items."""
        from corlinman_memory_kernel.vector import cosine_topk

        sql = (
            "SELECT id, embedding FROM mk_items"
            " WHERE embedding IS NOT NULL AND tenant_id = ?"
            "   AND (scope_user_id IS NULL OR scope_user_id = ?)"
            "   AND (persona_id = '' OR persona_id = ?)"
            "   AND valid_to_ms IS NULL"
        )
        async with self._conn.execute(
            sql, (scope.tenant_id, scope.scope_user_id, scope.persona_id)
        ) as cur:
            rows = await cur.fetchall()
        ranked = cosine_topk(
            query_vector,
            [(row["id"], row["embedding"]) for row in rows],
            top_k,
        )
        if not ranked:
            return []
        ids = [item_id for (item_id, _) in ranked]
        placeholders = ",".join("?" * len(ids))
        async with self._conn.execute(
            f"SELECT *, 0.0 AS fts_score FROM mk_items WHERE id IN ({placeholders})",
            ids,
        ) as cur:
            rows = await cur.fetchall()
        items = {row["id"]: self._row_to_item(row) for row in rows}
        return [items[item_id] for item_id in ids if item_id in items]

    async def record_injection(
        self, turn_key: str, entries: list[LedgerEntry]
    ) -> None:
        """Injection bookkeeping in ONE transaction: mk_recall_ledger rows
        plus recall_count/last_recalled_ms bumps on the injected items.
        Atomic so the trust loop's shown-vs-recalled ratio can't skew when
        one half fails."""
        if not entries:
            return
        ts = now_ms()
        placeholders = ",".join("?" * len(entries))
        async with self._lock:
            await self._conn.executemany(
                "INSERT INTO mk_recall_ledger("
                "turn_key, item_id, lane, rank, score, shown_chars, ts_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        turn_key,
                        e.item_id,
                        e.lane,
                        e.rank,
                        e.score,
                        e.shown_chars,
                        ts,
                    )
                    for e in entries
                ],
            )
            await self._conn.execute(
                f"UPDATE mk_items SET recall_count = recall_count + 1,"
                f" last_recalled_ms = ? WHERE id IN ({placeholders})",
                (ts, *[e.item_id for e in entries]),
            )
            await self._conn.commit()

    async def core_blocks(self, scope: KernelScope) -> list[tuple[str, str]]:
        """The scope's core-memory blocks as (block, content), stable order.

        Written by the W5 maintenance pipeline; empty until then. The
        caller renders them verbatim at a stable prompt position so the
        bytes stay prefix-cache-friendly across turns. Persona scoping
        matches the recall convention: shared (``persona_id=''``) blocks
        are visible to every persona, and a persona-specific block wins
        over a shared block of the same name.
        """
        async with self._conn.execute(
            "SELECT block, content, persona_id FROM mk_core"
            " WHERE tenant_id = ? AND scope_user_id = ?"
            "   AND (persona_id = '' OR persona_id = ?)"
            " ORDER BY block, persona_id",
            (scope.tenant_id, scope.scope_user_id or "", scope.persona_id),
        ) as cur:
            rows = await cur.fetchall()
        # ORDER BY persona_id puts '' first; the later (persona-specific)
        # row overwrites the shared one for the same block name.
        merged: dict[str, str] = {}
        for row in rows:
            merged[row["block"]] = row["content"]
        return sorted(merged.items())

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

    async def set_affect(
        self, item_id: str, e: float, p: float, a: float, salience: float
    ) -> None:
        """Stamp the EPA affect vector on an item (W6 affect lens)."""
        async with self._lock:
            await self._conn.execute(
                "UPDATE mk_items SET affect_e = ?, affect_p = ?,"
                " affect_a = ?, affect_salience = ? WHERE id = ?",
                (e, p, a, salience, item_id),
            )
            await self._conn.commit()

    async def get_affect_state(
        self, persona_id: str
    ) -> tuple[float, float, float]:
        """The persona's current mood in EPA space (0,0,0 when unset)."""
        async with self._conn.execute(
            "SELECT mood_e, mood_p, mood_a FROM mk_affect_state"
            " WHERE persona_id = ?",
            (persona_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return (0.0, 0.0, 0.0)
        return (float(row["mood_e"]), float(row["mood_p"]), float(row["mood_a"]))

    async def update_affect_state(
        self,
        persona_id: str,
        turn_affect: tuple[float, float, float],
        *,
        alpha: float = 0.1,
    ) -> tuple[float, float, float]:
        """EMA mood update: ``mood ← (1-α)·mood + α·turn_affect``.

        Slow by design — one emotional message nudges the persona's
        mood, it doesn't yank it. Returns the new mood.
        """
        current = await self.get_affect_state(persona_id)
        new = tuple(
            (1.0 - alpha) * c + alpha * t
            for c, t in zip(current, turn_affect, strict=True)
        )
        async with self._lock:
            await self._conn.execute(
                "INSERT INTO mk_affect_state("
                "persona_id, mood_e, mood_p, mood_a, updated_at_ms)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(persona_id) DO UPDATE SET"
                " mood_e = excluded.mood_e, mood_p = excluded.mood_p,"
                " mood_a = excluded.mood_a,"
                " updated_at_ms = excluded.updated_at_ms",
                (persona_id, new[0], new[1], new[2], now_ms()),
            )
            await self._conn.commit()
        return (new[0], new[1], new[2])

    async def add_edge(
        self, src_id: str, dst_id: str, rel: str, *, weight: float = 1.0
    ) -> None:
        """Record a typed edge (supports/contradicts/refines/derived_from)."""
        async with self._lock:
            await self._conn.execute(
                "INSERT OR REPLACE INTO mk_edges("
                "src_id, dst_id, rel, weight, created_at_ms)"
                " VALUES (?, ?, ?, ?, ?)",
                (src_id, dst_id, rel, weight, now_ms()),
            )
            await self._conn.commit()

    async def set_embedding(
        self, item_id: str, vector: list[float]
    ) -> None:
        """Stamp an embedding on an item (feeds the vector recall branch)."""
        from corlinman_memory_kernel.vector import encode_f32

        async with self._lock:
            await self._conn.execute(
                "UPDATE mk_items SET embedding = ?, embedding_dim = ?"
                " WHERE id = ?",
                (encode_f32(vector), len(vector), item_id),
            )
            await self._conn.commit()

    async def top_items_for_scope(
        self,
        scope: KernelScope,
        *,
        kinds: tuple[str, ...] = ("preference", "fact"),
        limit: int = 8,
    ) -> list[MemoryItem]:
        """Highest trust×importance valid items for a scope (core-block
        source). NULL-scope (agent-scoped) handled like every other
        scoped read — ``scope_user_id IS NULL`` matches, not ``= NULL``.
        """
        kind_ph = ",".join("?" * len(kinds))
        if scope.scope_user_id is None:
            user_pred = "scope_user_id IS NULL"
            params: tuple[Any, ...] = (scope.tenant_id, *kinds, scope.persona_id, limit)
        else:
            user_pred = "scope_user_id = ?"
            params = (
                scope.tenant_id,
                scope.scope_user_id,
                *kinds,
                scope.persona_id,
                limit,
            )
        sql = (
            "SELECT *, 0.0 AS fts_score FROM mk_items"
            f" WHERE tenant_id = ? AND {user_pred}"
            f"   AND kind IN ({kind_ph})"
            "   AND (persona_id = '' OR persona_id = ?)"
            "   AND valid_to_ms IS NULL"
            " ORDER BY (trust * importance) DESC, recorded_at_ms DESC LIMIT ?"
        )
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [self._row_to_item(row) for row in rows]

    async def set_core_block(
        self, scope: KernelScope, block: str, content: str
    ) -> None:
        """Upsert one core-memory block for a scope (maintenance writer).

        Content is rendered by the maintenance pipeline; recall injects
        it verbatim at a stable prompt position, so rebuilds should only
        write when the content actually changed (the caller compares) to
        keep the bytes prefix-cache-stable.
        """
        async with self._lock:
            await self._conn.execute(
                "INSERT INTO mk_core("
                "tenant_id, scope_user_id, persona_id, block, content,"
                " updated_at_ms) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(tenant_id, scope_user_id, persona_id, block)"
                " DO UPDATE SET content = excluded.content,"
                " updated_at_ms = excluded.updated_at_ms",
                (
                    scope.tenant_id,
                    scope.scope_user_id or "",
                    scope.persona_id,
                    block,
                    content,
                    now_ms(),
                ),
            )
            await self._conn.commit()

    async def merge_scope_user(self, from_user: str, into_user: str) -> int:
        """Re-stamp every row of a merged identity onto the survivor.

        Called after an operator ``merge_users``; returns the number of
        rows moved. Bi-temporal history is preserved as-is — only the
        scope key changes.
        """
        if not from_user or not into_user or from_user == into_user:
            return 0
        moved = 0
        async with self._lock:
            for table in ("mk_items", "mk_observations"):
                cur = await self._conn.execute(
                    f"UPDATE {table} SET scope_user_id = ?"
                    " WHERE scope_user_id = ?",
                    (into_user, from_user),
                )
                moved += cur.rowcount
            # mk_core is PK'd on (tenant, scope_user, persona, block):
            # where the survivor already has the same block, theirs wins
            # and the loser's row is dropped; the rest are re-stamped.
            await self._conn.execute(
                "DELETE FROM mk_core WHERE scope_user_id = ? AND EXISTS ("
                " SELECT 1 FROM mk_core c2 WHERE c2.tenant_id = mk_core.tenant_id"
                " AND c2.scope_user_id = ? AND c2.persona_id = mk_core.persona_id"
                " AND c2.block = mk_core.block)",
                (from_user, into_user),
            )
            cur = await self._conn.execute(
                "UPDATE mk_core SET scope_user_id = ? WHERE scope_user_id = ?",
                (into_user, from_user),
            )
            moved += cur.rowcount
            await self._conn.commit()
        return moved

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
            affect_e=float(row["affect_e"] or 0.0),
            affect_p=float(row["affect_p"] or 0.0),
            affect_a=float(row["affect_a"] or 0.0),
            affect_salience=float(row["affect_salience"] or 0.0),
            valid_from_ms=int(row["valid_from_ms"]),
            valid_to_ms=(
                int(row["valid_to_ms"]) if row["valid_to_ms"] is not None else None
            ),
            recorded_at_ms=int(row["recorded_at_ms"]),
            # bm25() is lower-is-better; flip to the higher-is-better
            # contract the rest of the codebase uses.
            score=-float(row["fts_score"]),
        )
