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

    __slots__ = ("_entries", "_seen")

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, str]] = {}
        # Paths the agent has observed this turn — read, written, or
        # edited. Distinct from ``_entries`` (the re-read cache) because
        # ``forget`` clears the cache after a write/edit while the agent
        # has still *seen* the file; the read-before-edit guard keys off
        # this set, not the cache.
        self._seen: set[str] = set()

    # ------------------------------------------------------------------
    # Cache surface
    # ------------------------------------------------------------------

    def record_read(self, path: Path, mtime: float, content: str) -> None:
        """Remember the (mtime, content) we just read for ``path``."""
        key = self._key(path)
        self._entries[key] = (float(mtime), content)
        self._seen.add(key)

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

    def mark_seen(self, path: Path) -> None:
        """Record that the agent has observed ``path`` this turn without
        populating the re-read cache.

        Call after a write/edit (alongside :meth:`forget`): the agent
        produced these bytes, so a follow-up edit to the same file is
        legitimate even though the cache entry was just dropped. Backs
        the read-before-edit guard.
        """
        self._seen.add(self._key(path))

    def was_seen(self, path: Path) -> bool:
        """True iff the agent has read, written, or edited ``path`` this
        turn. The read-before-edit guard rejects an edit to an existing
        file for which this is ``False``."""
        return self._key(path) in self._seen

    def forget(self, path: Path) -> None:
        """Drop the re-read cache entry for ``path`` — call after a
        write/edit so the next read re-fetches and re-pins the new mtime.

        Leaves :meth:`was_seen` intact (the agent has still seen the
        file), so pair it with :meth:`mark_seen` on the write/edit path.
        """
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
