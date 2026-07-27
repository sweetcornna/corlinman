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
    """

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self._data_dir = data_dir
        self._lock = threading.RLock()
        #: (tenant, session_key, tool, digest)
        self._session: set[tuple[str, str, str, str]] = set()
        #: (tenant, surface, user, tool, digest) — mirror of the DB plus
        #: any rows that failed to persist.
        self._always: set[tuple[str, str, str, str, str]] = set()
        self._db_loaded = False
        self._db_failed = False

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

    def _load_db(self) -> None:
        """Populate the always-grant cache from SQLite once per store."""
        if self._db_loaded or self._db_failed:
            return
        conn = self._connect()
        if conn is None:
            self._db_loaded = True
            return
        try:
            rows = conn.execute(
                "SELECT tenant, surface, user_id, tool, arg_digest FROM grants "
                "WHERE kind = 'always'"
            ).fetchall()
            for tenant, surface, user_id, tool, digest in rows:
                self._always.add(
                    (str(tenant), str(surface), str(user_id), str(tool), str(digest))
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent.authz.grant_store_read_failed", error=str(exc))
        finally:
            conn.close()
        self._db_loaded = True

    def _persist_always(self, key: tuple[str, str, str, str, str]) -> None:
        conn = self._connect()
        if conn is None:
            return
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
        finally:
            conn.close()

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
            surface = (
                str(getattr(subject, "surface", None) or "") if scope_surface else ""
            )
            user = str(getattr(subject, "user_id", None) or "") if scope_user else ""
            key = (tenant, surface, user, tool, digest)
            self._load_db()
            self._always.add(key)
            self._persist_always(key)

    def is_granted(self, subject: Any, tool: str, args: dict[str, Any] | None) -> bool:
        """True when a session or always grant covers this exact call."""
        tenant = _tenant_of(subject)
        digest = arg_digest(tool, args)
        session_key = str(getattr(subject, "session_key", None) or "")
        with self._lock:
            if (tenant, session_key, tool, digest) in self._session:
                return True
            self._load_db()
            surface = str(getattr(subject, "surface", None) or "")
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
            self._delete_always(key)

    def reset(self) -> None:
        """Forget everything in memory and re-arm lazy DB loading (tests)."""
        with self._lock:
            self._session.clear()
            self._always.clear()
            self._db_loaded = False
            self._db_failed = False


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
