"""Workspace-as-git-repo snapshot store.

The agent workspace is treated as a private git repository whose only
purpose is to give every turn a revertible checkpoint. The model never
sees these commits — they're invisible to the rest of the system — but
the user (or the model itself, via the ``revert_changes`` tool) can roll
the workspace back to any prior turn's snapshot.

Implementation notes:

* All git operations shell out to the ``git`` binary via
  :func:`subprocess.run` with ``check=False`` and ``capture_output=True``;
  this module never raises out to its callers — every failure path
  returns a sentinel (``False`` / ``None`` / ``{"error": ...}``) and is
  logged.
* If ``git`` is missing on ``PATH``, snapshotting becomes a logged no-op;
  the agent runs unchanged, just without revert support.
* The repo is initialised lazily on the first :func:`snapshot` call so a
  brand-new workspace doesn't get a ``.git`` it never uses.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

_USER_EMAIL = "agent@corlinman"
_USER_NAME = "corlinman-agent"
_MAX_LABEL_CHARS = 80
# Default length of git's abbreviated SHA (matches ``core.abbrev``
# default + what ``git log --pretty=format:%h`` emits on stock
# config). We truncate the full SHA to this width so the value
# returned by :func:`snapshot` round-trips against
# :func:`list_snapshots`.
_SHORT_SHA_LEN = 7


def _have_git() -> bool:
    """Return True when ``git`` is on ``PATH``. Logged once per absence."""
    if shutil.which("git") is None:
        logger.warning("agent.snapshot.git_missing")
        return False
    return True


def _run_git(workspace: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a ``git`` subcommand inside ``workspace``.

    Always uses ``check=False`` + ``capture_output=True`` + ``text=True``
    so the caller can inspect exit codes and stderr without exception
    handling. Never propagates exceptions out of the subprocess layer.
    """
    return subprocess.run(  # noqa: S603 — args are module-controlled
        ["git", *args],
        cwd=str(workspace),
        check=False,
        capture_output=True,
        text=True,
    )


def _read_git_head_sha(workspace: Path) -> str | None:
    """Resolve ``HEAD`` to its full 40-char SHA without forking ``git``.

    Mirrors what ``git rev-parse HEAD`` would print, but takes ~30 µs
    (one or two stat + small-file reads) instead of ~3 ms (fork + exec
    of the ``git`` binary). The hot path uses this directly after
    ``git commit`` so the SHA lookup costs nothing on each agent turn.

    Strategy:

    1. Read ``<ws>/.git/HEAD``.
       * If it starts with ``ref: <path>`` it's an attached HEAD — follow
         the ref to ``<ws>/.git/<path>`` and read the SHA from there.
       * If that ref file is missing the ref might be packed — fall
         back to scanning ``<ws>/.git/packed-refs`` for a matching line.
       * If it's already a 40-char hex string it's a detached HEAD;
         return as-is.
    2. Validate the result looks like a hex SHA before returning.
       Anything malformed (empty, comment lines, wrong length) becomes
       ``None`` — the caller logs and surfaces a snapshot failure.

    Never raises. Returns ``None`` for any parse failure so the caller
    can log + fall back gracefully (the snapshot itself already
    succeeded; we just can't report the SHA).
    """
    try:
        head_text = (workspace / ".git" / "HEAD").read_text().strip()
    except OSError:
        return None
    if not head_text:
        return None

    if head_text.startswith("ref:"):
        # Attached HEAD: "ref: refs/heads/main"
        ref_path = head_text[4:].strip()
        if not ref_path:
            return None
        # Loose ref first.
        loose = workspace / ".git" / ref_path
        try:
            sha = loose.read_text().strip()
            if _looks_like_sha(sha):
                return sha
        except OSError:
            # Fall through to packed-refs scan.
            pass
        return _lookup_packed_ref(workspace, ref_path)

    # Detached HEAD: the file already holds the SHA.
    if _looks_like_sha(head_text):
        return head_text
    return None


def _looks_like_sha(value: str) -> bool:
    """Cheap structural check for a 40-char lowercase hex SHA.

    git's loose-ref / packed-refs files store SHAs as exactly 40 hex
    characters; anything else (empty, padded, with annotations) is a
    parse failure. We avoid ``re`` for the per-turn hot path.
    """
    if len(value) != 40:
        return False
    return all(c in "0123456789abcdef" for c in value)


def _lookup_packed_ref(workspace: Path, ref_path: str) -> str | None:
    """Find ``<ref_path>`` inside ``<ws>/.git/packed-refs``; return SHA or None.

    ``packed-refs`` lines look like:

    .. code-block:: text

       # pack-refs with: peeled fully-peeled sorted
       fd4cba616ef506c032e4cd2fb089c80fd360e575 refs/heads/main
       ^c0fda7c83f1834212b6c1c1cdde6234aaaabbbbb   (peeled tag, ignored)

    We linear-scan because ``packed-refs`` rarely exceeds a few KB and
    the alternative (loading + bisecting) isn't worth the complexity
    on the hot path.
    """
    try:
        packed = (workspace / ".git" / "packed-refs").read_text()
    except OSError:
        return None
    for raw_line in packed.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("^"):
            continue
        sha, _, ref = line.partition(" ")
        if ref == ref_path and _looks_like_sha(sha):
            return sha
    return None


def _sanitise_label(label: str) -> str:
    """Coerce a label into a single line of ≤80 chars.

    Commit subjects don't get newlines (git would treat the rest as
    body), and overly-long labels clutter ``git log``.
    """
    if not isinstance(label, str):
        label = str(label)
    first_line = label.replace("\r", " ").split("\n", 1)[0].strip()
    if not first_line:
        first_line = "turn"
    if len(first_line) > _MAX_LABEL_CHARS:
        first_line = first_line[:_MAX_LABEL_CHARS].rstrip()
    return first_line


def ensure_repo(workspace: Path) -> bool:
    """Initialise ``workspace`` as a git repo if it isn't one already.

    Idempotent — a no-op when ``<workspace>/.git`` already exists. The
    initial empty commit guarantees ``HEAD`` is valid so the first real
    snapshot has something to compare against.

    Returns True on success, False when git is missing or init failed
    (the latter is logged).
    """
    if not _have_git():
        return False
    workspace.mkdir(parents=True, exist_ok=True)
    if (workspace / ".git").exists():
        return True
    init = _run_git(workspace, "init", "--quiet")
    if init.returncode != 0:
        logger.warning(
            "agent.snapshot.git_init_failed", stderr=init.stderr.strip()
        )
        return False
    # Local repo config — never touch the user's global config.
    _run_git(workspace, "config", "user.email", _USER_EMAIL)
    _run_git(workspace, "config", "user.name", _USER_NAME)
    # Initial commit so HEAD~1 / HEAD^ exists from the very first
    # ``snapshot()`` call. ``--allow-empty`` because the workspace may
    # already be empty.
    add = _run_git(workspace, "add", "-A")
    if add.returncode != 0:
        logger.warning(
            "agent.snapshot.git_add_failed", stderr=add.stderr.strip()
        )
        return False
    commit = _run_git(
        workspace,
        "commit",
        "--allow-empty",
        "--quiet",
        "-m",
        "snapshot: initial",
    )
    if commit.returncode != 0:
        logger.warning(
            "agent.snapshot.git_commit_failed",
            stderr=commit.stderr.strip(),
        )
        return False
    return True


def snapshot(workspace: Path, label: str) -> str | None:
    """Take a snapshot of ``workspace``; return the new short SHA.

    Calls :func:`ensure_repo` first so the first snapshot triggers the
    init. Always uses ``--allow-empty`` — a turn that made no file
    changes still gets a checkpoint so ``revert_changes`` semantics stay
    uniform (one turn = one snapshot).

    Returns ``None`` on any failure (git missing, init failed, commit
    failed). Failures are logged but never raised.

    Perf: the SHA lookup step is performed by reading ``.git/HEAD``
    directly via :func:`_read_git_head_sha` rather than forking
    ``git rev-parse``. Two subprocess calls per snapshot (``add`` +
    ``commit``) instead of three, saving ~3 ms per agent turn.
    """
    if not ensure_repo(workspace):
        return None
    safe_label = _sanitise_label(label)
    add = _run_git(workspace, "add", "-A")
    if add.returncode != 0:
        logger.warning(
            "agent.snapshot.git_add_failed", stderr=add.stderr.strip()
        )
        return None
    commit = _run_git(
        workspace,
        "commit",
        "--allow-empty",
        "--quiet",
        "-m",
        f"snapshot: {safe_label}",
    )
    if commit.returncode != 0:
        logger.warning(
            "agent.snapshot.git_commit_failed",
            stderr=commit.stderr.strip(),
        )
        return None
    # Perf: skip the ``git rev-parse --short HEAD`` subprocess. Read
    # ``.git/HEAD`` (+ optional loose-ref or packed-refs file) ourselves
    # — ~30 µs vs ~3 ms for a fork+exec of git. The result is the full
    # 40-char SHA; we truncate to 7 chars to match git's default
    # ``--short`` output so the value still round-trips against
    # :func:`list_snapshots` (which uses ``%h``).
    full_sha = _read_git_head_sha(workspace)
    if full_sha is None:
        # Parse failure — log + return None so the caller knows the
        # snapshot was taken but the SHA is unreadable. The commit
        # itself still exists; ``list_snapshots`` will find it.
        logger.warning(
            "agent.snapshot.head_parse_failed",
            workspace=str(workspace),
        )
        return None
    short = full_sha[:_SHORT_SHA_LEN]
    logger.info("agent.snapshot.taken", sha=short, label=safe_label)
    return short or None


def revert_last(workspace: Path) -> dict[str, str]:
    """Roll the workspace back to the snapshot before ``HEAD``.

    Strategy: find the second-most-recent commit and ``git reset
    --hard`` to it. The current ``HEAD`` (the most recent snapshot) is
    discarded along with whatever working-tree state lived on top of it.

    Returns:
        ``{"reverted_to": "<sha>", "from": "<old_sha>", "label": "<msg>"}``
        on success;
        ``{"error": "no_snapshots"}`` when only the initial commit exists;
        ``{"error": "<reason>"}`` for any other failure mode (logged).

    Never raises.
    """
    if not _have_git():
        return {"error": "git_missing"}
    if not (workspace / ".git").exists():
        return {"error": "no_snapshots"}
    # Two commits' SHAs + the parent's subject. ``%h`` gives the short
    # SHA, ``%s`` the subject line.
    log = _run_git(
        workspace, "log", "--max-count=2", "--pretty=format:%h%x09%s"
    )
    if log.returncode != 0:
        logger.warning(
            "agent.snapshot.git_log_failed", stderr=log.stderr.strip()
        )
        return {"error": "git_log_failed"}
    rows = [r for r in log.stdout.splitlines() if r.strip()]
    if len(rows) < 2:
        return {"error": "no_snapshots"}
    head_sha, _head_label = rows[0].split("\t", 1)
    parent_sha, parent_label = rows[1].split("\t", 1)
    reset = _run_git(workspace, "reset", "--hard", "--quiet", parent_sha)
    if reset.returncode != 0:
        logger.warning(
            "agent.snapshot.git_reset_failed", stderr=reset.stderr.strip()
        )
        return {"error": "git_reset_failed"}
    logger.info(
        "agent.snapshot.reverted",
        reverted_to=parent_sha,
        from_=head_sha,
    )
    return {
        "reverted_to": parent_sha,
        "from": head_sha,
        "label": parent_label,
    }


def list_snapshots(workspace: Path, limit: int = 10) -> list[dict[str, str]]:
    """Return the most recent ``limit`` snapshots (newest first).

    Each entry: ``{"sha": "<short>", "label": "<commit subject>"}``.
    Empty list when the workspace has no repo, no commits, or git is
    unavailable — never raises.
    """
    if not _have_git():
        return []
    if not (workspace / ".git").exists():
        return []
    try:
        n = max(1, int(limit))
    except (TypeError, ValueError):
        n = 10
    log = _run_git(
        workspace,
        "log",
        f"--max-count={n}",
        "--pretty=format:%h%x09%s",
    )
    if log.returncode != 0:
        logger.warning(
            "agent.snapshot.git_log_failed", stderr=log.stderr.strip()
        )
        return []
    out: list[dict[str, str]] = []
    for row in log.stdout.splitlines():
        if not row.strip():
            continue
        sha, _, label = row.partition("\t")
        out.append({"sha": sha, "label": label})
    return out


__all__ = [
    "ensure_repo",
    "list_snapshots",
    "revert_last",
    "snapshot",
]
