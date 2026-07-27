"""Memoried interactive grants (audit W3-1 — the ``GrantStore``).

Replaces the console resolver's ``always_allow: set[str]`` — a cache keyed
on the TOOL NAME ALONE, which meant approving one ``run_shell ls`` silenced
the prompt for every later ``run_shell`` including ``rm -rf /`` (agent-gate
finding §8.7). Three hard rules, from the design plan §1.2.4:

1. **The grant key includes the argument dimension** —
   ``arg_digest = sha256(extract_arg_candidates(tool, args))``. A grant is
   exact by default; the same tool with different arguments prompts again.
2. **``always`` grants land here (SQLite), never in the rules file.** The
   old ``persist_allow_rule`` path flattened a one-time, args-scoped
   approval into a global unconditional allow rule. The rules file is for
   operator-written policy; grants live in
   ``<data_dir>/authz/grants.sqlite3``.
3. **A grant can only narrow ``ask`` — it can never beat ``deny``.**
   Enforced structurally: the gate consults this store only after the rule
   scan returned ``ask`` (evaluation-order step 5), so a ``deny`` rule has
   already won by the time a grant could speak.

Key shapes:

* session grant — ``(tenant, session_key, tool, arg_digest)``; in-memory
  (evaporates with the process, cleared on session / mode boundaries).
* always grant — ``(tenant, surface?, user?, tool, arg_digest)``; durable
  SQLite, shared across processes on the same data_dir. The console's
  defaults leave ``surface`` / ``user`` out of the key (a trusted operator
  terminal's "always" means globally); channel prompts will default to
  scoping both (W3-3).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import structlog

from corlinman_agent.authz.matcher import extract_arg_candidates
from corlinman_agent.authz.model import Memory

logger = structlog.get_logger(__name__)

__all__ = [
    "GrantStore",
    "arg_digest",
    "get_grant_store",
    "reset_grant_store",
]

_DB_RELPATH = Path("authz") / "grants.sqlite3"

_DDL = """
CREATE TABLE IF NOT EXISTS grants (
    kind        TEXT NOT NULL,
    tenant      TEXT NOT NULL DEFAULT '',
    surface     TEXT NOT NULL DEFAULT '',
    user_id     TEXT NOT NULL DEFAULT '',
    tool        TEXT NOT NULL,
    arg_digest  TEXT NOT NULL,
    created_at  REAL NOT NULL,
    PRIMARY KEY (kind, tenant, surface, user_id, tool, arg_digest)
)
"""


def arg_digest(tool: str, args: dict[str, Any] | None) -> str:
    """Stable digest of the call's permission-relevant argument surface.

    Built from :func:`extract_arg_candidates` — the SAME normalization the
    rule matcher uses — so ``run_shell {"command": "ls"}`` and a later
    identical call collapse to one grant while ``rm -rf /`` gets its own.
    An argless call digests the empty candidate set (still a valid key).
    """
    candidates = extract_arg_candidates(tool, args)
    if candidates is None:
        payload: list[str] = []
    elif isinstance(candidates, str):
        payload = [candidates]
    else:
        payload = sorted(candidates)
    blob = json.dumps([tool, payload], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _surface_of(subject: Any) -> str:
    """The surface a scoped grant keys on.

    A subagent call carries ``surface="subagent"`` with the spawning
    turn's surface in ``parent_surface`` — grants belong to the HUMAN
    surface (the person who answered the prompt), so the parent surface
    is the key. Without this a child-granted approval never satisfied
    the parent's later asks AND was shared across every unrelated
    surface's subagents (W3-3 review fix).
    """
    surface = getattr(subject, "surface", None)
    if surface == "subagent":
        parent = getattr(subject, "parent_surface", None)
        if isinstance(parent, str) and parent.strip():
            return parent.strip()
    return str(surface or "")


def _tenant_of(subject: Any) -> str:
    """Normalize the tenant key exactly like the servicer's dispatch path.

    ``subject.tenant_id`` when populated; else the ``<tenant>::`` prefix of
    the session key; else the single-tenant sentinel ``"default"``. Both the
    record path (a resolver holding only a ``PermissionContext``) and the
    check path (the gate holding a full ``Subject``) go through here so
    their keys can never diverge.
    """
    tenant = getattr(subject, "tenant_id", None)
    if isinstance(tenant, str) and tenant.strip():
        return tenant.strip()
    session_key = getattr(subject, "session_key", None)
    if isinstance(session_key, str) and session_key:
        return session_key.split("::")[0]
    return "default"


def _resolve_data_dir(data_dir: Path | str | None) -> Path:
    if data_dir is not None:
        return Path(data_dir)
    env = os.environ.get("CORLINMAN_DATA_DIR", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".corlinman"


class GrantStore:
    """Session (in-memory) + always (SQLite) grant storage.

    Every SQLite touch is best-effort: an unwritable data_dir degrades the
    ``always`` tier to process-memory (with one WARN) rather than turning
    an approval into a deny or crashing dispatch.

    Cross-process invalidation (W3-4): the gateway's admin surface and the
    agent process share the SQLite file but hold *separate* in-memory
    mirrors, so a revocation written by the gateway must become visible to
    the agent without a restart. Strategy: every :meth:`is_granted` /
    :meth:`list_always` call re-stats the DB file and reloads the mirror
    when ``st_mtime_ns`` changed. Trade-offs, deliberately accepted:

    * cost — one ``os.stat`` per permission check (~µs); tool dispatch is
      already a millisecond-plus operation, so no throttle is needed and
      revocations take effect at the *next permission check* (≤ next turn).
    * mtime granularity — ``st_mtime_ns`` is nanosecond-precise on APFS
      and ext4; a same-instant writer pair could in theory be missed, but
      grants change at human speed, not write-storm speed.
    * memory-only rows — grants that failed to persist (unwritable DB)
      are tracked separately and survive a reload; they remain invisible
      to (and irrevocable from) other processes, which is exactly the
      degraded-durability contract the WARN log announces.
    """

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self._data_dir = data_dir
        self._lock = threading.RLock()
        #: (tenant, session_key, tool, digest)
        self._session: set[tuple[str, str, str, str]] = set()
        #: (tenant, surface, user, tool, digest) — mirror of the DB plus
        #: any rows that failed to persist.
        self._always: set[tuple[str, str, str, str, str]] = set()
        #: Rows we could not write to SQLite; re-merged after every
        #: reload so a broken DB never silently drops a live grant.
        self._always_unpersisted: set[tuple[str, str, str, str, str]] = set()
        self._db_loaded = False
        self._db_failed = False
        #: ``st_mtime_ns`` of the DB file at the last successful load —
        #: the cross-process invalidation watermark.
        self._db_mtime_ns: int | None = None

    # -- paths ----------------------------------------------------------

    def db_path(self) -> Path:
        return _resolve_data_dir(self._data_dir) / _DB_RELPATH

    # -- sqlite plumbing (lazy, tolerant) --------------------------------

    def _connect(self) -> sqlite3.Connection | None:
        try:
            path = self.db_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(path), timeout=5.0)
            conn.execute(_DDL)
            conn.commit()
            return conn
        except Exception as exc:  # noqa: BLE001 — degrade to memory-only
            if not self._db_failed:
                self._db_failed = True
                logger.warning(
                    "agent.authz.grant_store_db_unavailable", error=str(exc)
                )
            return None

    def _stat_mtime_ns(self) -> int | None:
        try:
            return os.stat(self.db_path()).st_mtime_ns
        except OSError:
            return None

    def _load_db(self) -> None:
        """Populate the always-grant cache from SQLite once per store."""
        if self._db_loaded or self._db_failed:
            return
        self._reload_db()
        self._db_loaded = True

    def _reload_db(self) -> None:
        """(Re)build the always mirror from disk; keeps unpersisted rows."""
        conn = self._connect()
        if conn is None:
            return
        mtime = self._stat_mtime_ns()
        fresh: set[tuple[str, str, str, str, str]] = set()
        try:
            rows = conn.execute(
                "SELECT tenant, surface, user_id, tool, arg_digest FROM grants "
                "WHERE kind = 'always'"
            ).fetchall()
            for tenant, surface, user_id, tool, digest in rows:
                fresh.add(
                    (str(tenant), str(surface), str(user_id), str(tool), str(digest))
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent.authz.grant_store_read_failed", error=str(exc))
            conn.close()
            return
        conn.close()
        self._always = fresh | self._always_unpersisted
        self._db_mtime_ns = mtime

    def _reload_if_changed(self) -> None:
        """Cross-process invalidation: reload when the DB file changed.

        Called (under the store lock) from every read path so a grant
        revoked by the gateway's admin surface disappears from the agent
        process at its next permission check. See the class docstring for
        the cost/granularity trade-offs.
        """
        if self._db_failed:
            return
        if not self._db_loaded:
            self._load_db()
            return
        if self._stat_mtime_ns() != self._db_mtime_ns:
            self._reload_db()

    def _persist_always(self, key: tuple[str, str, str, str, str]) -> bool:
        conn = self._connect()
        if conn is None:
            return False
        try:
            conn.execute(
                "INSERT OR REPLACE INTO grants "
                "(kind, tenant, surface, user_id, tool, arg_digest, created_at) "
                "VALUES ('always', ?, ?, ?, ?, ?, ?)",
                (*key, time.time()),
            )
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent.authz.grant_store_write_failed", error=str(exc))
            return False
        finally:
            conn.close()
        self._db_mtime_ns = self._stat_mtime_ns()
        return True

    def _delete_always(self, key: tuple[str, str, str, str, str]) -> None:
        conn = self._connect()
        if conn is None:
            return
        try:
            conn.execute(
                "DELETE FROM grants WHERE kind = 'always' AND tenant = ? "
                "AND surface = ? AND user_id = ? AND tool = ? AND arg_digest = ?",
                key,
            )
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent.authz.grant_store_delete_failed", error=str(exc))
        finally:
            conn.close()
        self._db_mtime_ns = self._stat_mtime_ns()

    # -- public API ------------------------------------------------------

    def record(
        self,
        subject: Any,
        tool: str,
        args: dict[str, Any] | None,
        memory: Memory | str,
        *,
        scope_surface: bool = False,
        scope_user: bool = False,
    ) -> None:
        """Remember an approval. ``once`` records nothing.

        ``scope_surface`` / ``scope_user`` narrow an ``always`` grant to
        the granting surface / user (the channel-side default, W3-3); the
        console default leaves both off.
        """
        mem = Memory.coerce(memory)
        if mem is Memory.ONCE:
            return
        tenant = _tenant_of(subject)
        digest = arg_digest(tool, args)
        with self._lock:
            if mem is Memory.SESSION:
                session_key = str(getattr(subject, "session_key", None) or "")
                self._session.add((tenant, session_key, tool, digest))
                return
            surface = _surface_of(subject) if scope_surface else ""
            user = str(getattr(subject, "user_id", None) or "") if scope_user else ""
            key = (tenant, surface, user, tool, digest)
            self._load_db()
            self._always.add(key)
            if not self._persist_always(key):
                self._always_unpersisted.add(key)

    def is_granted(self, subject: Any, tool: str, args: dict[str, Any] | None) -> bool:
        """True when a session or always grant covers this exact call."""
        tenant = _tenant_of(subject)
        digest = arg_digest(tool, args)
        session_key = str(getattr(subject, "session_key", None) or "")
        with self._lock:
            if (tenant, session_key, tool, digest) in self._session:
                return True
            self._reload_if_changed()
            surface = _surface_of(subject)
            user = str(getattr(subject, "user_id", None) or "")
            # Unscoped grant, then the progressively-narrower scoped shapes.
            for key in (
                (tenant, "", "", tool, digest),
                (tenant, surface, "", tool, digest),
                (tenant, "", user, tool, digest),
                (tenant, surface, user, tool, digest),
            ):
                if key in self._always:
                    return True
        return False

    def clear_session_grants(self, session_key: str | None = None) -> None:
        """Drop session grants — one session's, or every session's.

        Called on session boundaries (``/new`` / ``/clear``) and on EVERY
        permission-mode switch: the gate resolves explicit ``ask`` rules
        BEFORE the mode override, so a cached grant would otherwise bypass
        ``/plan`` entirely (Codex #104 — the constraint W3-1 §1.3 upgrades
        to "``set_mode()`` must invalidate session grants").
        """
        with self._lock:
            if session_key is None:
                self._session.clear()
            else:
                self._session = {
                    entry for entry in self._session if entry[1] != session_key
                }

    def session_grant_tools(self, session_key: str | None = None) -> set[str]:
        """Tool names with at least one live session grant (console listing)."""
        with self._lock:
            return {
                entry[2]
                for entry in self._session
                if session_key is None or entry[1] == session_key
            }

    def revoke_always(
        self, subject: Any, tool: str, args: dict[str, Any] | None
    ) -> None:
        """Remove a durable grant (memory + SQLite), unscoped-key shape."""
        tenant = _tenant_of(subject)
        digest = arg_digest(tool, args)
        key = (tenant, "", "", tool, digest)
        with self._lock:
            self._always.discard(key)
            self._always_unpersisted.discard(key)
            self._delete_always(key)

    def list_always(self) -> list[dict[str, Any]]:
        """Every durable grant as a plain dict (W3-4 admin listing).

        Reads the DB fresh (after a mtime check) so the gateway's admin
        surface always sees rows written by the agent process. Rows that
        exist only in this process's memory (persist failed) are appended
        with ``created_at=None`` so the operator still sees them.
        """
        rows: list[dict[str, Any]] = []
        with self._lock:
            self._reload_if_changed()
            conn = self._connect()
            seen: set[tuple[str, str, str, str, str]] = set()
            if conn is not None:
                try:
                    for tenant, surface, user_id, tool, digest, created in conn.execute(
                        "SELECT tenant, surface, user_id, tool, arg_digest, "
                        "created_at FROM grants WHERE kind = 'always' "
                        "ORDER BY created_at DESC"
                    ).fetchall():
                        key = (
                            str(tenant),
                            str(surface),
                            str(user_id),
                            str(tool),
                            str(digest),
                        )
                        seen.add(key)
                        rows.append(
                            {
                                "tenant": key[0],
                                "surface": key[1],
                                "user_id": key[2],
                                "tool": key[3],
                                "arg_digest": key[4],
                                "created_at": float(created),
                            }
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "agent.authz.grant_store_read_failed", error=str(exc)
                    )
                finally:
                    conn.close()
            for key in sorted(self._always - seen):
                rows.append(
                    {
                        "tenant": key[0],
                        "surface": key[1],
                        "user_id": key[2],
                        "tool": key[3],
                        "arg_digest": key[4],
                        "created_at": None,
                    }
                )
        return rows

    def revoke_always_entry(
        self,
        *,
        tenant: str,
        surface: str = "",
        user_id: str = "",
        tool: str,
        arg_digest: str,
    ) -> bool:
        """Revoke one durable grant by its exact key (W3-4 admin surface).

        Returns ``True`` when the grant existed (in the DB or this
        process's mirror). The deletion bumps the DB file's mtime, which
        is what makes the agent process drop the grant at its next
        permission check (see the class docstring).
        """
        key = (str(tenant), str(surface), str(user_id), str(tool), str(arg_digest))
        with self._lock:
            self._reload_if_changed()
            existed = key in self._always
            self._always.discard(key)
            self._always_unpersisted.discard(key)
            self._delete_always(key)
        return existed

    def reset(self) -> None:
        """Forget everything in memory and re-arm lazy DB loading (tests)."""
        with self._lock:
            self._session.clear()
            self._always.clear()
            self._always_unpersisted.clear()
            self._db_loaded = False
            self._db_failed = False
            self._db_mtime_ns = None


# ---------------------------------------------------------------------------
# Process-global store — the gate, the console resolver and (later) the
# admin surface must share ONE instance or record/check keys diverge.
# ---------------------------------------------------------------------------

_STORE: GrantStore | None = None
_STORE_LOCK = threading.RLock()


def get_grant_store(data_dir: Path | str | None = None) -> GrantStore:
    """The shared store. ``data_dir`` only matters on first construction."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = GrantStore(data_dir)
        return _STORE


def reset_grant_store() -> None:
    """Drop the shared store entirely (tests — data_dir may change)."""
    global _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            _STORE.reset()
        _STORE = None
