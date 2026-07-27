"""Redis backend for :class:`corlinman_server.agent_journal.AgentJournal`.

Low-latency shared journal for multi-gateway HA: N ``corlinman-server``
processes point ``CORLINMAN_JOURNAL_REDIS_URL`` at one Redis and a turn
started on gateway A can be resumed on gateway B after A dies. It is the
ephemeral-state sibling of
:class:`~corlinman_server.agent_journal_postgres.PostgresJournalBackend`
— same :class:`~corlinman_server.agent_journal_backend.JournalBackend`
Protocol, same method-by-method semantics, different durability posture
(Redis persistence is whatever the operator configured; RDB/AOF are out
of this module's hands).

**Data model — sorted sets + lists, not Streams.**

The roadmap allowed either Redis Streams or sorted sets for the event
flow. Sorted sets (plus plain lists for the per-turn replay buffer) fit
the Protocol's read paths better, so that is what ships:

- Every ordered read the Protocol asks for is either a *time-range scan*
  (``find_resumable_turn`` / ``list_resumable_in_progress`` /
  ``mark_stale_in_progress_as_errored`` / ``query_messages`` — all
  keyed on ``started_at_ms`` windows) or a *snapshot replay in seq
  order* (``load_messages``). ``ZRANGEBYSCORE`` with
  ``score = started_at_ms`` answers the former directly; ``RPUSH`` /
  ``LRANGE`` gives a dense, ordered, implicitly-numbered replay buffer
  for the latter.
- Streams would force their auto-generated ``ms-seq`` entry IDs on us,
  which neither match the journal's integer ``seq`` addressing nor its
  ``(started_at_ms, turn_id)`` ordering — we would carry the same zset
  bookkeeping *next to* the stream. And Streams' consumer-group
  delivery semantics have no consumer here: every journal read is a
  point-in-time replay, not a fan-out subscription.

Key layout (all under the ``corlinman:journal:`` prefix):

- ``turn_seq`` — ``INCR`` counter allocating ``turn_id`` (the Redis
  analogue of Postgres' ``BIGSERIAL``; monotone, never reused).
- ``turn:{id}`` — hash holding the ``turns``-row fields (status,
  timestamps, user/channel/tenant stamps, cost columns…). ``None``
  columns are simply absent fields.
- ``turn:{id}:messages`` — list of JSON message docs; list index IS the
  ``seq`` the SQL backends store explicitly.
- ``turns`` / ``in_progress`` — global zsets over ``turn_id`` scored by
  ``started_at_ms`` (all turns, resp. the in-progress subset).
- ``session:{key}:turns`` / ``session:{key}:errored`` — per-session
  zsets scored by ``started_at_ms``.
- ``sessions`` — zset of session_keys (recency-scored; used as the
  enumeration set for the summaries listing).
- ``session_meta:{key}`` — hash for operator title/pinned/archived.
- ``open_turn:{sha256(tuple)}`` — the C5 begin-race guard, see below.

**Concurrency.** ``begin_turn`` mirrors Postgres' C5 partial unique
index with ``SET NX PX``: the first gateway to claim the
``(session_key, user_text, user_id, channel, runtime_instance_id)``
tuple wins; the loser gets ``None`` back and the chat handler falls
back to ``find_resumable_turn``. The claim key expires after
:data:`~corlinman_server.agent_journal_backend.RESUME_MAX_AGE_MS` —
past the resume window a duplicate row is harmless because the old turn
is no longer resumable anyway (this bounded-TTL release stands in for
Postgres' "constraint holds while status is in_progress" and guarantees
a crashed gateway can never wedge a session for more than the window).
Status flips (``complete_turn`` / ``error_turn`` / the stale sweep) use
``WATCH``-based optimistic transactions so exactly one terminal status
wins, mirroring the SQL backends' ``UPDATE … WHERE status =
'in_progress'`` CAS.

Ordering parity: everywhere the SQL backends order by
``(started_at_ms, turn_id)`` this backend sorts the same composite in
Python after the zset range read — ``turn_id`` is INCR-monotone, so the
tie-break has the same "later insert wins" meaning as in SQL.

The W1.2 turn-events timeline is stubbed exactly like the Postgres
backend (SQLite remains the source of truth for SSE replay); the facade
degrades gracefully to an empty timeline.

redis-py is an optional extra; install with::

    pip install 'corlinman-server[redis]'

If it is missing when :meth:`RedisJournalBackend.open` is called we
raise a clear ``RuntimeError`` rather than an uncontextualised
``ImportError`` at gateway startup.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import time
from collections.abc import AsyncIterator, Sequence
from typing import Any

import structlog

from corlinman_server.agent_journal_backend import (
    DEFAULT_TENANT_ID,
    RESUME_MAX_AGE_MS,
    SESSION_SUMMARY_PREVIEW_LEN,
    TURN_COMPLETED,
    TURN_ERRORED,
    TURN_IN_PROGRESS,
    InProgressTurn,
    ResumeData,
    SessionSummary,
)

logger = structlog.get_logger(__name__)

# Every key this backend touches lives under this prefix so operators
# can inspect (or wipe) the journal with one SCAN pattern and so a
# shared Redis instance doesn't collide with other corlinman state.
_KEY_PREFIX = "corlinman:journal:"

# Bounded retries for the WATCH-based CAS loops. Contention on a single
# turn key is rare (the per-session servicer lock serialises the common
# case); this only guards the cross-gateway race, so a handful of
# retries is plenty before we log and give up (best-effort, matching
# the SQL backends' warn-and-continue posture).
_CAS_MAX_RETRIES = 8

# Error text the stale sweep stamps on abandoned rows — kept identical
# to the Postgres backend so operator dashboards match across backends.
_ABANDONED_ERROR = "abandoned: gateway restart left turn in_progress"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _tenant_matches(row_tenant: str, tenant_id: str) -> bool:
    """Python twin of the SQL backends' tenant guard: a row matches when
    its tenant equals the requested one, or when it is a legacy ``''``
    row and the requested tenant IS the default tenant."""
    return row_tenant == tenant_id or (row_tenant == "" and tenant_id == DEFAULT_TENANT_ID)


def _int_field(row: dict[str, str], field: str) -> int | None:
    value = row.get(field)
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _float_field(row: dict[str, str], field: str) -> float | None:
    value = row.get(field)
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


class RedisJournalBackend:
    """Redis-backed journal for shared / multi-gateway deployments.

    Implements the :class:`JournalBackend` Protocol verbatim — every
    method matches the SQLite and Postgres backends' signature, return
    shape, and exception-swallowing posture. See the module docstring
    for the key layout and the Streams-vs-zset tradeoff.
    """

    __slots__ = ("_client", "_url", "_watch_error_cls")

    def __init__(self, url: str) -> None:
        self._url = url
        # Concrete type is ``redis.asyncio.Redis`` but we keep ``Any``
        # so the module imports cleanly when redis-py is not installed.
        self._client: Any = None
        self._watch_error_cls: type[BaseException] = Exception

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @classmethod
    async def open(cls, url: str, *, client: Any | None = None) -> RedisJournalBackend:
        """Connect to ``url`` and verify the server answers ``PING``.

        Raises ``RuntimeError`` if redis-py is not installed — the
        import is deferred to here so the module stays importable in
        environments that never select the Redis backend (mirrors the
        Postgres backend's asyncpg posture).

        ``client`` is a test seam: pass a pre-built async Redis client
        (e.g. ``fakeredis.aioredis.FakeRedis(decode_responses=True)``)
        to skip the URL connection. The client MUST be created with
        ``decode_responses=True`` — every read below expects ``str``.
        """
        if importlib.util.find_spec("redis") is None:
            raise RuntimeError(
                "redis backend selected but the redis client is not installed; "
                "pip install corlinman-server[redis]"
            )
        backend = cls(url)
        await backend._open(client)
        return backend

    async def _open(self, client: Any | None) -> None:
        from redis import exceptions as redis_exceptions

        # Stashed so the CAS loops can catch the precise WatchError even
        # though the module never imports redis at top level.
        self._watch_error_cls = redis_exceptions.WatchError
        if client is None:
            from redis.asyncio import Redis

            client = Redis.from_url(
                self._url,
                decode_responses=True,
                socket_connect_timeout=5.0,
            )
        self._client = client
        # Fail loudly at startup on a bad URL / unreachable server —
        # the whole point of the env selector's "no silent fallback"
        # contract. (Postgres gets the same guarantee from pool creation.)
        await self._client.ping()

    async def close(self) -> None:
        """Close the underlying client. Idempotent."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def _r(self) -> Any:
        if self._client is None:
            raise RuntimeError("RedisJournalBackend not opened — call open() first")
        return self._client

    # ------------------------------------------------------------------
    # Key helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _k(suffix: str) -> str:
        return _KEY_PREFIX + suffix

    def _turn_key(self, turn_id: int | str) -> str:
        return self._k(f"turn:{int(turn_id)}")

    def _messages_key(self, turn_id: int | str) -> str:
        return self._k(f"turn:{int(turn_id)}:messages")

    def _session_turns_key(self, session_key: str) -> str:
        return self._k(f"session:{session_key}:turns")

    def _session_errored_key(self, session_key: str) -> str:
        return self._k(f"session:{session_key}:errored")

    def _meta_key(self, session_key: str) -> str:
        return self._k(f"session_meta:{session_key}")

    def _open_turn_key(
        self,
        session_key: str,
        user_text: str,
        user_id: str | None,
        channel: str,
        runtime_instance_id: str,
    ) -> str:
        """C5 claim key for one in-progress tuple.

        The tuple is hashed (sha256) so an arbitrarily long user_text
        can't blow up the key size; ``user_id=None`` and ``user_id=''``
        hash differently on purpose — NULL is a distinct legacy value in
        the SQL backends' partial index (``COALESCE(user_id, '')``
        collapses them there, but the resume matcher treats NULL as
        wildcard, so keeping them distinct here is the conservative
        choice that can only admit an extra row, never block one).
        """
        raw = "\x00".join(
            (
                session_key,
                user_text,
                "\x01" if user_id is None else user_id,
                channel,
                runtime_instance_id,
            )
        )
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return self._k(f"open_turn:{digest}")

    async def _hgetall_many(self, keys: list[str]) -> list[dict[str, str]]:
        """Pipelined HGETALL over ``keys`` (one round trip)."""
        if not keys:
            return []
        async with self._r.pipeline(transaction=False) as pipe:
            for key in keys:
                pipe.hgetall(key)
            rows = await pipe.execute()
        return [dict(row) if row else {} for row in rows]

    # ------------------------------------------------------------------
    # Turn lifecycle
    # ------------------------------------------------------------------

    async def begin_turn(
        self,
        session_key: str,
        user_text: str,
        *,
        user_id: str | None = None,
        channel: str = "",
        runtime_instance_id: str = "",
        tenant_id: str = "",
        pending_question_json: str | None = None,
    ) -> int | None:
        """Insert an in-progress row; return the new ``turn_id``.

        ``INCR turn_seq`` allocates the id (monotone across every
        gateway sharing the Redis, so two concurrent writers never
        collide — same guarantee BIGSERIAL gives the Postgres backend).

        C5: the ``SET NX PX`` claim on the open-turn tuple makes the
        insert race-safe across HA gateways — the loser returns ``None``
        and the chat handler re-runs ``find_resumable_turn`` to join the
        winner's row. The claim is released on complete/error/sweep and
        auto-expires with the resume window, so a crashed gateway can
        never block the tuple for longer than the window.

        S4 / auto-resume / ask_user field semantics are identical to the
        SQLite and Postgres backends — see their docstrings.
        """
        ts = _now_ms()
        r = self._r
        turn_id = int(await r.incr(self._k("turn_seq")))
        member = str(turn_id)
        open_key = self._open_turn_key(
            session_key or "",
            user_text,
            user_id,
            channel or "",
            runtime_instance_id or "",
        )
        claimed = await r.set(open_key, member, nx=True, px=RESUME_MAX_AGE_MS)
        if not claimed:
            # Another gateway holds the in-progress claim for this exact
            # tuple. (The burnt INCR id is fine — BIGSERIAL burns ids on
            # conflicting inserts too.)
            logger.info(
                "agent.journal.begin_turn_conflict",
                session_key=session_key,
                user_id=user_id,
            )
            return None
        mapping: dict[str, str] = {
            "session_key": session_key or "",
            "status": TURN_IN_PROGRESS,
            "started_at_ms": str(ts),
            "user_text": user_text,
            "channel": channel or "",
            "runtime_instance_id": runtime_instance_id or "",
            "tenant_id": tenant_id or "",
            "tool_call_count": "0",
            "reasoning_token_count": "0",
            # Stored so terminalisation can release the claim without
            # re-deriving the tuple (user_id=None wouldn't round-trip).
            "open_key": open_key,
        }
        if user_id is not None:
            mapping["user_id"] = user_id
        if pending_question_json is not None:
            mapping["pending_question_json"] = pending_question_json
        async with r.pipeline(transaction=True) as pipe:
            pipe.hset(self._turn_key(turn_id), mapping=mapping)
            pipe.zadd(self._k("turns"), {member: ts})
            pipe.zadd(self._k("in_progress"), {member: ts})
            pipe.zadd(self._session_turns_key(session_key or ""), {member: ts})
            pipe.zadd(self._k("sessions"), {(session_key or ""): ts})
            await pipe.execute()
        return turn_id

    async def complete_turn(self, turn_id: int) -> None:
        """CAS-flip ``turn_id`` to completed and fold the aggregates.

        Mirrors the Postgres backend: ``elapsed_ms`` and
        ``tool_call_count`` are computed at completion time;
        ``reasoning_token_count`` stays 0 (no event timeline on this
        backend) and cost columns are left to :meth:`update_turn_cost`.
        The WATCH transaction guarantees exactly one terminal status
        wins against a racing ``error_turn`` on another gateway.
        """
        turn_key = self._turn_key(turn_id)
        try:
            async with self._r.pipeline(transaction=True) as pipe:
                for _ in range(_CAS_MAX_RETRIES):
                    try:
                        await pipe.watch(turn_key)
                        row = await pipe.hgetall(turn_key)
                        if not row or row.get("status") != TURN_IN_PROGRESS:
                            await pipe.unwatch()
                            return
                        ended_at_ms = _now_ms()
                        started_at_ms = _int_field(row, "started_at_ms")
                        # Immediate-mode read; message appends racing this
                        # count is the same benign race the SQLite backend's
                        # two-step aggregate write accepts.
                        docs = await pipe.lrange(self._messages_key(turn_id), 0, -1)
                        tool_call_count = sum(1 for d in docs if _doc_role(d) == "tool")
                        mapping: dict[str, str] = {
                            "status": TURN_COMPLETED,
                            "ended_at_ms": str(ended_at_ms),
                            "tool_call_count": str(tool_call_count),
                        }
                        if started_at_ms is not None:
                            mapping["elapsed_ms"] = str(max(0, ended_at_ms - started_at_ms))
                        open_key = row.get("open_key")
                        pipe.multi()
                        pipe.hset(turn_key, mapping=mapping)
                        pipe.zrem(self._k("in_progress"), str(int(turn_id)))
                        if open_key:
                            pipe.delete(open_key)
                        await pipe.execute()
                        return
                    except self._watch_error_cls:
                        continue
            logger.warning(
                "agent.journal.complete_failed",
                error=f"CAS contention on turn {turn_id} after {_CAS_MAX_RETRIES} retries",
            )
        except Exception as exc:
            logger.warning("agent.journal.complete_failed", error=str(exc))

    async def _terminalize_errored(
        self,
        turn_id: int,
        error: str,
        *,
        keep_existing_error: bool,
    ) -> bool:
        """CAS-flip one in-progress turn to errored. Returns ``True``
        iff this call performed the flip.

        ``keep_existing_error`` mirrors the sweep's ``COALESCE(error,
        …)`` — an already-recorded error survives; ``error_turn`` passes
        ``False`` to overwrite unconditionally (its SQL peer writes the
        column outright).
        """
        turn_key = self._turn_key(turn_id)
        async with self._r.pipeline(transaction=True) as pipe:
            for _ in range(_CAS_MAX_RETRIES):
                try:
                    await pipe.watch(turn_key)
                    row = await pipe.hgetall(turn_key)
                    if not row or row.get("status") != TURN_IN_PROGRESS:
                        await pipe.unwatch()
                        return False
                    mapping: dict[str, str] = {
                        "status": TURN_ERRORED,
                        "ended_at_ms": str(_now_ms()),
                    }
                    if not (keep_existing_error and row.get("error")):
                        mapping["error"] = error[:1000]
                    session_key = row.get("session_key", "")
                    started_at_ms = _int_field(row, "started_at_ms") or 0
                    open_key = row.get("open_key")
                    pipe.multi()
                    pipe.hset(turn_key, mapping=mapping)
                    pipe.zrem(self._k("in_progress"), str(int(turn_id)))
                    pipe.zadd(
                        self._session_errored_key(session_key),
                        {str(int(turn_id)): started_at_ms},
                    )
                    if open_key:
                        pipe.delete(open_key)
                    await pipe.execute()
                    return True
                except self._watch_error_cls:
                    continue
        logger.warning(
            "agent.journal.error_failed",
            error=f"CAS contention on turn {turn_id} after {_CAS_MAX_RETRIES} retries",
        )
        return False

    async def error_turn(self, turn_id: int, error: str) -> None:
        try:
            await self._terminalize_errored(turn_id, error, keep_existing_error=False)
        except Exception as exc:
            logger.warning("agent.journal.error_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Message append
    # ------------------------------------------------------------------

    def _prepare_message_doc(
        self,
        role: str,
        content: Any,
        tool_call_id: Any | None,
        tool_calls: Any | None,
        attachments: Any | None,
    ) -> str | None:
        """Serialise one message into the JSON doc the replay list holds.

        Error posture mirrors the SQL backends: an unserialisable
        ``tool_calls`` drops the whole message (``None`` return); an
        unserialisable ``attachments`` drops only the attachment
        metadata and keeps the message.
        """
        if not isinstance(content, str):
            content = str(content)
        msg: dict[str, Any] = {"role": role, "content": content}
        if tool_call_id is not None:
            msg["tool_call_id"] = str(tool_call_id)
        if tool_calls is not None:
            try:
                json.dumps(tool_calls)
            except (TypeError, ValueError) as exc:
                logger.warning("agent.journal.append_serialize_failed", error=str(exc))
                return None
            msg["tool_calls"] = tool_calls
        if attachments:
            try:
                json.dumps(attachments)
                msg["attachments"] = attachments
            except (TypeError, ValueError) as exc:
                # Replay sugar only — keep the message, drop the meta.
                logger.warning(
                    "agent.journal.append_attachments_serialize_failed",
                    error=str(exc),
                )
        try:
            return json.dumps(msg)
        except (TypeError, ValueError) as exc:  # pragma: no cover — defensive
            logger.warning("agent.journal.append_serialize_failed", error=str(exc))
            return None

    async def append_message(
        self,
        turn_id: int,
        role: str,
        content: str,
        *,
        tool_call_id: str | None = None,
        tool_calls: Any | None = None,
        attachments: Any | None = None,
    ) -> None:
        """Append one message at the next ``seq`` slot.

        ``RPUSH`` is atomic and ordering-preserving, so unlike the SQL
        backends no explicit ``MAX(seq)`` transaction is needed — the
        list index IS the seq. The parent-existence probe mirrors the
        FK constraint (an append against a deleted/unknown turn warns
        and no-ops instead of creating an orphan buffer).
        """
        doc = self._prepare_message_doc(role, content, tool_call_id, tool_calls, attachments)
        if doc is None:
            return
        try:
            r = self._r
            if not await r.exists(self._turn_key(turn_id)):
                logger.warning(
                    "agent.journal.append_failed",
                    error=f"turn {int(turn_id)} does not exist",
                )
                return
            await r.rpush(self._messages_key(turn_id), doc)
        except Exception as exc:
            logger.warning("agent.journal.append_failed", error=str(exc))

    async def append_messages(
        self,
        turn_id: int,
        messages: list[dict[str, Any]],
    ) -> None:
        """Append multiple messages in one ``RPUSH`` (single command =
        atomic + ordered, the Redis analogue of the SQL backends' one
        transaction). Empty ``messages`` is a no-op; a per-message
        ``tool_calls`` serialisation failure skips that message and
        continues, same as the SQL peers.
        """
        if not messages:
            return
        docs: list[str] = []
        for msg in messages:
            doc = self._prepare_message_doc(
                str(msg.get("role") or ""),
                msg.get("content") or "",
                msg.get("tool_call_id"),
                msg.get("tool_calls"),
                msg.get("attachments"),
            )
            if doc is not None:
                docs.append(doc)
        if not docs:
            return
        try:
            r = self._r
            if not await r.exists(self._turn_key(turn_id)):
                logger.warning(
                    "agent.journal.append_batch_failed",
                    error=f"turn {int(turn_id)} does not exist",
                )
                return
            await r.rpush(self._messages_key(turn_id), *docs)
        except Exception as exc:
            logger.warning("agent.journal.append_batch_failed", error=str(exc))

    # ------------------------------------------------------------------
    # Resume
    # ------------------------------------------------------------------

    async def find_resumable_turn(
        self,
        session_key: str,
        user_text: str,
        *,
        user_id: str | None = None,
        channel: str | None = None,
        runtime_instance_id: str | None = None,
    ) -> ResumeData | None:
        """Return the most-recent in-progress turn for ``session_key``
        whose ``user_text`` matches and that is younger than
        :data:`RESUME_MAX_AGE_MS`. Candidate-only semantics — the caller
        decides whether to resume.

        The global in-progress zset is small (bounded by concurrently
        running turns plus the 5-minute window), so a range read plus a
        pipelined hash filter is one round trip cheaper than any
        per-tuple index would earn back. S4 semantics match the SQL
        backends: ``user_id`` set → the row must carry the same user_id
        or none at all (legacy rows journaled before the column).
        """
        if not session_key or not user_text:
            return None
        cutoff = _now_ms() - RESUME_MAX_AGE_MS
        try:
            r = self._r
            members = await r.zrangebyscore(self._k("in_progress"), cutoff, "+inf")
            if not members:
                return None
            rows = await self._hgetall_many([self._turn_key(m) for m in members])
            candidates: list[tuple[int, int]] = []
            for member, row in zip(members, rows, strict=False):
                if not row or row.get("status") != TURN_IN_PROGRESS:
                    continue
                if row.get("session_key", "") != session_key:
                    continue
                if row.get("user_text") != user_text:
                    continue
                started_at_ms = _int_field(row, "started_at_ms") or 0
                if started_at_ms < cutoff:
                    # Hash is the source of truth; a skewed zset score
                    # must not resurrect an out-of-window turn.
                    continue
                if user_id is not None:
                    row_user = row.get("user_id")
                    if row_user is not None and row_user != user_id:
                        continue
                if channel is not None and row.get("channel", "") != channel:
                    continue
                if (
                    runtime_instance_id is not None
                    and row.get("runtime_instance_id", "") != runtime_instance_id
                ):
                    continue
                candidates.append((started_at_ms, int(member)))
        except Exception as exc:
            logger.warning("agent.journal.find_resumable_failed", error=str(exc))
            return None
        if not candidates:
            return None
        started_at_ms, turn_id = max(candidates)
        messages = await self.load_messages(turn_id)
        return ResumeData(
            turn_id=turn_id,
            started_at_ms=started_at_ms,
            messages=messages,
        )

    async def load_messages(self, turn_id: int) -> list[dict[str, Any]]:
        """Load every message under ``turn_id`` in seq (list) order."""
        try:
            docs = await self._r.lrange(self._messages_key(turn_id), 0, -1)
        except Exception as exc:
            logger.warning("agent.journal.load_messages_failed", error=str(exc))
            return []
        out: list[dict[str, Any]] = []
        for doc in docs:
            try:
                msg = json.loads(doc)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(msg, dict):
                out.append(msg)
        return out

    async def query_messages(
        self,
        *,
        start_ms: int,
        end_ms: int,
        roles: Sequence[str] | None = None,
        channels: Sequence[str] | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        session_key: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        """Scoped chronological read; same filter and ordering contract
        as the SQL backends (``(started_at_ms, turn_id, seq) ASC``,
        ``user_id`` strict-match, tenant guard, LIMIT on message rows).
        """
        limit_n = max(1, min(int(limit), 10000))
        role_values = {str(v) for v in (roles or []) if str(v)}
        channel_values = {str(v) for v in (channels or []) if str(v)}
        try:
            r = self._r
            index_key = (
                self._session_turns_key(session_key)
                if session_key is not None
                else self._k("turns")
            )
            members = await r.zrangebyscore(index_key, int(start_ms), int(end_ms))
            rows = await self._hgetall_many([self._turn_key(m) for m in members])
            turns: list[tuple[int, int, dict[str, str]]] = []
            for member, row in zip(members, rows, strict=False):
                if not row:
                    continue
                started_at_ms = _int_field(row, "started_at_ms") or 0
                if started_at_ms < int(start_ms) or started_at_ms > int(end_ms):
                    continue
                if channel_values and row.get("channel", "") not in channel_values:
                    continue
                if tenant_id is not None and not _tenant_matches(
                    row.get("tenant_id", ""), tenant_id
                ):
                    continue
                if user_id is not None and row.get("user_id") != user_id:
                    continue
                if session_key is not None and row.get("session_key", "") != session_key:
                    continue
                turns.append((started_at_ms, int(member), row))
            turns.sort(key=lambda t: (t[0], t[1]))
            out: list[dict[str, Any]] = []
            for started_at_ms, turn_id, row in turns:
                docs = await r.lrange(self._messages_key(turn_id), 0, -1)
                for seq, doc in enumerate(docs):
                    try:
                        msg = json.loads(doc)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if not isinstance(msg, dict):
                        continue
                    role = str(msg.get("role") or "")
                    if role_values and role not in role_values:
                        continue
                    out.append(
                        {
                            "turn_id": turn_id,
                            "session_key": row.get("session_key", ""),
                            "started_at_ms": started_at_ms,
                            "channel": row.get("channel", ""),
                            "tenant_id": row.get("tenant_id", ""),
                            "user_id": row.get("user_id"),
                            "seq": seq,
                            "role": role,
                            "content": msg.get("content") or "",
                        }
                    )
                    if len(out) >= limit_n:
                        return out
            return out
        except Exception as exc:
            logger.warning("agent.journal.query_messages_failed", error=str(exc))
            return []

    # ------------------------------------------------------------------
    # T4.4 — Error breadcrumbs
    # ------------------------------------------------------------------

    async def recent_errored_turns(self, session_key: str, limit: int = 5) -> list[dict[str, Any]]:
        """Return the most recent errored turns for ``session_key``."""
        try:
            pairs = await self._r.zrange(
                self._session_errored_key(session_key), 0, -1, withscores=True
            )
            ordered = sorted(
                ((int(score), int(member)) for member, score in pairs),
                reverse=True,
            )[: max(1, int(limit))]
            rows = await self._hgetall_many([self._turn_key(tid) for _, tid in ordered])
        except Exception as exc:
            logger.warning("agent.journal.recent_errored_failed", error=str(exc))
            return []
        out: list[dict[str, Any]] = []
        for (_, turn_id), row in zip(ordered, rows, strict=False):
            if not row:
                continue
            out.append(
                {
                    "turn_id": turn_id,
                    "started_at_ms": _int_field(row, "started_at_ms") or 0,
                    "ended_at_ms": _int_field(row, "ended_at_ms"),
                    "user_text": row.get("user_text"),
                    "error": row.get("error"),
                }
            )
        return out

    # ------------------------------------------------------------------
    # Sessions surface
    # ------------------------------------------------------------------

    async def _summarize_session(
        self, session_key: str, tenant_id: str | None
    ) -> SessionSummary | None:
        """Build one :class:`SessionSummary` — the Python fold of the SQL
        backends' per-session aggregate (min/max started, counts, latest
        turn's text+status, meta join). ``None`` when the session has no
        turns (or none visible to ``tenant_id``)."""
        r = self._r
        pairs = await r.zrange(self._session_turns_key(session_key), 0, -1)
        if not pairs:
            return None
        rows = await self._hgetall_many([self._turn_key(m) for m in pairs])
        turns: list[tuple[int, int, dict[str, str]]] = []
        for member, row in zip(pairs, rows, strict=False):
            if not row:
                continue
            if tenant_id is not None and not _tenant_matches(row.get("tenant_id", ""), tenant_id):
                continue
            turns.append((_int_field(row, "started_at_ms") or 0, int(member), row))
        if not turns:
            return None
        turns.sort(key=lambda t: (t[0], t[1]))
        first_seen = turns[0][0]
        last_seen = turns[-1][0]
        latest_row = turns[-1][2]
        async with r.pipeline(transaction=False) as pipe:
            for _, turn_id, _row in turns:
                pipe.llen(self._messages_key(turn_id))
            lengths = await pipe.execute()
        message_count = sum(int(n or 0) for n in lengths)
        meta = await r.hgetall(self._meta_key(session_key))
        preview = latest_row.get("user_text")
        if preview is not None and len(preview) > SESSION_SUMMARY_PREVIEW_LEN:
            preview = preview[:SESSION_SUMMARY_PREVIEW_LEN]
        return SessionSummary(
            session_key=session_key,
            first_seen_at_ms=first_seen,
            last_seen_at_ms=last_seen,
            turn_count=len(turns),
            message_count=message_count,
            last_user_text=preview,
            last_status=latest_row.get("status"),
            title=meta.get("title") if meta else None,
            pinned=bool(meta) and meta.get("pinned") == "1",
            archived=bool(meta) and meta.get("archived") == "1",
        )

    async def list_session_summaries(
        self, *, limit: int = 200, tenant_id: str | None = None
    ) -> list[SessionSummary]:
        """One row per session, ``ORDER BY pinned DESC, last_seen DESC``.

        The full session set is enumerated (pinned sessions may sit
        anywhere in recency order, exactly why the SQL backends also
        aggregate the whole table before LIMIT); per-session folds are
        pipelined. Admin surface — correctness over micro-latency.
        """
        if limit <= 0:
            return []
        try:
            session_keys = await self._r.zrange(self._k("sessions"), 0, -1)
            summaries: list[SessionSummary] = []
            for session_key in session_keys:
                summary = await self._summarize_session(session_key, tenant_id)
                if summary is not None:
                    summaries.append(summary)
            summaries.sort(key=lambda s: (not s.pinned, -s.last_seen_at_ms, s.session_key))
            return summaries[: int(limit)]
        except Exception as exc:
            logger.warning(
                "agent.journal.list_session_summaries_failed",
                error=str(exc),
            )
            return []

    async def delete_session(self, session_key: str, *, tenant_id: str | None = None) -> int:
        """Delete every turn (and its replay buffer) for ``session_key``.

        Returns the number of turn rows deleted; ``0`` when nothing
        matched (routes map that to 404). The message lists are deleted
        explicitly — Redis has no FK cascade — and any live C5 claim is
        released so a follow-up ``begin_turn`` isn't blocked. The
        ``session_meta`` hash survives on purpose (same lifecycle call
        the SQL backends made: a recreated session keeps its title).

        W8 — ``tenant_id`` restricts the wipe to turns owned by that
        tenant; a cross-tenant delete matches nothing and returns 0.
        """
        if not session_key:
            return 0
        try:
            r = self._r
            session_turns_key = self._session_turns_key(session_key)
            members = await r.zrange(session_turns_key, 0, -1)
            if not members:
                return 0
            rows = await self._hgetall_many([self._turn_key(m) for m in members])
            deleted = 0
            async with r.pipeline(transaction=True) as pipe:
                for member, row in zip(members, rows, strict=False):
                    if not row:
                        # Orphan index member (should not happen) — clean
                        # it up but don't count it as a deleted turn.
                        pipe.zrem(session_turns_key, member)
                        pipe.zrem(self._k("turns"), member)
                        pipe.zrem(self._k("in_progress"), member)
                        pipe.zrem(self._session_errored_key(session_key), member)
                        continue
                    if tenant_id is not None and not _tenant_matches(
                        row.get("tenant_id", ""), tenant_id
                    ):
                        continue
                    turn_id = int(member)
                    pipe.delete(self._turn_key(turn_id))
                    pipe.delete(self._messages_key(turn_id))
                    pipe.zrem(session_turns_key, member)
                    pipe.zrem(self._k("turns"), member)
                    pipe.zrem(self._k("in_progress"), member)
                    pipe.zrem(self._session_errored_key(session_key), member)
                    open_key = row.get("open_key")
                    if open_key:
                        pipe.delete(open_key)
                    deleted += 1
                await pipe.execute()
            if await r.zcard(session_turns_key) == 0:
                await r.zrem(self._k("sessions"), session_key)
            return deleted
        except Exception as exc:
            logger.warning("agent.journal.delete_session_failed", error=str(exc))
            return 0

    async def session_exists(self, session_key: str, *, tenant_id: str | None = None) -> bool:
        """Existence probe scoped to journaled turns (not the meta hash)
        so stale meta can't resurrect a deleted session.

        W8 — with ``tenant_id`` set, a session whose turns all belong to
        another tenant reads as absent.
        """
        if not session_key:
            return False
        try:
            r = self._r
            if tenant_id is None:
                return int(await r.zcard(self._session_turns_key(session_key))) > 0
            members = await r.zrange(self._session_turns_key(session_key), 0, -1)
            if not members:
                return False
            rows = await self._hgetall_many([self._turn_key(m) for m in members])
            return any(
                row and _tenant_matches(row.get("tenant_id", ""), tenant_id) for row in rows
            )
        except Exception as exc:
            logger.warning("agent.journal.session_exists_failed", error=str(exc))
            return False

    async def update_session_meta(
        self,
        session_key: str,
        *,
        title: str | None = None,
        pinned: bool | None = None,
        archived: bool | None = None,
        tenant_id: str | None = None,
    ) -> SessionSummary | None:
        """Upsert title/pinned/archived for ``session_key``.

        Partial-update semantics match the SQL peers: ``None`` leaves a
        field alone (only non-None fields are HSET). Returns the
        refreshed :class:`SessionSummary`, or ``None`` when the session
        has no journaled turns (→ 404 upstream).
        """
        if not await self.session_exists(session_key, tenant_id=tenant_id):
            return None
        mapping: dict[str, str] = {"updated_at_ms": str(_now_ms())}
        if title is not None:
            mapping["title"] = title
        if pinned is not None:
            mapping["pinned"] = "1" if pinned else "0"
        if archived is not None:
            mapping["archived"] = "1" if archived else "0"
        try:
            await self._r.hset(self._meta_key(session_key), mapping=mapping)
            return await self._summarize_session(session_key, tenant_id)
        except Exception as exc:
            logger.warning(
                "agent.journal.update_session_meta_failed",
                error=str(exc),
                session_key=session_key,
            )
            return None

    # ------------------------------------------------------------------
    # Stale sweep / auto-resume scan
    # ------------------------------------------------------------------

    async def mark_stale_in_progress_as_errored(self, older_than_seconds: int | None = None) -> int:
        """Sweep in-progress turns started strictly before the cutoff.

        ``older_than_seconds=None`` keeps the legacy
        :data:`RESUME_MAX_AGE_MS` window; the boot-time resume service
        passes a much larger window (e.g. 24h). Each flip goes through
        the same CAS as :meth:`error_turn`, so a turn that completes
        between the scan and the flip is left alone. Returns the number
        of rows flipped.
        """
        now = _now_ms()
        if older_than_seconds is None:
            cutoff = now - RESUME_MAX_AGE_MS
        else:
            cutoff = now - max(0, int(older_than_seconds)) * 1000
        try:
            members = await self._r.zrangebyscore(self._k("in_progress"), "-inf", f"({cutoff}")
            flipped = 0
            for member in members:
                if await self._terminalize_errored(
                    int(member), _ABANDONED_ERROR, keep_existing_error=True
                ):
                    flipped += 1
        except Exception as exc:
            logger.warning("agent.journal.sweep_failed", error=str(exc))
            return 0
        if flipped:
            logger.info("agent.journal.swept_stale", count=flipped)
        return flipped

    async def list_resumable_in_progress(
        self, *, window_ms: int = RESUME_MAX_AGE_MS
    ) -> list[InProgressTurn]:
        """Every in-progress turn started within ``window_ms``, ordered
        ``started_at_ms ASC`` (arrival order for re-delivery). Behaves
        like the SQLite/Postgres peers; empty list on any read failure.
        """
        cutoff = _now_ms() - max(0, int(window_ms))
        try:
            members = await self._r.zrangebyscore(self._k("in_progress"), cutoff, "+inf")
            rows = await self._hgetall_many([self._turn_key(m) for m in members])
        except Exception as exc:
            logger.warning(
                "agent.journal.list_resumable_in_progress_failed",
                error=str(exc),
            )
            return []
        entries: list[tuple[int, int, dict[str, str]]] = []
        for member, row in zip(members, rows, strict=False):
            if not row or row.get("status") != TURN_IN_PROGRESS:
                continue
            entries.append((_int_field(row, "started_at_ms") or 0, int(member), row))
        entries.sort(key=lambda t: (t[0], t[1]))
        return [
            InProgressTurn(
                turn_id=turn_id,
                session_key=row.get("session_key", ""),
                user_id=row.get("user_id"),
                user_text=row.get("user_text", ""),
                started_at_ms=started_at_ms,
                channel=row.get("channel", ""),
                runtime_instance_id=row.get("runtime_instance_id", ""),
            )
            for started_at_ms, turn_id, row in entries
        ]

    # ------------------------------------------------------------------
    # W1.2 — turn events timeline.
    #
    # Stubbed exactly like the Postgres backend: the admin replay surface
    # targets the SQLite journal (see that backend's block comment); the
    # SSE bridge degrades to "no replay buffer" rather than 500-ing.
    # ------------------------------------------------------------------

    async def append_event(self, envelope: Any) -> None:  # pragma: no cover
        return None

    async def append_events_batch(self, envelopes: Sequence[Any]) -> None:  # pragma: no cover
        return None

    async def load_events(self, turn_id: str | int) -> list[dict[str, Any]]:  # pragma: no cover
        return []

    async def iter_events(  # type: ignore[misc]
        self, turn_id: str | int, start_sequence: int = 0, limit: int | None = None
    ) -> AsyncIterator[dict[str, Any]]:  # pragma: no cover
        # Async-generator stub — yields nothing (see the Postgres peer
        # for why this stays a generator function).
        if False:
            yield {}
        return

    async def latest_sequence(self, turn_id: str | int) -> int:  # pragma: no cover
        return -1

    async def latest_event_rowid(self) -> int:  # pragma: no cover
        return 0

    async def load_subagent_events_since(
        self, after_rowid: int, *, limit: int = 500
    ) -> tuple[int, list[dict[str, Any]]]:  # pragma: no cover
        return int(after_rowid), []

    # ------------------------------------------------------------------
    # Turn listings
    # ------------------------------------------------------------------

    async def get_session_turn_ids(self, session_key: str, limit: int = 50) -> list[int]:
        """Most-recent turn ids for ``session_key`` (newest first)."""
        if not session_key or limit <= 0:
            return []
        try:
            pairs = await self._r.zrange(
                self._session_turns_key(session_key), 0, -1, withscores=True
            )
        except Exception as exc:
            logger.warning("agent.journal.get_session_turn_ids_failed", error=str(exc))
            return []
        ordered = sorted(((int(score), int(member)) for member, score in pairs), reverse=True)
        return [turn_id for _, turn_id in ordered[: int(limit)]]

    async def list_session_turns(
        self,
        session_key: str,
        *,
        limit: int = 50,
        before_turn_id: str | None = None,
        tenant_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Per-turn metadata page, newest first, keyset-paged on
        ``(started_at_ms, turn_id)``. An unknown/invalid cursor returns
        an empty page — same semantics as the SQL backends' NULL
        subquery comparison.
        """
        if not session_key or limit <= 0:
            return []
        try:
            members = await self._r.zrange(self._session_turns_key(session_key), 0, -1)
            rows = await self._hgetall_many([self._turn_key(m) for m in members])
            entries: list[tuple[int, int, dict[str, str]]] = []
            for member, row in zip(members, rows, strict=False):
                if not row:
                    continue
                if tenant_id is not None and not _tenant_matches(
                    row.get("tenant_id", ""), tenant_id
                ):
                    continue
                entries.append((_int_field(row, "started_at_ms") or 0, int(member), row))
            if before_turn_id is not None:
                try:
                    cursor_id = int(before_turn_id)
                except (TypeError, ValueError):
                    cursor_id = -1
                cursor_row = (
                    await self._r.hgetall(self._turn_key(cursor_id)) if cursor_id >= 0 else None
                )
                if not cursor_row:
                    return []
                cursor_started = _int_field(dict(cursor_row), "started_at_ms") or 0
                entries = [e for e in entries if (e[0], e[1]) < (cursor_started, cursor_id)]
            entries.sort(key=lambda t: (t[0], t[1]), reverse=True)
        except Exception as exc:
            logger.warning("agent.journal.list_session_turns_failed", error=str(exc))
            return []
        out: list[dict[str, Any]] = []
        for started_at_ms, turn_id, row in entries[: int(limit)]:
            user_text = row.get("user_text")
            preview: str | None = None
            if isinstance(user_text, str):
                preview = user_text[:200] + "…" if len(user_text) > 200 else user_text
            out.append(
                {
                    "turn_id": str(turn_id),
                    "started_at_ms": started_at_ms,
                    "ended_at_ms": _int_field(row, "ended_at_ms"),
                    "status": row.get("status"),
                    # Same placeholder as the SQL backends: finish_reason
                    # lives in the (stubbed) event timeline, not on turns.
                    "finish_reason": None,
                    "elapsed_ms": _int_field(row, "elapsed_ms"),
                    "estimated_cost_usd": _float_field(row, "estimated_cost_usd"),
                    "cost_status": row.get("cost_status"),
                    "tool_call_count": _int_field(row, "tool_call_count") or 0,
                    "reasoning_token_count": _int_field(row, "reasoning_token_count") or 0,
                    "user_text_preview": preview,
                }
            )
        return out

    async def update_turn_cost(
        self,
        turn_id: int,
        *,
        estimated_cost_usd: float | None,
        cost_status: str | None,
    ) -> None:
        """Late-binding cost write; ``None`` leaves a field untouched.
        A missing turn is a silent no-op (mirrors UPDATE-zero-rows)."""
        if estimated_cost_usd is None and cost_status is None:
            return
        mapping: dict[str, str] = {}
        if estimated_cost_usd is not None:
            mapping["estimated_cost_usd"] = str(float(estimated_cost_usd))
        if cost_status is not None:
            mapping["cost_status"] = str(cost_status)
        try:
            r = self._r
            turn_key = self._turn_key(turn_id)
            if not await r.exists(turn_key):
                return
            await r.hset(turn_key, mapping=mapping)
        except Exception as exc:
            logger.warning("agent.journal.update_turn_cost_failed", error=str(exc))


def _doc_role(doc: str) -> str:
    """Best-effort role extraction from one stored message doc."""
    try:
        msg = json.loads(doc)
    except (TypeError, json.JSONDecodeError):
        return ""
    if isinstance(msg, dict):
        return str(msg.get("role") or "")
    return ""


__all__ = ["RedisJournalBackend"]
