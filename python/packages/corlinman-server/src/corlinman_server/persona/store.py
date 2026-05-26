"""SQLite-backed registry of human-like personas.

A *persona* is a named system_prompt block injected at the head of the
chat request when a channel's ``humanlike`` toggle is on. The store is
deliberately tiny — one table, no relations, no per-row settings beyond
the markdown body — so it can grow alongside whatever the operator
needs without schema churn.

Scope today: QQ channel only consults the store, via
:class:`corlinman_channels.QqChannelParams`. The store API is channel-
agnostic so Telegram / Discord / Slack can opt in later by reading the
same store and the same ``persona_id`` field. No persona schema change
is needed when a new channel hooks up.

Built-in protection
-------------------

The store ships a single seeded persona — the ``grantley`` row, see
:mod:`corlinman_server.persona.default_grantley`. Its row is flagged
``is_builtin = 1``; that flag is read-only after seeding:

* :meth:`create` refuses ``is_builtin=True`` from API callers — only
  :func:`seed_builtin_personas` (which goes through the same SQL with
  the bypass set) can stamp it.
* :meth:`update` preserves the flag across patches; admins can re-write
  the body of a builtin but cannot clear / set the flag itself.
* :meth:`delete` refuses to remove a builtin row — the admin must
  re-seed or rewrite the body via :meth:`update` instead.

WAL + foreign_keys ON, same as the rest of the corlinman SQLite stores.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import aiosqlite
import structlog

logger = structlog.get_logger(__name__)


_SCHEMA: str = """
CREATE TABLE IF NOT EXISTS personas (
    id            TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    short_summary TEXT NOT NULL DEFAULT '',
    system_prompt TEXT NOT NULL,
    is_builtin    INTEGER NOT NULL DEFAULT 0,
    owner_user_id TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_personas_updated_at
    ON personas(updated_at_ms);
"""


async def _migrate_personas_table(conn: aiosqlite.Connection) -> None:
    """Idempotent migration: add ``owner_user_id`` column (+ index) to
    pre-W1 schemas. SQLite has no ``ADD COLUMN IF NOT EXISTS``; we
    PRAGMA-check first and ALTER only when missing.

    *Why the index lives here and not in ``_SCHEMA``*: an
    ``IF NOT EXISTS`` index on a column the legacy table doesn't yet
    have (``owner_user_id``) makes ``executescript(_SCHEMA)`` raise
    ``no such column`` on existing DBs. The schema script only
    references columns that exist in the legacy shape; the migration
    adds the new column AND its index in one step so the two never
    drift apart.

    Field rationale: nullable for forward-compatibility with the
    Persona Studio auth migration (see PLAN_PERSONA_STUDIO.md W1).
    Today no callers populate it; the column rides quietly so a future
    auth-enforced migration is a one-line UPDATE.
    """
    async with conn.execute("PRAGMA table_info(personas)") as cur:
        cols = {row[1] for row in await cur.fetchall()}
    if "owner_user_id" not in cols:
        await conn.execute("ALTER TABLE personas ADD COLUMN owner_user_id TEXT")
    # Index creation is idempotent and cheap; run unconditionally so a
    # half-migrated DB (column present, index missing) self-heals.
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_personas_owner "
        "ON personas(owner_user_id)"
    )
    await conn.commit()


@dataclass(frozen=True)
class Persona:
    """Read view of one persona row.

    Frozen so call sites cannot accidentally mutate the cached object and
    end up writing back a stale row. The store always re-fetches the row
    after a mutation so the returned :class:`Persona` reflects the post-
    write state including server-assigned ``updated_at_ms``.
    """

    id: str
    display_name: str
    short_summary: str
    system_prompt: str
    is_builtin: bool
    created_at_ms: int
    updated_at_ms: int
    owner_user_id: str | None = None


# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------


class PersonaError(Exception):
    """Base class for persona-store domain errors. Route layer maps each
    subclass to an HTTP status; callers pattern-match on the type."""


class PersonaExists(PersonaError):
    """:meth:`PersonaStore.create` was called with a duplicate ``id``."""


class PersonaProtected(PersonaError):
    """Attempted to delete a row flagged ``is_builtin``, or to set
    ``is_builtin`` from outside :func:`seed_builtin_personas`."""


def _row_to_persona(row: aiosqlite.Row) -> Persona:
    # ``owner_user_id`` may be absent on a row materialised before the
    # W1 migration ran (rare — the migration runs at open()) so use
    # ``.keys()`` to probe; fall back to None for the legacy shape.
    owner = row["owner_user_id"] if "owner_user_id" in row.keys() else None
    return Persona(
        id=row["id"],
        display_name=row["display_name"],
        short_summary=row["short_summary"],
        system_prompt=row["system_prompt"],
        is_builtin=bool(row["is_builtin"]),
        created_at_ms=int(row["created_at_ms"]),
        updated_at_ms=int(row["updated_at_ms"]),
        owner_user_id=owner,
    )


def _now_ms() -> int:
    return int(time.time() * 1000)


class PersonaStore:
    """Async SQLite-backed persona registry.

    Lifecycle: construct via :meth:`open` (which executes the schema
    DDL); call :meth:`close` at shutdown. The store holds a single
    :class:`aiosqlite.Connection` — SQLite serialises writes internally
    so a single connection is sufficient for the admin-write + per-turn-
    read load pattern of this surface.
    """

    __slots__ = ("_path", "_conn")

    def __init__(self, path: Path) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    @classmethod
    async def open(cls, path: Path) -> "PersonaStore":
        """Open the store at ``path``, creating the file + schema if
        necessary. Mirrors :meth:`corlinman_server.inbox.Inbox.open` so
        the call sites read identically across stores."""
        store = cls(path)
        await store._open()
        return store

    async def _open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self._path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA synchronous = NORMAL")
        await conn.execute("PRAGMA busy_timeout = 5000")
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.executescript(_SCHEMA)
        await conn.commit()
        await _migrate_personas_table(conn)
        self._conn = conn

    async def close(self) -> None:
        """Idempotent close. Safe to call multiple times — the second
        call is a no-op."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _c(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError(
                "PersonaStore not opened — call await PersonaStore.open(...) "
                "first"
            )
        return self._conn

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------

    async def list(self) -> list[Persona]:
        """Return every persona, builtins first, then operator-created
        rows sorted by ``updated_at_ms`` descending.

        Mirrors the admin-UI expectation of "stable canonical personas
        on top, recently-edited custom ones next" so picker order does
        not jitter when an admin patches a row.
        """
        async with self._c.execute(
            """
            SELECT id, display_name, short_summary, system_prompt,
                   is_builtin, owner_user_id, created_at_ms, updated_at_ms
              FROM personas
             ORDER BY is_builtin DESC, updated_at_ms DESC, id ASC
            """,
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_persona(r) for r in rows]

    async def get(self, persona_id: str) -> Persona | None:
        """Return one persona or ``None`` if the row is absent."""
        async with self._c.execute(
            """
            SELECT id, display_name, short_summary, system_prompt,
                   is_builtin, owner_user_id, created_at_ms, updated_at_ms
              FROM personas
             WHERE id = ?
            """,
            (persona_id,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_persona(row) if row is not None else None

    # ------------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------------

    async def create(self, persona: Persona) -> Persona:
        """Insert a new persona row.

        Raises:
          * :class:`PersonaExists` if ``persona.id`` already exists.
          * :class:`PersonaProtected` if the caller tried to set
            ``is_builtin=True`` from outside :func:`seed_builtin_personas`.

        ``created_at_ms`` / ``updated_at_ms`` are server-assigned;
        whatever the caller passed in is overwritten. Returns the freshly
        inserted row (re-fetched so the timestamps reflect the actual
        store state).
        """
        if persona.is_builtin:
            raise PersonaProtected(
                "is_builtin is read-only; use seed_builtin_personas() "
                "to stamp builtin rows"
            )
        return await self._insert(persona, builtin=False)

    async def _insert(self, persona: Persona, *, builtin: bool) -> Persona:
        """Internal: shared insert path for :meth:`create` and the seeder.

        The split lets :func:`seed_builtin_personas` set ``is_builtin=1``
        without granting that capability to public callers.
        """
        now = _now_ms()
        try:
            await self._c.execute(
                """
                INSERT INTO personas (
                    id, display_name, short_summary, system_prompt,
                    is_builtin, owner_user_id, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    persona.id,
                    persona.display_name,
                    persona.short_summary,
                    persona.system_prompt,
                    1 if builtin else 0,
                    persona.owner_user_id,
                    now,
                    now,
                ),
            )
            await self._c.commit()
        except aiosqlite.IntegrityError as exc:
            # Primary-key collision — the only IntegrityError this table
            # can throw on insert (no FKs, no NOT NULL violations the
            # caller can trigger from outside the type system).
            raise PersonaExists(
                f"persona id already exists: {persona.id!r}"
            ) from exc

        row = await self.get(persona.id)
        # ``row`` is None only if a concurrent DELETE landed between
        # INSERT + SELECT — extremely unlikely in practice; the route
        # layer 500s on that path which is the right thing to do.
        assert row is not None
        return row

    async def update(
        self,
        persona_id: str,
        *,
        display_name: str | None = None,
        short_summary: str | None = None,
        system_prompt: str | None = None,
    ) -> Persona:
        """Patch a persona row. All keyword args are optional — missing
        fields are preserved verbatim. Returns the post-write row.

        Preserves ``is_builtin`` across the patch — the admin UI lets
        operators edit the body of a builtin persona but cannot change
        the flag itself.

        Raises :class:`PersonaError` (the bare base) when the row does
        not exist; the route layer maps this to 404. We intentionally
        do not introduce a dedicated ``PersonaNotFound`` subclass — the
        route layer already checks for ``None`` from :meth:`get` for the
        same purpose and the symmetry keeps the surface narrow.
        """
        existing = await self.get(persona_id)
        if existing is None:
            raise PersonaError(f"persona not found: {persona_id!r}")

        new_display = (
            display_name if display_name is not None else existing.display_name
        )
        new_summary = (
            short_summary
            if short_summary is not None
            else existing.short_summary
        )
        new_prompt = (
            system_prompt
            if system_prompt is not None
            else existing.system_prompt
        )

        now = _now_ms()
        await self._c.execute(
            """
            UPDATE personas
               SET display_name  = ?,
                   short_summary = ?,
                   system_prompt = ?,
                   updated_at_ms = ?
             WHERE id = ?
            """,
            (new_display, new_summary, new_prompt, now, persona_id),
        )
        await self._c.commit()

        row = await self.get(persona_id)
        assert row is not None
        return row

    async def delete(self, persona_id: str) -> bool:
        """Remove one persona row.

        Returns ``True`` if a row was deleted, ``False`` if no row with
        that id existed in the first place.

        Raises :class:`PersonaProtected` when the target row is flagged
        ``is_builtin``. The admin UI is expected to hide / grey-out the
        delete affordance on builtin rows; the 404 vs 409 split lets
        operators that go around the UI still get a clear error.
        """
        existing = await self.get(persona_id)
        if existing is None:
            return False
        if existing.is_builtin:
            raise PersonaProtected(
                f"cannot delete builtin persona {persona_id!r}; "
                "rewrite the body via update() if you want to neuter it"
            )

        await self._c.execute(
            "DELETE FROM personas WHERE id = ?",
            (persona_id,),
        )
        await self._c.commit()
        return True


async def seed_builtin_personas(store: PersonaStore) -> None:
    """Idempotent: ensure every built-in persona exists in ``store``.

    Called once during gateway boot (after :meth:`PersonaStore.open`).
    Re-runs on every boot are a no-op for already-seeded rows; an admin
    who edited a builtin row via :meth:`update` keeps their edits
    (we never overwrite an existing row).

    Currently seeds the single ``grantley`` persona (see
    :mod:`corlinman_server.persona.default_grantley`). Future builtins
    plug in by appending an entry here — the surface intentionally
    favours code-side declaration over a JSON config file so the
    builtin set is reviewable in git.
    """
    # Lazy-import the body module so test harnesses that swap in a
    # stripped-down sibling module still work — importing through the
    # package ``__init__`` would force-load this seed function too.
    from corlinman_server.persona.default_grantley import (
        DEFAULT_GRANTLEY_DISPLAY_NAME,
        DEFAULT_GRANTLEY_ID,
        DEFAULT_GRANTLEY_SUMMARY,
        load_default_grantley_body,
    )

    existing = await store.get(DEFAULT_GRANTLEY_ID)
    if existing is not None:
        # Idempotent: leave existing rows alone so operator edits stick.
        # An admin who replaced the grantley body via /admin/personas
        # PATCH keeps their replacement; the seeder only fills gaps.
        return

    body = load_default_grantley_body()
    persona = Persona(
        id=DEFAULT_GRANTLEY_ID,
        display_name=DEFAULT_GRANTLEY_DISPLAY_NAME,
        short_summary=DEFAULT_GRANTLEY_SUMMARY,
        system_prompt=body,
        # Timestamps overwritten by the insert path; pass 0 for clarity.
        is_builtin=True,
        created_at_ms=0,
        updated_at_ms=0,
    )
    await store._insert(persona, builtin=True)
    logger.info(
        "persona.seed.inserted",
        persona_id=DEFAULT_GRANTLEY_ID,
        body_chars=len(body),
    )


__all__ = [
    "Persona",
    "PersonaError",
    "PersonaExists",
    "PersonaProtected",
    "PersonaStore",
    "seed_builtin_personas",
]
