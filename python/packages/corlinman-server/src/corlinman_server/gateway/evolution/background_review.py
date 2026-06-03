"""LLM-driven background review — autonomous skill + memory consolidation.

Port of hermes-agent's background review fork
(``/tmp/hermes-agent-shallow/agent/background_review.py``).

Unlike the pure deterministic curator (:mod:`.curator`), this module makes
one LLM call with a strict tool-call schema and writes the resulting
mutations back to disk. The hermes implementation forks a full ``AIAgent``
inside the same Python process; corlinman's analogue is more conservative
— it is a **scoped runner** that:

1. Streams a single chat completion from the parent's provider.
2. Re-assembles ``tool_call_*`` chunks into discrete tool calls.
3. Dispatches the result through a hard-coded **whitelist** of two tools.
4. Writes mutations through :mod:`corlinman_skills_registry`'s existing
   safe-write paths (atomic tempfile + ``os.replace``).

Tool whitelist
--------------

The LLM can call ONLY these (anything else is dropped with a warning):

* ``skill_manage(action: "create"|"edit"|"patch"|"delete", name, ...)``
* ``memory_write(target: "MEMORY"|"USER", action: "append"|"replace", content)``

This guarantees the background review can never escape the profile
directory or execute side-effects (no terminal, no web, no arbitrary
file IO).

Review kinds
------------

* ``"memory"``           — only update ``MEMORY.md`` / ``USER.md``
* ``"skill"``            — only create/patch SKILL.md files
* ``"combined"``         — both, in one prompt
* ``"curator"``          — overlap consolidation (folds duplicate
                            ``agent-created`` skills under one umbrella)
* ``"user-correction"``  — process a specific user-correction signal and
                            patch the implicated skill body

Failure mode
------------

:func:`spawn_background_review` **never raises**. Provider failures,
timeouts, malformed tool calls, and disk write errors all surface as a
:class:`BackgroundReviewReport` whose ``error`` field is populated. The
gateway calls this in a fire-and-forget background task; if the fork
crashes we want a structured artefact, not an unhandled exception
killing the asyncio loop.

Modularisation note
-------------------

Cohesive helper groups have been extracted verbatim into sibling private
modules to keep this file focused on the orchestrator. They are re-imported
below so the public surface (and every external importer) is unchanged:

* :mod:`._prompt_loading`      — ``ReviewKind``, ``load_prompt``
* :mod:`._provider_invocation` — ``_TOOL_SCHEMA``, ``_invoke_provider``,
  ``_collect_tool_calls_from_stream``
* :mod:`._skill_dispatch`      — ``ReviewWriteRecord``, ``WHITELISTED_TOOLS``,
  ``_utc_now``, ``_apply_tool_calls`` and the skill/memory write helpers
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog
from corlinman_skills_registry import (
    SkillRegistry,
)

from corlinman_server.gateway.evolution._prompt_loading import (
    ReviewKind,
    load_prompt,
)
from corlinman_server.gateway.evolution._provider_invocation import (
    _invoke_provider,
)
from corlinman_server.gateway.evolution._skill_dispatch import (
    WHITELISTED_TOOLS,
    ReviewWriteRecord,
    _apply_tool_calls,
    _utc_now,
)

logger = structlog.get_logger(__name__)


# ─── Public types ────────────────────────────────────────────────────


# ``ReviewKind`` lives in :mod:`._prompt_loading`; re-exported above so it
# stays importable from this module (``__init__`` + tests depend on it).


# ``WHITELISTED_TOOLS`` lives in :mod:`._skill_dispatch`; re-exported above.


# ``ReviewWriteRecord`` lives in :mod:`._skill_dispatch`; re-exported above.


@dataclass(frozen=True)
class BackgroundReviewReport:
    """Audit artefact produced by one :func:`spawn_background_review` call.

    Mirrors the hermes summarisation shape (`agent/background_review.py:218`)
    in spirit, but is structured rather than a flat list of strings — the
    gateway needs both the human summary AND machine-readable records to
    drive the admin UI's curator preview.
    """

    profile_slug: str
    kind: ReviewKind
    started_at: datetime
    finished_at: datetime
    writes: list[ReviewWriteRecord]
    error: str | None = None  # populated on failure

    @property
    def duration_ms(self) -> int:
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    @property
    def applied_count(self) -> int:
        return sum(1 for w in self.writes if w.applied)

    @property
    def skipped_count(self) -> int:
        return sum(1 for w in self.writes if not w.applied)


def _summarise_skills(registry: SkillRegistry, *, limit: int = 50) -> list[dict[str, Any]]:
    """Build a compact summary of the active registry for the prompt.

    We include only the metadata the model needs to make a routing
    decision — name, origin, state, version, first line of body. The full
    body is left off because (a) it can be large, (b) the curator review
    primarily needs to see the *shape* of the library, not the contents.
    """
    summary: list[dict[str, Any]] = []
    for skill in registry:
        if len(summary) >= limit:
            break
        first_line = ""
        for line in skill.body_markdown.splitlines():
            if line.strip():
                first_line = line.strip()[:200]
                break
        summary.append(
            {
                "name": skill.name,
                "description": skill.description,
                "origin": skill.origin,
                "state": skill.state,
                "version": skill.version,
                "pinned": skill.pinned,
                "first_line": first_line,
            }
        )
    return summary


async def spawn_background_review(
    *,
    kind: ReviewKind,
    profile_slug: str,
    profile_root: Path,
    recent_messages: list[dict[str, Any]],
    registry: SkillRegistry,
    provider: Any,
    model: str,
    timeout_seconds: float = 60.0,
    user_correction_text: str | None = None,
    darwin_input: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> BackgroundReviewReport:
    """One-shot LLM call → tool-call dispatcher → write mutations.

    Catches **every** exception — provider errors, timeouts, malformed
    tool calls, disk write failures — and surfaces them as a
    :class:`BackgroundReviewReport` with ``error`` populated. The gateway
    calls this in a fire-and-forget background task; raising would kill
    the asyncio task with no audit row.

    Mirrors hermes' :func:`spawn_background_review_thread` in shape
    (system prompt + conversation snapshot → tool calls → writes), but
    runs as a scoped async function rather than a fork-and-replay of a
    full :class:`AIAgent`.
    """
    started_at = now or _utc_now()
    profile_root = Path(profile_root)

    try:
        system_prompt = load_prompt(kind)
    except (OSError, ValueError) as err:
        return BackgroundReviewReport(
            profile_slug=profile_slug,
            kind=kind,
            started_at=started_at,
            finished_at=_utc_now(),
            writes=[],
            error=f"prompt_load_failed: {err}",
        )

    if kind == "user-correction" and user_correction_text:
        system_prompt = (
            system_prompt
            + "\n\n## User correction\n\n"
            + user_correction_text.strip()
            + "\n"
        )

    # Build the user message. We send a structured JSON envelope so the
    # model has clear signal about "this is the snapshot, this is the
    # registry context". Tool-call models tolerate JSON in the user turn
    # without confusing it for tool input.
    try:
        skill_summary = _summarise_skills(registry)
    except Exception as err:
        # Registry inspection is best-effort; failures should not break
        # the review pipeline.
        logger.warning(
            "background_review.skill_summary_failed",
            profile_slug=profile_slug,
            err=str(err),
        )
        skill_summary = []

    user_envelope = {
        "profile_slug": profile_slug,
        "kind": kind,
        "recent_messages": list(recent_messages or []),
        "active_skills": skill_summary,
    }
    if kind == "user-correction" and user_correction_text:
        user_envelope["user_correction"] = user_correction_text
    if kind == "darwin" and darwin_input is not None:
        # W3 v2 — darwin proposals carry their rubric report (markdown)
        # + the target skill name + the current SKILL.md body so the
        # LLM has every input it needs in one envelope. We deliberately
        # do NOT let the LLM Read arbitrary files; the curator picks
        # the body and the dispatcher will only accept skill_manage
        # calls targeting that exact name.
        user_envelope["rubric_report"] = str(darwin_input.get("rubric_report", ""))
        user_envelope["skill_name"] = str(darwin_input.get("skill_name", ""))
        user_envelope["current_skill_md"] = str(
            darwin_input.get("current_skill_md", "")
        )

    user_message = (
        "Conversation snapshot + active skill registry follow as JSON. "
        "Emit tool_calls only.\n\n"
        + json.dumps(user_envelope, ensure_ascii=False, default=str)
    )

    chat_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    log = logger.bind(
        profile_slug=profile_slug,
        kind=kind,
        model=model,
    )

    try:
        tool_calls, stream_error = await asyncio.wait_for(
            _invoke_provider(provider=provider, model=model, messages=chat_messages),
            timeout=timeout_seconds,
        )
    except TimeoutError:
        log.warning("background_review.timeout", timeout_seconds=timeout_seconds)
        return BackgroundReviewReport(
            profile_slug=profile_slug,
            kind=kind,
            started_at=started_at,
            finished_at=_utc_now(),
            writes=[],
            error="timeout",
        )
    except asyncio.CancelledError:
        # Respect cooperative cancellation — re-raise after stamping the
        # report so the gateway can record what we did before the cancel.
        raise
    except Exception as err:
        log.warning("background_review.provider_failure", err=str(err))
        return BackgroundReviewReport(
            profile_slug=profile_slug,
            kind=kind,
            started_at=started_at,
            finished_at=_utc_now(),
            writes=[],
            error=f"provider_failure: {err}",
        )

    if stream_error and not tool_calls:
        # The provider produced no tool calls AND signalled an error;
        # treat as a soft failure so the audit row reflects what
        # happened. ``no_chunks`` collapses to "no writes" which is
        # benign — mock provider returns nothing, that's fine.
        if stream_error == "no_chunks":
            return BackgroundReviewReport(
                profile_slug=profile_slug,
                kind=kind,
                started_at=started_at,
                finished_at=_utc_now(),
                writes=[],
                error=None,
            )
        return BackgroundReviewReport(
            profile_slug=profile_slug,
            kind=kind,
            started_at=started_at,
            finished_at=_utc_now(),
            writes=[],
            error=stream_error,
        )

    try:
        writes = await _apply_tool_calls(
            tool_calls=tool_calls,
            profile_root=profile_root,
            registry=registry,
            now=started_at,
        )
    except Exception as err:
        log.warning("background_review.dispatch_failure", err=str(err))
        return BackgroundReviewReport(
            profile_slug=profile_slug,
            kind=kind,
            started_at=started_at,
            finished_at=_utc_now(),
            writes=[],
            error=f"dispatch_failure: {err}",
        )

    log.info(
        "background_review.completed",
        applied=sum(1 for w in writes if w.applied),
        skipped=sum(1 for w in writes if not w.applied),
    )
    return BackgroundReviewReport(
        profile_slug=profile_slug,
        kind=kind,
        started_at=started_at,
        finished_at=_utc_now(),
        writes=writes,
        error=None,
    )


__all__ = [
    "WHITELISTED_TOOLS",
    "BackgroundReviewReport",
    "ReviewKind",
    "ReviewWriteRecord",
    "load_prompt",
    "spawn_background_review",
]
