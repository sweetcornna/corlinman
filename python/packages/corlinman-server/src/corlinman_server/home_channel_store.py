"""Per-user "home channel" assignment store + first-chat tip flag.

Companion to the W3 first-run wizard (see ``docs/PLAN_FIRST_RUN_WIZARD.md``
Agent D's slice).

Two responsibilities, one SQLite file:

* :func:`set_home` / :func:`get_home` / :func:`list_all_homes` —
  remember which :class:`~corlinman_channels.common.ChannelBinding` a
  user designated as their "home" via the ``/sethome`` slash command.
  Server-restart broadcasts (and any other important system pings) are
  sent only to home channels so the noise stays opt-in.

* :func:`mark_tip_shown` / :func:`was_tip_shown` — record which
  ``(user_id, channel, thread)`` triples have already seen the one-time
  "tip: try /sethome" system message. The chat-bootstrap layer reads
  this before its LLM dispatch so the same user never sees the same
  tip twice.

Connection model
----------------

Pure stdlib :mod:`sqlite3`, one short connection per call. The store
is consumed from both async (lifecycle / channel handlers) and sync
(``chat_bootstrap.rewrite_trailing_user_message``) call sites, so an
async-only API would force the sync paths to spin up an event loop on
the hot path. Per-call connections also dodge the cross-connection WAL
visibility race that the documented per-tenant stores worry about, at
the cost of a few extra ``open`` syscalls — fine for the call-volume
this surface sees (one read on chat-start, one write on /sethome, one
write on first-chat tip).

DB path resolution mirrors the identity store: take ``data_dir`` from
the gateway boot (``CORLINMAN_DATA_DIR`` env var or the ``--data-dir``
arg) and write the SQLite file as
``<data_dir>/home_channels.sqlite``. Callers that want a different
location pass an explicit ``db_path`` to every helper.

Schema
------

Two tables; both are ``CREATE TABLE IF NOT EXISTS`` so opening an
already-bootstrapped file is a no-op.

``home_channels`` — PK on ``user_id`` so a re-issue of ``/sethome``
silently replaces the previous binding (the user reset their home).
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "HomeChannelRow",
    "default_db_path",
    "get_home",
    "list_all_homes",
    "mark_tip_shown",
    "resolve_user_id",
    "set_home",
    "was_tip_shown",
]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS home_channels (
    user_id   TEXT PRIMARY KEY,
    channel   TEXT NOT NULL,
    account   TEXT NOT NULL,
    thread    TEXT NOT NULL,
    sender    TEXT NOT NULL,
    set_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS first_chat_tips_shown (
    user_id   TEXT NOT NULL,
    channel   TEXT NOT NULL,
    thread    TEXT NOT NULL,
    shown_at_ms INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, channel, thread)
);
"""


# ---------------------------------------------------------------------------
# Public dataclass / path helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HomeChannelRow:
    """One row out of :func:`list_all_homes`.

    Mirrors the four-tuple of ``ChannelBinding`` so a caller can
    rebuild the binding without importing the channels package (which
    would create a soft cycle when this store is used from inside the
    channels handlers themselves).
    """

    user_id: str
    channel: str
    account: str
    thread: str
    sender: str
    set_at_ms: int


def default_db_path() -> Path:
    """Resolve the canonical SQLite file path under the gateway data dir.

    Precedence matches ``entrypoint._resolve_data_dir``:

    1. ``$CORLINMAN_DATA_DIR`` env var,
    2. ``~/.corlinman``,
    3. ``./.corlinman`` (final fallback when ``$HOME`` is missing).
    """
    raw = os.environ.get("CORLINMAN_DATA_DIR")
    if raw:
        return Path(raw) / "home_channels.sqlite"
    try:
        return Path.home() / ".corlinman" / "home_channels.sqlite"
    except (RuntimeError, OSError):
        return Path(".corlinman") / "home_channels.sqlite"


def resolve_user_id(channel: str, sender: str) -> str:
    """Stable per-channel user id for ``home_channels.user_id``.

    The full :class:`UserIdentityResolver` graph is overkill here —
    we only need a single deterministic key per (channel, sender)
    pair so a follow-up ``/sethome`` from the same human on the same
    channel overwrites the previous row. Returns ``"<channel>:<sender>"``.
    Identity unification (across-channel merging) is a separate
    concern handled by ``corlinman-identity`` and is not what
    ``/sethome`` is targeting today.
    """
    return f"{channel}:{sender}"


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------


def _connect(db_path: Path | None) -> sqlite3.Connection:
    """Open a fresh sqlite3 connection, applying the schema on first touch.

    WAL is enabled so the rare "lifespan startup writer + chat-bootstrap
    reader" race doesn't block either side. ``synchronous=NORMAL`` matches
    the identity / inbox stores — durability is fine for the kind of
    operator-facing data this table holds.
    """
    path = db_path or default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(_SCHEMA)
    except Exception:
        conn.close()
        raise
    return conn


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def set_home(
    user_id: str,
    *,
    channel: str,
    account: str,
    thread: str,
    sender: str,
    db_path: Path | None = None,
    now_ms: int | None = None,
) -> None:
    """Record the user's current binding as their home channel.

    Idempotent on ``user_id`` — re-issuing ``/sethome`` from another
    binding silently replaces the old row. Stamps ``set_at_ms`` with
    the wall clock so operators can audit recent reassignments.
    """
    ts = now_ms if now_ms is not None else int(time.time() * 1000)
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO home_channels
                (user_id, channel, account, thread, sender, set_at_ms)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                channel   = excluded.channel,
                account   = excluded.account,
                thread    = excluded.thread,
                sender    = excluded.sender,
                set_at_ms = excluded.set_at_ms
            """,
            (user_id, channel, account, thread, sender, ts),
        )
    finally:
        conn.close()


def get_home(
    user_id: str,
    *,
    db_path: Path | None = None,
) -> HomeChannelRow | None:
    """Return the user's home-channel row, or ``None`` if unset."""
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT user_id, channel, account, thread, sender, set_at_ms
            FROM home_channels
            WHERE user_id = ?
            """,
            (user_id,),
        )
        row = cur.fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return HomeChannelRow(
        user_id=row[0],
        channel=row[1],
        account=row[2],
        thread=row[3],
        sender=row[4],
        set_at_ms=int(row[5]),
    )


def list_all_homes(
    *,
    db_path: Path | None = None,
) -> list[HomeChannelRow]:
    """Snapshot every home-channel row.

    Used by the gateway lifespan's post-startup hook to broadcast
    restart notifications. Returns a list (rather than an iterator)
    so callers can close the connection before fanning out the sends.
    """
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT user_id, channel, account, thread, sender, set_at_ms
            FROM home_channels
            ORDER BY set_at_ms ASC
            """
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return [
        HomeChannelRow(
            user_id=r[0],
            channel=r[1],
            account=r[2],
            thread=r[3],
            sender=r[4],
            set_at_ms=int(r[5]),
        )
        for r in rows
    ]


def mark_tip_shown(
    user_id: str,
    channel: str,
    thread: str,
    *,
    db_path: Path | None = None,
    now_ms: int | None = None,
) -> None:
    """Record that the one-time ``/sethome`` tip has been shown.

    Idempotent: re-marking the same triple is a no-op. The
    ``shown_at_ms`` column is preserved on re-write (no UPSERT bump)
    so the first display time is what audits see.
    """
    ts = now_ms if now_ms is not None else int(time.time() * 1000)
    conn = _connect(db_path)
    try:
        conn.execute(
            """
            INSERT INTO first_chat_tips_shown
                (user_id, channel, thread, shown_at_ms)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, channel, thread) DO NOTHING
            """,
            (user_id, channel, thread, ts),
        )
    finally:
        conn.close()


def was_tip_shown(
    user_id: str,
    channel: str,
    thread: str,
    *,
    db_path: Path | None = None,
) -> bool:
    """``True`` when this user has already seen the tip in this thread."""
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            """
            SELECT 1 FROM first_chat_tips_shown
            WHERE user_id = ? AND channel = ? AND thread = ?
            LIMIT 1
            """,
            (user_id, channel, thread),
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


# Keep ``Iterable`` reachable for future bulk-set / bulk-export
# helpers — declared here so static analysers don't flag the import as
# unused while the public surface stays per-row today.
_ = Iterable
