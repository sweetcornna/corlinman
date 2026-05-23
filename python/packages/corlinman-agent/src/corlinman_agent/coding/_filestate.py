"""Per-RPC file-read state — small cache + staleness tracker.

One :class:`FileState` is created at the start of each agent ``Chat`` RPC
and threaded into the file-tool dispatch calls. It does two jobs:

1. **Cheap re-read cache.** If the model reads the same file twice in
   one turn (very common during planning/verification) we skip the disk
   hit when the file's mtime is unchanged.
2. **Staleness tracker for edits.** Records the mtime observed at read
   time so a follow-up tool (T2.2's fuzzy edit) can refuse an edit
   when the file changed under the agent.

Per-RPC — instances are scoped to one chat turn. No locks; one event
loop owns the instance for its lifetime.
"""

from __future__ import annotations

from pathlib import Path


class FileState:
    """In-memory ``(path → (mtime, content))`` map for one chat turn.

    All paths are stored under their resolved absolute string form so
    relative / symlink variants land on the same key.
    """

    __slots__ = ("_entries",)

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, str]] = {}

    # ------------------------------------------------------------------
    # Cache surface
    # ------------------------------------------------------------------

    def record_read(self, path: Path, mtime: float, content: str) -> None:
        """Remember the (mtime, content) we just read for ``path``."""
        self._entries[self._key(path)] = (float(mtime), content)

    def cached_read(self, path: Path) -> str | None:
        """Return the cached content iff the file's current mtime matches.

        A missing/unstat-able file is a cache miss (returns ``None``); we
        do not surface filesystem errors here — callers handle them on
        the real read path.
        """
        entry = self._entries.get(self._key(path))
        if entry is None:
            return None
        recorded_mtime, content = entry
        try:
            current_mtime = path.stat().st_mtime
        except OSError:
            return None
        if current_mtime != recorded_mtime:
            return None
        return content

    # ------------------------------------------------------------------
    # Staleness — T2.2 hook
    # ------------------------------------------------------------------

    def is_stale(self, path: Path) -> bool:
        """True iff we have a record for ``path`` AND its mtime moved.

        A path with no record is **not** stale — we have nothing to
        compare against, so the edit goes through. Unreadable / missing
        files are not stale either (a write to a missing file is a
        create, which is fine).
        """
        entry = self._entries.get(self._key(path))
        if entry is None:
            return False
        recorded_mtime, _ = entry
        try:
            current_mtime = path.stat().st_mtime
        except OSError:
            return False
        return current_mtime != recorded_mtime

    def forget(self, path: Path) -> None:
        """Drop any record for ``path`` — call after a write/edit so the
        next read re-fetches and re-pins the new mtime."""
        self._entries.pop(self._key(path), None)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _key(path: Path) -> str:
        try:
            return str(path.resolve())
        except OSError:
            return str(path)


__all__ = ["FileState"]
