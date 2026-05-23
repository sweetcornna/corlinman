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
    sha = _run_git(workspace, "rev-parse", "--short", "HEAD")
    if sha.returncode != 0:
        logger.warning(
            "agent.snapshot.git_revparse_failed", stderr=sha.stderr.strip()
        )
        return None
    short = sha.stdout.strip()
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
