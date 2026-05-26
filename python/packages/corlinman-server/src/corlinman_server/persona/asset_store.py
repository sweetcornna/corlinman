"""Per-persona asset registry: emoji packs + reference images.

Backs the Persona Studio (see ``docs/PLAN_PERSONA_STUDIO.md`` W1).
Each persona owns two asset buckets:

* ``emoji`` — labelled stickers / facial-expression images the agent
  attaches via ``send_attachment`` to add character flavour to replies.
* ``reference`` — labelled立绘 views (front / side / casual / …) that
  the ``image_with_refs`` tool passes to the image-generation provider
  as character refs for QZone 说说 illustration.

Storage layout (sqlite metadata + filesystem blobs):

* SQLite table ``persona_assets`` keyed by ulid, with the persona id,
  bucket kind, label, original file name, mime, size, sha256 and
  create-time. ``(persona_id, kind, label)`` is a hard unique key so a
  re-upload to the same slot is treated as a replacement.
* Blob file lives at::

      <base>/personas/<persona_id>/<kind>/<sha256>.<ext>

  Filename keyed by sha256 so two emojis with identical bytes share
  one inode (cheap dedup) and ETag-based HTTP caching is trivial.

Caps (locked in PLAN W1 / 2026-05-26):

* 8 MiB per asset — comfortably fits Telegram document + NapCat upload
  + Discord/Slack/Feishu attach limits.
* 200 MiB per persona — operator can grow this via env override.
* Only ``image/png`` ``image/jpeg`` ``image/webp`` ``image/gif`` MIME
  types are accepted on write. Other types raise
  :class:`AssetMimeRejected`.
"""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import aiosqlite
import structlog

logger = structlog.get_logger(__name__)


__all__ = [
    "ALLOWED_MIMES",
    "AssetKind",
    "AssetMimeRejected",
    "AssetNotFound",
    "AssetQuotaExceeded",
    "AssetRecord",
    "AssetTooLarge",
    "PersonaAssetStore",
    "DEFAULT_MAX_BYTES_PER_ASSET",
    "DEFAULT_MAX_BYTES_PER_PERSONA",
]


#: Bucket discriminant. ``emoji`` rides via ``send_attachment``;
#: ``reference`` feeds the image-generation provider as character refs.
AssetKind = Literal["emoji", "reference"]


#: MIME allowlist. Channels render images natively; PNG / JPEG / WEBP
#: cover the vast majority of bot-friendly emoji + 立绘 packs. GIF is
#: kept for animated emoji (Telegram sticker style); the channels send
#: it as a document, not an animation, so size discipline still applies.
ALLOWED_MIMES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif"}
)


#: Hard cap on a single asset's byte size. Defaults to the value locked
#: in the PLAN; an operator who needs higher-resolution立绘 can override
#: with ``CORLINMAN_PERSONA_MAX_ASSET_BYTES``.
DEFAULT_MAX_BYTES_PER_ASSET: int = 8 * 1024 * 1024

#: Hard cap on total bytes a single persona's asset bucket can hold.
#: Override with ``CORLINMAN_PERSONA_MAX_BYTES_PER_PERSONA``.
DEFAULT_MAX_BYTES_PER_PERSONA: int = 200 * 1024 * 1024


_SCHEMA: str = """
CREATE TABLE IF NOT EXISTS persona_assets (
    id            TEXT PRIMARY KEY,
    persona_id    TEXT NOT NULL,
    kind          TEXT NOT NULL,
    label         TEXT NOT NULL,
    file_name     TEXT NOT NULL,
    mime          TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    sha256        TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    UNIQUE(persona_id, kind, label)
);
CREATE INDEX IF NOT EXISTS idx_persona_assets_persona
    ON persona_assets(persona_id);
CREATE INDEX IF NOT EXISTS idx_persona_assets_kind
    ON persona_assets(persona_id, kind);
"""


@dataclass(frozen=True)
class AssetRecord:
    """Read view of one ``persona_assets`` row.

    ``label`` is the human-meaningful slot name within a persona's
    bucket (``happy`` / ``angry`` for emoji; ``front`` / ``casual``
    for reference). ``sha256`` doubles as the on-disk filename stem
    and as the HTTP ``ETag`` value when the asset is served.
    """

    id: str
    persona_id: str
    kind: AssetKind
    label: str
    file_name: str
    mime: str
    size_bytes: int
    sha256: str
    created_at_ms: int


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AssetError(Exception):
    """Base class for asset-store domain errors. Route layer maps each
    subclass to an HTTP status; callers pattern-match on the type."""


class AssetMimeRejected(AssetError):
    """Caller submitted a MIME outside :data:`ALLOWED_MIMES`."""


class AssetTooLarge(AssetError):
    """Single asset's byte size exceeds the per-asset cap."""


class AssetQuotaExceeded(AssetError):
    """Per-persona quota would be exceeded by this write."""


class AssetNotFound(AssetError):
    """Requested asset id / (persona, kind, label) triple is absent."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(time.time() * 1000)


def _ulid() -> str:
    """Cheap monotonic-ish id. We use uuid4 hex truncated to 26 chars —
    not a real ULID but the same shape (lex-sortable enough for asset
    listing isn't required since we sort by created_at_ms)."""
    return uuid.uuid4().hex[:26]


def _ext_for_mime(mime: str) -> str:
    """Map MIME → filename extension. Asset files are keyed by sha256
    on disk; the extension is purely cosmetic but kept so a manual
    inspector ("what's in this dir?") sees something parseable."""
    return {
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/webp": "webp",
        "image/gif": "gif",
    }.get(mime, "bin")


def _resolve_caps() -> tuple[int, int]:
    """Read per-asset / per-persona caps, allowing env overrides for
    operators that need bigger 立绘 packs."""
    raw_a = os.environ.get("CORLINMAN_PERSONA_MAX_ASSET_BYTES")
    raw_p = os.environ.get("CORLINMAN_PERSONA_MAX_BYTES_PER_PERSONA")
    try:
        per_asset = int(raw_a) if raw_a else DEFAULT_MAX_BYTES_PER_ASSET
    except ValueError:
        per_asset = DEFAULT_MAX_BYTES_PER_ASSET
    try:
        per_persona = int(raw_p) if raw_p else DEFAULT_MAX_BYTES_PER_PERSONA
    except ValueError:
        per_persona = DEFAULT_MAX_BYTES_PER_PERSONA
    return per_asset, per_persona


def _row_to_record(row: aiosqlite.Row) -> AssetRecord:
    return AssetRecord(
        id=row["id"],
        persona_id=row["persona_id"],
        kind=row["kind"],  # type: ignore[arg-type]
        label=row["label"],
        file_name=row["file_name"],
        mime=row["mime"],
        size_bytes=int(row["size_bytes"]),
        sha256=row["sha256"],
        created_at_ms=int(row["created_at_ms"]),
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class PersonaAssetStore:
    """Async-SQLite + filesystem store for persona asset blobs.

    Lifecycle mirrors :class:`PersonaStore`: open at boot via
    :meth:`open`, close at shutdown via :meth:`close`. The store owns
    its sqlite connection and the on-disk base directory it scopes
    blobs into; both are idempotent.

    Concurrency: SQLite serialises writes; the filesystem write is
    keyed by content hash so concurrent uploads of the same bytes
    converge on the same file. We do not need a per-row lock.
    """

    __slots__ = (
        "_base",
        "_conn",
        "_max_per_asset",
        "_max_per_persona",
        "_path",
    )

    def __init__(
        self,
        sqlite_path: Path,
        base_dir: Path,
        *,
        max_bytes_per_asset: int | None = None,
        max_bytes_per_persona: int | None = None,
    ) -> None:
        self._path = sqlite_path
        self._base = base_dir
        self._conn: aiosqlite.Connection | None = None
        env_asset, env_persona = _resolve_caps()
        self._max_per_asset = (
            max_bytes_per_asset
            if max_bytes_per_asset is not None
            else env_asset
        )
        self._max_per_persona = (
            max_bytes_per_persona
            if max_bytes_per_persona is not None
            else env_persona
        )

    @classmethod
    async def open(
        cls,
        sqlite_path: Path,
        base_dir: Path,
        **kwargs: int | None,
    ) -> PersonaAssetStore:
        store = cls(sqlite_path, base_dir, **kwargs)
        await store._open()
        return store

    async def _open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._base.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self._path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode = WAL")
        await conn.execute("PRAGMA synchronous = NORMAL")
        await conn.execute("PRAGMA busy_timeout = 5000")
        await conn.executescript(_SCHEMA)
        await conn.commit()
        self._conn = conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def _c(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError(
                "PersonaAssetStore not opened — call await "
                "PersonaAssetStore.open(...) first"
            )
        return self._conn

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _blob_dir(self, persona_id: str, kind: AssetKind) -> Path:
        return self._base / persona_id / kind

    def _blob_path(self, record: AssetRecord) -> Path:
        ext = _ext_for_mime(record.mime)
        return (
            self._blob_dir(record.persona_id, record.kind)
            / f"{record.sha256}.{ext}"
        )

    def path_for(self, record: AssetRecord) -> Path:
        """Public alias of :meth:`_blob_path` — kept here so callers
        outside the module don't need to reach for the underscore name.
        Used by ``send_attachment`` resolvers + the W4
        ``image_with_refs`` tool to read asset bytes off disk."""
        return self._blob_path(record)

    # ------------------------------------------------------------------
    # Quota check
    # ------------------------------------------------------------------

    async def used_bytes(self, persona_id: str) -> int:
        """Sum of ``size_bytes`` for all assets of one persona. Used by
        :meth:`put` to enforce the per-persona cap *before* writing to
        disk so a partial write can never push the bucket over."""
        async with self._c.execute(
            "SELECT COALESCE(SUM(size_bytes), 0) FROM persona_assets "
            "WHERE persona_id = ?",
            (persona_id,),
        ) as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # Write paths
    # ------------------------------------------------------------------

    async def put(
        self,
        persona_id: str,
        kind: AssetKind,
        label: str,
        *,
        bytes_: bytes,
        mime: str,
        file_name: str,
    ) -> AssetRecord:
        """Upload one asset, replacing any existing slot at
        ``(persona_id, kind, label)``.

        Raises:
          * :class:`AssetMimeRejected` if ``mime`` is outside
            :data:`ALLOWED_MIMES`.
          * :class:`AssetTooLarge` if ``len(bytes_) > per-asset cap``.
          * :class:`AssetQuotaExceeded` if the write would push the
            persona's bucket past the per-persona cap.

        Returns the freshly persisted :class:`AssetRecord`. Idempotent
        on identical bytes for the same slot — the on-disk file is
        keyed by sha256 so a re-upload of the same content reuses the
        existing blob.
        """
        if mime not in ALLOWED_MIMES:
            raise AssetMimeRejected(
                f"mime {mime!r} not in allowlist; accepted: "
                f"{', '.join(sorted(ALLOWED_MIMES))}"
            )
        if len(bytes_) > self._max_per_asset:
            raise AssetTooLarge(
                f"asset is {len(bytes_)} bytes; cap is "
                f"{self._max_per_asset}"
            )

        # Quota check accounts for the slot we're about to replace: if
        # the same (persona, kind, label) row already exists, its
        # existing size is freed by the upsert and shouldn't count
        # twice toward the cap.
        existing = await self.get(persona_id, kind, label)
        used = await self.used_bytes(persona_id)
        if existing is not None:
            used -= existing.size_bytes
        if used + len(bytes_) > self._max_per_persona:
            raise AssetQuotaExceeded(
                f"persona {persona_id!r} would consume "
                f"{used + len(bytes_)} bytes; cap is "
                f"{self._max_per_persona}"
            )

        digest = hashlib.sha256(bytes_).hexdigest()
        record = AssetRecord(
            id=existing.id if existing is not None else _ulid(),
            persona_id=persona_id,
            kind=kind,
            label=label,
            file_name=file_name,
            mime=mime,
            size_bytes=len(bytes_),
            sha256=digest,
            created_at_ms=existing.created_at_ms if existing is not None
            else _now_ms(),
        )

        # Write the new blob FIRST so a row never points at a missing
        # file. Identical content for the same slot reuses the path.
        blob = self._blob_path(record)
        blob.parent.mkdir(parents=True, exist_ok=True)
        if not blob.exists() or blob.stat().st_size != len(bytes_):
            blob.write_bytes(bytes_)

        # Upsert the metadata row. We don't reuse the row id when the
        # content changes — but we DO reuse it when the row already
        # existed (preserves stable URL ids for cached callers).
        if existing is not None:
            await self._c.execute(
                """
                UPDATE persona_assets
                   SET file_name = ?, mime = ?, size_bytes = ?,
                       sha256 = ?, created_at_ms = ?
                 WHERE id = ?
                """,
                (
                    record.file_name,
                    record.mime,
                    record.size_bytes,
                    record.sha256,
                    record.created_at_ms,
                    record.id,
                ),
            )
            # An UPDATE of an in-place sha256 might orphan the old blob
            # if the bytes actually changed. Sweep below.
            if existing.sha256 != record.sha256:
                old_blob = self._blob_path(existing)
                _safe_unlink(old_blob)
        else:
            await self._c.execute(
                """
                INSERT INTO persona_assets (
                    id, persona_id, kind, label, file_name, mime,
                    size_bytes, sha256, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.persona_id,
                    record.kind,
                    record.label,
                    record.file_name,
                    record.mime,
                    record.size_bytes,
                    record.sha256,
                    record.created_at_ms,
                ),
            )
        await self._c.commit()
        return record

    # ------------------------------------------------------------------
    # Read paths
    # ------------------------------------------------------------------

    async def get(
        self, persona_id: str, kind: AssetKind, label: str
    ) -> AssetRecord | None:
        async with self._c.execute(
            """
            SELECT id, persona_id, kind, label, file_name, mime,
                   size_bytes, sha256, created_at_ms
              FROM persona_assets
             WHERE persona_id = ? AND kind = ? AND label = ?
            """,
            (persona_id, kind, label),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_record(row) if row is not None else None

    async def get_by_id(self, asset_id: str) -> AssetRecord | None:
        async with self._c.execute(
            """
            SELECT id, persona_id, kind, label, file_name, mime,
                   size_bytes, sha256, created_at_ms
              FROM persona_assets
             WHERE id = ?
            """,
            (asset_id,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_record(row) if row is not None else None

    async def list(
        self, persona_id: str, *, kind: AssetKind | None = None
    ) -> list[AssetRecord]:
        if kind is None:
            sql = """
                SELECT id, persona_id, kind, label, file_name, mime,
                       size_bytes, sha256, created_at_ms
                  FROM persona_assets
                 WHERE persona_id = ?
                 ORDER BY kind ASC, label ASC
            """
            params: tuple = (persona_id,)
        else:
            sql = """
                SELECT id, persona_id, kind, label, file_name, mime,
                       size_bytes, sha256, created_at_ms
                  FROM persona_assets
                 WHERE persona_id = ? AND kind = ?
                 ORDER BY label ASC
            """
            params = (persona_id, kind)
        async with self._c.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_row_to_record(r) for r in rows]

    async def read_bytes(self, record: AssetRecord) -> bytes:
        """Read the blob payload off disk. Raises :class:`AssetNotFound`
        if the file vanished out from under the metadata row (rare;
        usually means a manual ``rm`` on the data dir)."""
        path = self._blob_path(record)
        if not path.is_file():
            raise AssetNotFound(
                f"blob missing for asset {record.id!r}: {path}"
            )
        return path.read_bytes()

    # ------------------------------------------------------------------
    # Delete paths
    # ------------------------------------------------------------------

    async def delete(
        self, persona_id: str, kind: AssetKind, label: str
    ) -> bool:
        """Remove one asset slot. Returns True if a row was removed.

        Orphaned blob cleanup: only deletes the on-disk file when no
        other row references the same sha256. This protects shared
        bytes if a future feature lets multiple slots reference one
        underlying blob; today the (persona, kind, label) uniqueness
        means there's exactly one row per blob, but the check costs
        nothing and removes a footgun.
        """
        existing = await self.get(persona_id, kind, label)
        if existing is None:
            return False
        await self._c.execute(
            "DELETE FROM persona_assets WHERE id = ?",
            (existing.id,),
        )
        await self._c.commit()
        await self._sweep_blob_if_unreferenced(existing)
        return True

    async def delete_by_id(self, asset_id: str) -> bool:
        existing = await self.get_by_id(asset_id)
        if existing is None:
            return False
        await self._c.execute(
            "DELETE FROM persona_assets WHERE id = ?",
            (asset_id,),
        )
        await self._c.commit()
        await self._sweep_blob_if_unreferenced(existing)
        return True

    async def delete_all(self, persona_id: str) -> int:
        """Remove every asset belonging to a persona. Returns the count
        of rows deleted. Called from the persona-delete admin route so
        a removed persona leaves no orphaned bytes."""
        rows = await self.list(persona_id)
        if not rows:
            return 0
        await self._c.execute(
            "DELETE FROM persona_assets WHERE persona_id = ?",
            (persona_id,),
        )
        await self._c.commit()
        # Best-effort blob sweep — failure to unlink a single file is
        # not fatal (the next deploy can prune).
        for r in rows:
            _safe_unlink(self._blob_path(r))
        persona_dir = self._base / persona_id
        # Try to remove the now-empty persona dir; safe-ignore if any
        # non-asset content lingered (e.g. user-staged scratch).
        for sub in ("emoji", "reference"):
            d = persona_dir / sub
            if d.is_dir():
                try:
                    d.rmdir()
                except OSError:
                    pass
        try:
            persona_dir.rmdir()
        except OSError:
            pass
        return len(rows)

    async def _sweep_blob_if_unreferenced(self, record: AssetRecord) -> None:
        """Remove ``record``'s on-disk blob if no other metadata row
        points at the same sha256."""
        async with self._c.execute(
            "SELECT COUNT(*) FROM persona_assets WHERE sha256 = ?",
            (record.sha256,),
        ) as cur:
            row = await cur.fetchone()
        refs = int(row[0]) if row else 0
        if refs == 0:
            _safe_unlink(self._blob_path(record))


def _safe_unlink(path: Path) -> None:
    """``Path.unlink(missing_ok=True)`` swallowing PermissionError so
    a cleanup pass on a stuck file doesn't crash the request."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
