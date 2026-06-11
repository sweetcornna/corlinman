"""Per-binding conversation preferences store (/new, /model across channels).

Companion to :mod:`corlinman_server.home_channel_store` — same connection
model (pure stdlib :mod:`sqlite3`, one short connection per call, sync API
so both async channel handlers and sync chat-assembly paths can use it),
same path convention (``<data_dir>/binding_prefs.sqlite``).

One row per :class:`~corlinman_channels.common.ChannelBinding` four-tuple:

* ``model_override`` — the model id/alias the user picked via ``/model``
  on that conversation; ``NULL`` means "use the deployment default".
* ``session_epoch`` — monotonically increasing counter bumped by ``/new``.
  The channel request builders fold a non-zero epoch into the derived
  session key (``{base}:e{epoch}``), which gives the user a fresh
  conversation context without deleting any journaled history — old
  epochs stay addressable for audit/resume.

The store is deliberately schema-minimal; per-binding settings that later
waves need (permission mode, persona pin, …) are added as columns here so
every channel keeps a single prefs row per conversation.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "BindingPrefs",
    "bump_session_epoch",
    "default_db_path",
    "get_prefs",
    "set_model_override",
]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS binding_prefs (
    binding_key    TEXT PRIMARY KEY,
    channel        TEXT NOT NULL,
    account        TEXT NOT NULL,
    thread         TEXT NOT NULL,
    sender         TEXT NOT NULL,
    model_override TEXT,
    session_epoch  INTEGER NOT NULL DEFAULT 0,
    updated_at_ms  INTEGER NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class BindingPrefs:
    """Current preferences for one conversation binding."""

    model_override: str | None = None
    session_epoch: int = 0


def default_db_path() -> Path:
    """``<data_dir>/binding_prefs.sqlite`` — same resolution order as
    :func:`corlinman_server.home_channel_store.default_db_path`."""
    env = os.environ.get("CORLINMAN_DATA_DIR")
    if env:
        return Path(env) / "binding_prefs.sqlite"
    home = os.environ.get("HOME")
    if home:
        return Path(home) / ".corlinman" / "binding_prefs.sqlite"
    return Path(".corlinman") / "binding_prefs.sqlite"


def _binding_key(channel: str, account: str, thread: str, sender: str) -> str:
    return f"{channel}|{account}|{thread}|{sender}"


def _connect(db_path: Path | None) -> sqlite3.Connection:
    path = db_path or default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5.0)
    conn.executescript(_SCHEMA)
    return conn


def get_prefs(
    channel: str,
    account: str,
    thread: str,
    sender: str,
    *,
    db_path: Path | None = None,
) -> BindingPrefs:
    """Read the prefs row for a binding; absent row ⇒ defaults."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT model_override, session_epoch FROM binding_prefs "
            "WHERE binding_key = ?",
            (_binding_key(channel, account, thread, sender),),
        ).fetchone()
    if row is None:
        return BindingPrefs()
    model_override = row[0] if isinstance(row[0], str) and row[0] else None
    return BindingPrefs(
        model_override=model_override,
        session_epoch=int(row[1] or 0),
    )


def set_model_override(
    channel: str,
    account: str,
    thread: str,
    sender: str,
    model: str | None,
    *,
    db_path: Path | None = None,
) -> BindingPrefs:
    """Set (or clear, with ``None``) the per-binding model override.

    The write is a single atomic upsert that touches ONLY the model
    column — a concurrent ``/new`` epoch bump can interleave at any
    point without either update being lost (the old read-modify-write
    shape could drop one of two racing writes).
    """
    key = _binding_key(channel, account, thread, sender)
    now_ms = int(time.time() * 1000)
    clean = model if isinstance(model, str) and model else None
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO binding_prefs "
            "(binding_key, channel, account, thread, sender, "
            " model_override, session_epoch, updated_at_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?) "
            "ON CONFLICT(binding_key) DO UPDATE SET "
            " model_override = excluded.model_override, "
            " updated_at_ms = excluded.updated_at_ms",
            (key, channel, account, thread, sender, clean, now_ms),
        )
        row = conn.execute(
            "SELECT model_override, session_epoch FROM binding_prefs "
            "WHERE binding_key = ?",
            (key,),
        ).fetchone()
    return BindingPrefs(
        model_override=row[0] if row and isinstance(row[0], str) and row[0] else None,
        session_epoch=int(row[1] or 0) if row else 0,
    )


def bump_session_epoch(
    channel: str,
    account: str,
    thread: str,
    sender: str,
    *,
    db_path: Path | None = None,
) -> BindingPrefs:
    """``/new`` — advance the conversation epoch by one, atomically.

    ``session_epoch = binding_prefs.session_epoch + 1`` runs inside the
    upsert itself, so two near-simultaneous ``/new`` commands yield
    epochs N+1 and N+2 (never both landing on N+1), and the model
    column is never touched.
    """
    key = _binding_key(channel, account, thread, sender)
    now_ms = int(time.time() * 1000)
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO binding_prefs "
            "(binding_key, channel, account, thread, sender, "
            " model_override, session_epoch, updated_at_ms) "
            "VALUES (?, ?, ?, ?, ?, NULL, 1, ?) "
            "ON CONFLICT(binding_key) DO UPDATE SET "
            " session_epoch = binding_prefs.session_epoch + 1, "
            " updated_at_ms = excluded.updated_at_ms",
            (key, channel, account, thread, sender, now_ms),
        )
        row = conn.execute(
            "SELECT model_override, session_epoch FROM binding_prefs "
            "WHERE binding_key = ?",
            (key,),
        ).fetchone()
    return BindingPrefs(
        model_override=row[0] if row and isinstance(row[0], str) and row[0] else None,
        session_epoch=int(row[1] or 0) if row else 0,
    )


