"""Project memory — claude-code's CLAUDE.md system, as CORLINMAN.md.

Pure functions, no console dependencies. Discovery mirrors claude-code:
a user-global file under the data dir, then one ``CORLINMAN.md`` (and
``CORLINMAN.local.md``) per directory from the repo root *down* to the
working directory — files closer to cwd are appended later and so carry
higher priority. ``@path`` lines pull other files in-place.
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "LOCAL_MEMORY_FILENAME",
    "MEMORY_FILENAME",
    "discover_memory_files",
    "expand_includes",
    "load_project_memory",
]

MEMORY_FILENAME = "CORLINMAN.md"
LOCAL_MEMORY_FILENAME = "CORLINMAN.local.md"

#: Upward-walk cap when no ``.git`` (and no home dir) bounds the search.
_MAX_WALK_LEVELS = 20
#: ``@include`` recursion cap.
_MAX_INCLUDE_DEPTH = 5
#: Assembled system-prompt block cap (bytes of UTF-8).
_MAX_TOTAL_BYTES = 64 * 1024

_HEADER = "Project memory (CORLINMAN.md) — instructions from the user/project:"
_TRUNCATION_MARKER = "\n<!-- truncated: project memory exceeds 64KB -->"

#: A directive is a line that is *only* ``@<path>`` (``@./rel``, ``@~/x``,
#: ``@/abs``, or a bare relative path) — anything else passes through.
_INCLUDE_RE = re.compile(r"^@(\S+)$")


def _walk_chain(cwd: Path) -> list[Path]:
    """Directories from ``cwd`` upward (cwd first).

    The walk stops at the git repo root (first directory containing a
    ``.git`` entry — dir or worktree file), else at the home directory,
    else at the filesystem root, else after ``_MAX_WALK_LEVELS``.
    """
    chain: list[Path] = []
    home = Path.home()
    current = cwd
    for _ in range(_MAX_WALK_LEVELS):
        chain.append(current)
        if (current / ".git").exists():
            break
        if current == home:
            break
        parent = current.parent
        if parent == current:  # filesystem root
            break
        current = parent
    return chain


def discover_memory_files(cwd: Path, data_dir: Path) -> list[Path]:
    """Existing memory files in concatenation order (lowest priority first).

    Order: user-global ``<data_dir>/CORLINMAN.md`` first, then for each
    directory from the repo root down to ``cwd``: ``CORLINMAN.md`` then
    ``CORLINMAN.local.md``. Later files are appended after earlier ones,
    so closer-to-cwd files override (claude-code semantics).
    """
    found: list[Path] = []
    seen: set[Path] = set()

    def add(path: Path) -> None:
        if not path.is_file():
            return
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        found.append(path)

    add(data_dir / MEMORY_FILENAME)
    for directory in reversed(_walk_chain(cwd)):  # repo root → cwd
        add(directory / MEMORY_FILENAME)
        add(directory / LOCAL_MEMORY_FILENAME)
    return found


def expand_includes(
    text: str,
    *,
    base_dir: Path,
    visited: set[Path] | None = None,
    depth: int = 0,
) -> str:
    """Expand ``@path`` directive lines in-place.

    Relative paths resolve against the including file's directory
    (``base_dir``); ``~`` expands to the user home; absolute paths are
    used as-is. A missing target becomes a one-line HTML-comment marker,
    re-included files are skipped via ``visited`` (cycle break), and
    expansion stops at depth ``_MAX_INCLUDE_DEPTH``.
    """
    if visited is None:
        visited = set()
    out: list[str] = []
    for line in text.splitlines():
        match = _INCLUDE_RE.match(line.strip())
        if match is None:
            out.append(line)
            continue
        raw = match.group(1)
        if depth >= _MAX_INCLUDE_DEPTH:
            out.append(f"<!-- include depth limit reached: {raw} -->")
            continue
        target = Path(raw).expanduser()
        if not target.is_absolute():
            target = base_dir / target
        resolved = target.resolve()
        if resolved in visited:
            out.append(f"<!-- skipped cyclic include: {raw} -->")
            continue
        try:
            content = target.read_text(encoding="utf-8")
        except OSError:
            out.append(f"<!-- missing include: {raw} -->")
            continue
        visited.add(resolved)
        out.append(
            expand_includes(
                content,
                base_dir=target.parent,
                visited=visited,
                depth=depth + 1,
            )
        )
    return "\n".join(out)


def load_project_memory(cwd: Path, data_dir: Path) -> tuple[str | None, list[Path]]:
    """Assemble the project-memory system-prompt block.

    Returns ``(text, files)`` where ``text`` is the header plus every
    discovered file's expanded content joined by ``# from: <path>``
    separators (capped at 64KB with a truncation marker), or
    ``(None, [])`` when nothing was found/readable.
    """
    parts: list[str] = [_HEADER]
    loaded: list[Path] = []
    for path in discover_memory_files(cwd, data_dir):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:  # raced away / unreadable — skip, don't crash startup
            continue
        loaded.append(path)
        body = expand_includes(raw, base_dir=path.parent, visited={path.resolve()})
        parts.append(f"# from: {path}")
        parts.append(body.rstrip("\n"))
    if not loaded:
        return None, []
    text = "\n\n".join(parts)
    encoded = text.encode("utf-8")
    if len(encoded) > _MAX_TOTAL_BYTES:
        text = encoded[:_MAX_TOTAL_BYTES].decode("utf-8", errors="ignore") + _TRUNCATION_MARKER
    return text, loaded
