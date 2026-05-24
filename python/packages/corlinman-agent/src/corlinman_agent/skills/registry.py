"""In-memory skill registry — Python mirror of the Rust
``corlinman-skills`` crate's ``SkillRegistry``.

The loader walks a directory tree looking for ``*.md`` files, splits
each on the ``---`` YAML frontmatter fence, parses the frontmatter, and
keeps the Markdown body verbatim. Duplicate ``name`` fields across
files are a hard error — two skills cannot share an identifier.

The ``check_requirements`` path is deliberately synchronous and cheap:
the context assembler calls it once per skill-ref during every prompt
assembly, so any dependency on async I/O here would fan out into every
request.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml  # type: ignore[import-untyped]

from corlinman_agent.skills.card import Skill, SkillRequirements

_log = structlog.get_logger(__name__)


#: How long :meth:`SkillRegistry.refresh` waits between real disk scans
#: by default. The chat handler calls ``refresh()`` at every turn
#: boundary, which on a hot session was costing ~5-10ms / turn in
#: ``rglob`` + ``stat()`` work for a directory that almost never changes
#: between adjacent turns. 30 seconds is well below the operator
#: "I dropped a new SKILL.md in" expectation (they almost always pause
#: at least that long before their next chat) and well above the
#: per-turn cadence. Operators can override via
#: ``CORLINMAN_SKILL_REFRESH_INTERVAL_MS`` (set 0 to disable debounce
#: entirely; tests pass ``force=True`` for the same effect).
_DEFAULT_SKILL_REFRESH_INTERVAL_MS: int = 30_000


def _default_refresh_interval_ms() -> int:
    raw = os.environ.get("CORLINMAN_SKILL_REFRESH_INTERVAL_MS")
    if raw is None or raw == "":
        return _DEFAULT_SKILL_REFRESH_INTERVAL_MS
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_SKILL_REFRESH_INTERVAL_MS


@dataclass(frozen=True)
class RefreshDelta:
    """Diff returned by :meth:`SkillRegistry.refresh`.

    Each list holds skill **names** (not file paths). The three lists are
    disjoint by construction: a file whose mtime bumped and whose
    contents now declare a brand-new ``name`` shows up as ``added`` for
    the new name and ``removed`` for the old, never ``updated``.

    Empty deltas (the steady-state case on a hot path) are cheap to
    construct — callers can ``if delta:`` because :meth:`__bool__`
    reports the union of the three lists.
    """

    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:  # pragma: no cover — trivial
        return bool(self.added or self.updated or self.removed)


class SkillLoadError(RuntimeError):
    """Raised when a ``*.md`` file under the skills root is unparseable
    or missing a required field. The offending path is included so
    operators can locate it without re-running the loader."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    """Split ``text`` into ``(yaml, body)``.

    Returns ``None`` if the file does not start with a ``---`` fence.
    The closing fence is a line that is exactly ``---`` (CR-LF tolerated
    on both delimiters, since Windows checkouts happen). The body is
    everything after the closing fence line, preserved verbatim.
    """
    # Normalise Windows newlines on the opening fence; keep the body
    # with whatever line endings it had so round-tripping stays faithful.
    if text.startswith("---\n"):
        rest = text[len("---\n"):]
    elif text.startswith("---\r\n"):
        rest = text[len("---\r\n"):]
    else:
        return None

    # Scan line-by-line for a closing `---`.
    offset = 0
    for line in rest.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if stripped == "---":
            yaml_str = rest[:offset]
            body_start = offset + len(line)
            return yaml_str, rest[body_start:]
        offset += len(line)
    return None


def _as_str_list(value: Any, field_name: str, path: Path) -> list[str]:
    """Coerce an optional ``list[str]`` frontmatter field; reject
    non-list / non-str values so silent type drift can't smuggle bad
    data into requirements checks."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise SkillLoadError(path, f"{field_name} must be a list of strings")
    out: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            raise SkillLoadError(path, f"{field_name} entries must be strings")
        out.append(entry)
    return out


def _parse_requires(raw: Any, path: Path) -> SkillRequirements:
    """Parse the ``metadata.openclaw.requires`` block. Missing or
    ``None`` means an empty requirements set."""
    if raw is None:
        return SkillRequirements()
    if not isinstance(raw, dict):
        raise SkillLoadError(path, "metadata.openclaw.requires must be a mapping")
    return SkillRequirements(
        bins=_as_str_list(raw.get("bins"), "requires.bins", path),
        # Match the Rust rename: YAML uses camelCase ``anyBins``.
        any_bins=_as_str_list(raw.get("anyBins"), "requires.anyBins", path),
        config=_as_str_list(raw.get("config"), "requires.config", path),
        env=_as_str_list(raw.get("env"), "requires.env", path),
    )


def _parse_skill(path: Path, text: str) -> Skill:
    """Parse one ``SKILL.md`` file's raw text into a :class:`Skill`.

    Mirrors the Rust parser's field layout so the two implementations
    agree on wire format: ``name`` and ``description`` at the top
    level; ``emoji`` / ``requires`` / ``install`` under
    ``metadata.openclaw``; ``allowed-tools`` at the top level.
    """
    split = _split_frontmatter(text)
    if split is None:
        raise SkillLoadError(path, "missing YAML frontmatter (expected leading '---' fence)")
    yaml_str, body = split

    try:
        raw = yaml.safe_load(yaml_str) if yaml_str.strip() else {}
    except yaml.YAMLError as exc:
        raise SkillLoadError(path, f"yaml parse error: {exc}") from exc

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SkillLoadError(path, "frontmatter must be a mapping")

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SkillLoadError(path, "name is required and must be a non-empty string")

    description = raw.get("description")
    if not isinstance(description, str) or not description.strip():
        raise SkillLoadError(path, "description is required and must be a non-empty string")

    metadata = raw.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise SkillLoadError(path, "metadata must be a mapping")
    openclaw = metadata.get("openclaw") or {}
    if not isinstance(openclaw, dict):
        raise SkillLoadError(path, "metadata.openclaw must be a mapping")

    emoji = openclaw.get("emoji")
    if emoji is not None and not isinstance(emoji, str):
        raise SkillLoadError(path, "metadata.openclaw.emoji must be a string")

    install = openclaw.get("install")
    if install is not None and not isinstance(install, str):
        raise SkillLoadError(path, "metadata.openclaw.install must be a string")

    requires = _parse_requires(openclaw.get("requires"), path)
    # Rust uses the rename ``allowed-tools``; keep that on the wire.
    allowed_tools = _as_str_list(raw.get("allowed-tools"), "allowed-tools", path)

    return Skill(
        name=name,
        description=description,
        emoji=emoji,
        requires=requires,
        install=install,
        allowed_tools=allowed_tools,
        body_markdown=body,
        source_path=path,
    )


class SkillRegistry:
    """Read-only lookup over skills parsed from disk.

    Duplicate skill names across files raise :class:`SkillLoadError`
    immediately — silent last-wins behaviour produces hard-to-debug
    "why did my skill change?" tickets.
    """

    def __init__(
        self,
        skills: dict[str, Skill] | None = None,
        *,
        min_interval_ms: int | None = None,
    ) -> None:
        self._skills: dict[str, Skill] = skills or {}
        # ``refresh()`` resolves changes by stat()ing the same root the
        # initial load walked. ``None`` means "never loaded from disk" —
        # in that mode :meth:`refresh` is a no-op so unit tests that hand
        # in a fixed dict don't get surprising disk traffic.
        self._root: Path | None = None
        # Per-file mtime cache, keyed by resolved source path. Populated
        # by :meth:`load_from_dir` and kept in sync by :meth:`refresh`.
        self._mtimes: dict[Path, float] = {}
        # Reverse index so a deleted file can be mapped back to the
        # skill name we need to drop. Skills can share neither name nor
        # source path, so this is a true bijection over the live set.
        self._path_to_name: dict[Path, str] = {}
        # ms-since-epoch of the most recent successful refresh (or the
        # initial load when no refresh has run). ``None`` only for
        # registries built from an in-memory dict via ``__init__``.
        self._last_refreshed_at_ms: int | None = None
        # Debounce: monotonic timestamp (in ms) of the last refresh that
        # actually scanned disk. Refresh calls within ``min_interval_ms``
        # of this stamp short-circuit to an empty delta so the per-turn
        # caller (chat handler) doesn't pay rglob + stat cost on every
        # turn. ``force=True`` on :meth:`refresh` bypasses the debounce.
        # Monotonic so a wall-clock jump back can't unblock the debounce
        # spuriously, and so two refresh()es inside the same ms still
        # produce a strictly-monotonic stamp.
        self._min_interval_ms: int = (
            int(min_interval_ms)
            if min_interval_ms is not None
            else _default_refresh_interval_ms()
        )
        self._last_refresh_monotonic_ms: float = 0.0

    @classmethod
    def load_from_dir(cls, root: Path) -> SkillRegistry:
        """Walk ``root`` recursively and parse every ``*.md`` file.

        Non-existent roots yield an empty registry (lets operators start
        with no skills configured). A path that exists but isn't a
        directory is a configuration error and raises.

        The registry retains ``root`` so :meth:`refresh` can re-walk it
        on demand without forcing callers to pass the path again.
        """
        skills: dict[str, Skill] = {}
        mtimes: dict[Path, float] = {}
        path_to_name: dict[Path, str] = {}
        if root.exists():
            if not root.is_dir():
                raise SkillLoadError(root, "skills root must be a directory")

            # Deterministic traversal so duplicate errors are reproducible
            # across platforms.
            for path in sorted(root.rglob("*.md")):
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8")
                skill = _parse_skill(path, text)
                existing = skills.get(skill.name)
                if existing is not None:
                    raise SkillLoadError(
                        path,
                        f"duplicate skill name {skill.name!r} "
                        f"(also defined in {existing.source_path})",
                    )
                skills[skill.name] = skill
                try:
                    mtimes[path] = path.stat().st_mtime
                except OSError:
                    # Lost the race to a concurrent delete; pretend the
                    # file was never there for refresh-tracking purposes.
                    # The Skill is still in the registry — this only
                    # affects whether :meth:`refresh` will later see it
                    # as "unchanged" or "added/removed".
                    pass
                path_to_name[path] = skill.name

        inst = cls(skills)
        inst._root = root
        inst._mtimes = mtimes
        inst._path_to_name = path_to_name
        inst._last_refreshed_at_ms = int(time.time() * 1000)
        # Intentionally leave ``_last_refresh_monotonic_ms`` at 0.0 here
        # so the FIRST call to :meth:`refresh` after boot still scans
        # (the debounce gate checks ``> 0.0`` before kicking in). The
        # second turn — usually within the 30s window — is where the
        # cost-savings start to land.
        return inst

    def get(self, name: str) -> Skill | None:
        """Return the skill for ``name`` or ``None`` if not registered."""
        return self._skills.get(name)

    def names(self) -> list[str]:
        """Sorted list of all registered skill names."""
        return sorted(self._skills.keys())

    @property
    def last_refreshed_at_ms(self) -> int | None:
        """ms-since-epoch of the most recent successful disk scan.

        ``None`` only for in-memory registries built via ``__init__``
        without going through :meth:`load_from_dir` (test fixtures).
        Surfaced for diagnostics; do not use as a cache key.
        """
        return self._last_refreshed_at_ms

    def status_summary(self) -> dict[str, Any]:
        """Diagnostic snapshot of the registry suitable for an admin
        page or a log line.

        Keys:

        * ``skill_count`` — number of registered skills right now.
        * ``last_refreshed_at_ms`` — see :attr:`last_refreshed_at_ms`.
        * ``root`` — string path the registry watches (``None`` for an
          in-memory registry).
        * ``names`` — sorted list of registered skill names.

        Read-only by design; mutating the returned dict will not affect
        the registry.
        """
        return {
            "skill_count": len(self._skills),
            "last_refreshed_at_ms": self._last_refreshed_at_ms,
            "root": str(self._root) if self._root is not None else None,
            "names": self.names(),
        }

    def refresh(self, *, force: bool = False) -> RefreshDelta:
        """Re-walk the registry's root directory and reconcile against
        the cached state.

        Returns a :class:`RefreshDelta` so the caller can log only when
        something actually changed. The hot-path cost is one ``stat()``
        per known file plus one ``rglob`` on the root — single-digit ms
        for a directory of ~16 SKILL.md files on a warm filesystem.

        Debounce — calls within ``self._min_interval_ms`` (default
        :data:`_DEFAULT_SKILL_REFRESH_INTERVAL_MS`, overridable via
        ``CORLINMAN_SKILL_REFRESH_INTERVAL_MS``) of the previous real
        scan short-circuit to an empty delta. The chat handler invokes
        :meth:`refresh` at every turn boundary; without this guard a
        hot session pays ~5-10ms / turn rglob+stat on a directory that
        almost never changes between adjacent turns. Pass
        ``force=True`` to bypass the debounce — used by tests and the
        future "force refresh" admin button.

        Failure modes are all fail-soft (the registry stays usable):

        * The root no longer exists → drop every tracked skill and
          return them all as ``removed``. Lets operators delete the
          skills dir at runtime without crashing prompt assembly.
        * A specific file fails to parse / load → log a warning and
          leave the prior version (if any) in place. A broken edit
          should never blow away the working copy of a skill.
        * Two surviving files now declare the same ``name`` →
          deterministic last-loses (sorted traversal): keep the first,
          warn for the duplicate. Hard-erroring on a per-turn refresh
          would brick the chat path.

        For registries built from an in-memory dict (no ``_root``) this
        is a no-op and returns an empty delta — tests that hand-craft a
        registry never get surprise disk traffic.
        """
        if self._root is None:
            return RefreshDelta()

        # Debounce gate. ``min_interval_ms <= 0`` disables debouncing
        # entirely (operator opt-out via env). ``force=True`` is the
        # explicit bypass for the admin / test paths.
        if not force and self._min_interval_ms > 0:
            now_ms = time.monotonic_ns() / 1_000_000
            elapsed = now_ms - self._last_refresh_monotonic_ms
            if (
                self._last_refresh_monotonic_ms > 0.0
                and elapsed < self._min_interval_ms
            ):
                return RefreshDelta()

        root = self._root
        added: list[str] = []
        updated: list[str] = []
        removed: list[str] = []

        # Snapshot what we currently know so we can diff the post-scan
        # state without mutating the live dicts mid-walk.
        prev_paths: set[Path] = set(self._mtimes.keys()) | set(self._path_to_name.keys())

        if not root.exists():
            # Whole directory vanished — drop everything we were tracking.
            for path in sorted(prev_paths):
                name = self._path_to_name.get(path)
                if name is not None and name in self._skills:
                    del self._skills[name]
                    removed.append(name)
            self._mtimes.clear()
            self._path_to_name.clear()
            self._stamp_refresh()
            return RefreshDelta(added=added, updated=updated, removed=removed)

        if not root.is_dir():
            # A file appeared where a directory should be. Treat the
            # same as "no skills dir" for refresh purposes; the original
            # load_from_dir would have raised, but that's a boot-time
            # contract — we don't want to crash a chat turn over it.
            _log.warning("agent.skills.refresh.root_not_dir", root=str(root))
            self._stamp_refresh()
            return RefreshDelta()

        # Track per-name resolutions so a duplicate within one refresh
        # cycle is handled deterministically (sorted-traversal wins).
        live_paths: set[Path] = set()
        seen_names: dict[str, Path] = {}

        for path in sorted(root.rglob("*.md")):
            if not path.is_file():
                continue
            live_paths.add(path)
            try:
                mtime = path.stat().st_mtime
            except OSError as exc:
                _log.warning(
                    "agent.skills.refresh.stat_failed",
                    path=str(path),
                    error=str(exc),
                )
                continue

            prev_mtime = self._mtimes.get(path)
            prev_name = self._path_to_name.get(path)

            if prev_mtime is not None and prev_mtime == mtime:
                # Unchanged — but still record the name we resolved for
                # this path so the duplicate-detection pass below sees
                # it.
                if prev_name is not None:
                    seen_names[prev_name] = path
                continue

            # Either new (prev_mtime is None) or modified.
            try:
                text = path.read_text(encoding="utf-8")
                skill = _parse_skill(path, text)
            except (OSError, SkillLoadError) as exc:
                _log.warning(
                    "agent.skills.refresh.parse_failed",
                    path=str(path),
                    error=str(exc),
                )
                # Leave the prior version (if any) intact — a half-edit
                # to a SKILL.md must not knock the working copy out of
                # the registry mid-conversation.
                continue

            # Duplicate check against other files in this same refresh
            # pass. Sorted-traversal makes "first wins" deterministic.
            other_path = seen_names.get(skill.name)
            if other_path is not None and other_path != path:
                _log.warning(
                    "agent.skills.refresh.duplicate_name",
                    name=skill.name,
                    first=str(other_path),
                    second=str(path),
                )
                continue

            # The previous skill name at this path may differ (someone
            # renamed the ``name:`` field). Treat that as remove+add so
            # the delta is accurate.
            renamed = False
            if prev_name is not None and prev_name != skill.name:
                renamed = True
                if prev_name in self._skills:
                    del self._skills[prev_name]
                    removed.append(prev_name)

            # If a different file currently owns this skill name in the
            # live set, the sorted-first-wins rule above handled it
            # already (we'd have continued); so it's safe to assign.
            self._skills[skill.name] = skill
            self._mtimes[path] = mtime
            self._path_to_name[path] = skill.name
            seen_names[skill.name] = path
            if prev_mtime is None or renamed:
                # Brand-new path, or same path now exposing a fresh
                # skill identity — either way the *name* is new.
                added.append(skill.name)
            else:
                updated.append(skill.name)

        # Any path we knew about but didn't see this scan was deleted.
        for path in sorted(prev_paths - live_paths):
            name = self._path_to_name.pop(path, None)
            self._mtimes.pop(path, None)
            if name is not None and name in self._skills:
                # Guard against the rename case above already having
                # removed it.
                # Also guard against another file in this pass having
                # taken over the name — in that case don't drop the
                # name from the registry, only the dead path mapping.
                live_owner = seen_names.get(name)
                if live_owner is None:
                    del self._skills[name]
                    removed.append(name)

        self._stamp_refresh()
        return RefreshDelta(added=added, updated=updated, removed=removed)

    def _stamp_refresh(self) -> None:
        """Bookkeeping shared by every successful :meth:`refresh` exit.

        Updates the wall-clock ``_last_refreshed_at_ms`` (exposed for
        diagnostics) and the monotonic ``_last_refresh_monotonic_ms``
        that the debounce gate at the top of :meth:`refresh` reads.
        Kept private — callers go through :meth:`refresh`.
        """
        self._last_refreshed_at_ms = int(time.time() * 1000)
        self._last_refresh_monotonic_ms = time.monotonic_ns() / 1_000_000

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._skills

    def __iter__(self):
        return iter(self._skills.values())

    def __len__(self) -> int:
        return len(self._skills)

    def check_requirements(
        self,
        skill_name: str,
        config_lookup: Callable[[str], str | None],
    ) -> list[str] | None:
        """Verify every requirement for ``skill_name``.

        Returns ``None`` when the skill can run. Otherwise returns a list
        of actionable human-readable problem messages, one per unmet
        requirement.

        ``config_lookup(key)`` should return ``Some(value)`` for a set,
        non-empty config key and ``None`` otherwise.

        Raises a :class:`KeyError`-like problem list if ``skill_name``
        isn't registered — the caller usually already resolved the skill
        via :meth:`get`, but we guard the method too so it stays safe to
        call standalone.
        """
        skill = self._skills.get(skill_name)
        if skill is None:
            return [f"skill '{skill_name}' is not registered"]

        problems: list[str] = []
        req = skill.requires

        for bin_name in req.bins:
            if shutil.which(bin_name) is None:
                problems.append(
                    f"skill '{skill.name}' requires binary '{bin_name}' on $PATH; "
                    "install it first"
                )

        if req.any_bins:
            any_ok = any(shutil.which(b) is not None for b in req.any_bins)
            if not any_ok:
                joined = ", ".join(req.any_bins)
                problems.append(
                    f"skill '{skill.name}' requires one of: {{{joined}}}; none found"
                )

        for key in req.config:
            value = config_lookup(key)
            present = isinstance(value, str) and value.strip() != ""
            if not present:
                problems.append(
                    f"skill '{skill.name}' requires config '{key}' to be set (non-empty)"
                )

        for var in req.env:
            env_val = os.environ.get(var)
            present = env_val is not None and env_val != ""
            if not present:
                problems.append(
                    f"skill '{skill.name}' requires env var '{var}' to be set"
                )

        return problems if problems else None


__all__ = ["RefreshDelta", "SkillLoadError", "SkillRegistry"]
