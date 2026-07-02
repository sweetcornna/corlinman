"""``/rewind`` — restore the workspace to a prior per-turn checkpoint.

The agent servicer snapshots the workspace at the *start* of every chat
turn (``corlinman_agent.coding._snapshot.snapshot``, called from the
servicer's chat path), labelling each git commit with the turn's user
text. A checkpoint therefore holds the workspace as it was *before*
that turn's edits — rewinding to the checkpoint labelled "fix the bug"
undoes that turn and everything after it.

This module is a pure consumer of the snapshot store:

* :func:`list_checkpoints` enumerates via the canonical
  :func:`~corlinman_agent.coding._snapshot.list_snapshots`, decorated
  with best-effort committer timestamps (read-only ``git log`` through
  the store's own ``_run_git`` runner — no git logic re-implemented).
* :func:`rewind_to` walks back with repeated
  :func:`~corlinman_agent.coding._snapshot.revert_last` calls until
  HEAD is the chosen checkpoint — the exact machinery the
  ``revert_changes`` tool uses, just applied N times.

Conversation-window truncation is *best effort by design*: the only
data linking a checkpoint to a console turn is the sanitised label
(first line of the user text, ≤80 chars), and the snapshot store is
global — turns from other surfaces (web chat, channels) interleave in
the same git log. When exactly one user message in this console's
window matches the checkpoint label we truncate the window there;
otherwise we restore files only and say so honestly in the output
("files restored; conversation window unchanged").
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from corlinman_agent.coding._common import resolve_workspace
from corlinman_agent.coding._snapshot import (
    _run_git,
    _sanitise_label,
    list_snapshots,
    revert_last,
)

if TYPE_CHECKING:
    from pathlib import Path

    from corlinman_server.console.brain import BrainSession

logger = structlog.get_logger(__name__)

__all__ = [
    "Checkpoint",
    "RewindResult",
    "cmd_rewind",
    "format_checkpoints",
    "list_checkpoints",
    "rewind_to",
]

#: Commit-subject prefix the snapshot store puts in front of every label.
_SUBJECT_PREFIX = "snapshot: "

#: How many checkpoints /rewind shows and can target by ordinal.
_DEFAULT_LIMIT = 50

_NO_CHECKPOINTS = (
    "no checkpoints — the workspace snapshots on each agent turn "
    "(requires git on PATH; the first turn creates the store)"
)


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """One restorable snapshot, newest-first ordinal aside."""

    sha: str
    label: str
    #: ISO-8601 committer date; empty string when the decoration pass
    #: failed (the checkpoint is still fully usable without it).
    timestamp: str
    #: Journal turn this snapshot precedes (parsed from the ``[turn:<id>]``
    #: subject tag stamped since v1.23.x); ``None`` for legacy snapshots —
    #: those fall back to the user-text label match for window truncation.
    turn_id: int | None = None


#: ``[turn:<id>] `` tag the servicer stamps into the snapshot subject.
_TURN_TAG_RE = re.compile(r"^\[turn:(\d+)\]\s*")


def _parse_turn_tag(label: str) -> tuple[int | None, str]:
    """Split ``"[turn:42] fix the bug"`` → ``(42, "fix the bug")``."""
    m = _TURN_TAG_RE.match(label)
    if m is None:
        return None, label
    return int(m.group(1)), label[m.end() :]


@dataclass(frozen=True, slots=True)
class RewindResult:
    """Outcome of :func:`rewind_to` — ``message`` is REPL-ready."""

    ok: bool
    message: str
    sha: str = ""
    label: str = ""
    files_restored: bool = False
    window_truncated: bool = False
    dropped_messages: int = 0


def _strip_subject(subject: str) -> str:
    """``"snapshot: fix the bug"`` → ``"fix the bug"``."""
    if subject.startswith(_SUBJECT_PREFIX):
        return subject[len(_SUBJECT_PREFIX) :]
    return subject


def _timestamps_by_sha(workspace: Path, limit: int) -> dict[str, str]:
    """Best-effort ``{short_sha: iso_committer_date}`` for the log head.

    Read-only decoration on top of :func:`list_snapshots` (which is the
    source of truth for what exists); any failure degrades to ``{}`` so
    checkpoints simply render without timestamps.
    """
    if not (workspace / ".git").exists():
        return {}
    proc = _run_git(workspace, "log", f"--max-count={limit}", "--pretty=format:%h%x09%cI")
    if proc.returncode != 0:
        return {}
    out: dict[str, str] = {}
    for row in proc.stdout.splitlines():
        sha, _, when = row.partition("\t")
        if sha and when:
            out[sha.strip()] = when.strip()
    return out


def list_checkpoints(
    workspace: Path | str | None = None, *, limit: int = _DEFAULT_LIMIT
) -> list[Checkpoint]:
    """Enumerate restorable snapshots for the workspace, newest first.

    ``workspace=None`` resolves exactly the way the servicer does
    (:func:`corlinman_agent.coding._common.resolve_workspace`), so the
    embedded console sees the same store the agent writes to. Empty
    list when git is missing or no snapshot has ever been taken.
    """
    ws = resolve_workspace(workspace)
    snaps = list_snapshots(ws, limit=limit)
    if not snaps:
        return []
    times = _timestamps_by_sha(ws, limit=limit)
    out: list[Checkpoint] = []
    for row in snaps:
        turn_id, label = _parse_turn_tag(_strip_subject(row["label"]))
        out.append(
            Checkpoint(
                sha=row["sha"],
                label=label,
                timestamp=times.get(row["sha"], ""),
                turn_id=turn_id,
            )
        )
    return out


def format_checkpoints(checkpoints: list[Checkpoint]) -> str:
    """Numbered, newest-first listing for the REPL."""
    if not checkpoints:
        return _NO_CHECKPOINTS
    lines = ["workspace checkpoints (newest first):"]
    for n, cp in enumerate(checkpoints, start=1):
        ts = f"  {cp.timestamp}" if cp.timestamp else ""
        current = "  (current)" if n == 1 else ""
        lines.append(f"  {n:>3}. {cp.sha}{ts}  {cp.label}{current}")
    lines.append("usage: /rewind <n|sha> to restore the workspace to a checkpoint")
    return "\n".join(lines)


def _resolve_target(target: str, checkpoints: list[Checkpoint]) -> tuple[int | None, str]:
    """Map a user-supplied ``<n|sha>`` onto a checkpoint index.

    Ordinals win when in range; otherwise the argument is matched
    against the short SHAs (exact, ≥4-char prefix, or a longer full SHA
    that starts with the short one). Returns ``(index, "")`` or
    ``(None, <polite error>)``.
    """
    t = target.strip().lower()
    if not t:
        return None, "usage: /rewind [n|sha]"
    if t.isdigit():
        n = int(t)
        if 1 <= n <= len(checkpoints):
            return n - 1, ""
    matches = [
        i
        for i, cp in enumerate(checkpoints)
        if cp.sha == t
        or (len(t) >= 4 and cp.sha.startswith(t))
        or (len(t) > len(cp.sha) and t.startswith(cp.sha))
    ]
    if len(matches) == 1:
        return matches[0], ""
    if matches:
        shas = ", ".join(checkpoints[i].sha for i in matches)
        return None, f"ambiguous checkpoint '{target}' — matches {shas}"
    return None, f"no checkpoint '{target}' — /rewind with no arguments lists them"


def _truncate_window(session: BrainSession, label: str) -> tuple[bool, int, str]:
    """Drop window messages from the turn the checkpoint precedes.

    The checkpoint was taken at the *start* of the turn whose user text
    sanitises to ``label``, so on a unique match we delete that user
    message and everything after it. Zero or multiple matches → no
    truncation, with the reason returned for the honest degrade path.
    """
    matches = [
        i
        for i, msg in enumerate(session.window)
        if msg.get("role") == "user" and _sanitise_label(str(msg.get("content", ""))) == label
    ]
    if len(matches) == 1:
        dropped = len(session.window) - matches[0]
        del session.window[matches[0] :]
        return True, dropped, ""
    if matches:
        return False, 0, "checkpoint label matches multiple turns in this window"
    return False, 0, "no matching turn in this console window"


def rewind_to(
    target: str,
    *,
    session: BrainSession | None = None,
    workspace: Path | str | None = None,
    limit: int = _DEFAULT_LIMIT,
    skip_window: bool = False,
) -> RewindResult:
    """Restore the workspace to the checkpoint named by ``target``.

    Walks HEAD back one snapshot at a time via
    :func:`~corlinman_agent.coding._snapshot.revert_last` — the same
    call the ``revert_changes`` tool makes — until the chosen
    checkpoint is HEAD. Discarded checkpoints disappear from the log,
    matching ``revert_last`` semantics. Never raises on store errors;
    every failure mode comes back as a polite ``RewindResult``.
    """
    ws = resolve_workspace(workspace)
    checkpoints = list_checkpoints(ws, limit=limit)
    if not checkpoints:
        return RewindResult(ok=False, message=_NO_CHECKPOINTS)
    idx, err = _resolve_target(target, checkpoints)
    if idx is None:
        return RewindResult(ok=False, message=err)
    chosen = checkpoints[idx]
    if idx == 0:
        # Snapshots are taken at the START of a turn, so the common idle
        # state is HEAD == newest checkpoint plus uncommitted edits made
        # DURING that turn. Rewinding to checkpoint 1 therefore means
        # discarding those edits with a hard reset to HEAD — not a no-op.
        reset = _run_git(ws, "reset", "--hard", "HEAD")
        if reset.returncode != 0:
            return RewindResult(
                ok=False,
                sha=chosen.sha,
                label=chosen.label,
                message=(
                    f"reset to checkpoint {chosen.sha} failed: "
                    f"{(reset.stderr or reset.stdout).strip()[:200]}"
                ),
            )
        # ``reset --hard`` leaves files the latest turn CREATED (they are
        # untracked at HEAD) — sweep them too, or the "restore" is partial.
        _run_git(ws, "clean", "-fd")
        truncated, dropped, reason = (False, 0, "no console session attached")
        if not skip_window and session is not None:
            truncated, dropped, reason = _truncate_window(session, chosen.label)
        if skip_window:
            window_note = ""  # caller owns window handling (turn-keyed rebuild)
        elif truncated:
            window_note = f"; {dropped} window message(s) dropped"
        else:
            window_note = f"; conversation window unchanged ({reason})"
        return RewindResult(
            ok=True,
            sha=chosen.sha,
            label=chosen.label,
            files_restored=True,
            message=(
                f"workspace reset to checkpoint {chosen.sha} ({chosen.label}) "
                "— uncommitted edits made after the snapshot were discarded"
                + window_note
            ),
        )

    reverted_to = ""
    for step in range(idx):
        res = revert_last(ws)
        if "error" in res:
            return RewindResult(
                ok=False,
                sha=chosen.sha,
                label=chosen.label,
                files_restored=step > 0,
                message=(
                    f"rewind stopped after {step} of {idx} step(s): "
                    f"{res['error']} — /rewind to see where the workspace is now"
                ),
            )
        reverted_to = res.get("reverted_to", "")
    # Same untracked-file sweep as the checkpoint-1 path: files created
    # after the target snapshot are untracked at the reverted HEAD.
    _run_git(ws, "clean", "-fd")
    if reverted_to != chosen.sha:
        return RewindResult(
            ok=False,
            sha=chosen.sha,
            label=chosen.label,
            files_restored=True,
            message=(
                f"rewind landed on {reverted_to or '(unknown)'} but expected "
                f"{chosen.sha} — /rewind to inspect the store"
            ),
        )

    logger.info("console.rewind.restored", sha=chosen.sha, label=chosen.label)

    truncated, dropped, reason = (False, 0, "no console session attached")
    if not skip_window and session is not None:
        truncated, dropped, reason = _truncate_window(session, chosen.label)
    if skip_window:
        tail = "files restored"  # caller appends the turn-keyed window note
    elif truncated:
        tail = f"files restored; conversation window: dropped {dropped} message(s)"
    else:
        tail = f"files restored; conversation window unchanged ({reason})"
    return RewindResult(
        ok=True,
        sha=chosen.sha,
        label=chosen.label,
        files_restored=True,
        window_truncated=truncated,
        dropped_messages=dropped,
        message=f"rewound to checkpoint {chosen.sha} — {chosen.label}\n{tail}",
    )


async def cmd_rewind(app: Any, args: str) -> str:
    """``/rewind`` handler — no args lists, ``<n|sha>`` restores.

    Embedded mode only: in attach mode the workspace (and its snapshot
    store) lives in the remote gateway process, so a local rewind would
    touch the wrong directory.
    """
    if not getattr(app, "embedded", False):
        return (
            "/rewind needs the embedded brain — in attach mode the workspace "
            "lives in the gateway process"
        )
    arg = args.strip()
    try:
        if not arg:
            return format_checkpoints(list_checkpoints())
        # Turn-keyed window rebuild (Dim 11): when the chosen checkpoint
        # carries the journal turn id it precedes AND the app can replay the
        # journal, restore files with rewind_to and rebuild the window from
        # turns strictly before that id — exact, no label heuristics. Legacy
        # snapshots (no tag) keep the label-match fallback inside rewind_to.
        checkpoints = list_checkpoints()
        idx, _err = _resolve_target(arg, checkpoints)
        chosen = checkpoints[idx] if idx is not None else None
        rebuild = getattr(app, "replay_window_before", None)
        turn_keyed = (
            chosen is not None
            and chosen.turn_id is not None
            and callable(rebuild)
        )
        result = rewind_to(
            arg,
            session=getattr(app, "session", None),
            skip_window=turn_keyed,
        )
        if result.ok and turn_keyed and chosen is not None and rebuild is not None:
            replayed = await rebuild(chosen.turn_id)
            if replayed is None:
                note = "conversation window unchanged (journal unavailable)"
            else:
                note = (
                    f"conversation window rebuilt from the journal: "
                    f"{replayed} message(s) (turns before turn {chosen.turn_id})"
                )
            return f"{result.message}; {note}"
    except Exception as exc:  # noqa: BLE001 — typos must not stack-trace the REPL
        logger.warning("console.rewind.failed", error=str(exc))
        return f"rewind failed: {exc}"
    return result.message
