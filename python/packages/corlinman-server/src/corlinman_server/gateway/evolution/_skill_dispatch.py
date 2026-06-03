"""Whitelisted-tool dispatch + skill/memory file helpers.

Extracted verbatim from
:mod:`corlinman_server.gateway.evolution.background_review` as part of a
behaviour-preserving god-file split. This module owns the tool-call
dispatcher, the skill/memory write helpers, the safe-name guard, and the
:class:`ReviewWriteRecord` audit row + ``WHITELISTED_TOOLS`` constant +
``_utc_now`` clock that the dispatcher (and the orchestrator) share.

It MUST NOT import the source module (``background_review``) to avoid an
import cycle — the source module re-imports the public names from here.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from corlinman_skills_registry import (
    Skill,
    SkillRegistry,
    SkillRequirements,
    bump_patch,
    write_skill_md,
)

logger = structlog.get_logger(__name__)


# Hard whitelist; the dispatcher refuses anything else. Kept module-level
# so tests can ``in WHITELISTED_TOOLS`` without re-instantiating anything.
WHITELISTED_TOOLS: frozenset[str] = frozenset({"skill_manage", "memory_write"})


@dataclass(frozen=True)
class ReviewWriteRecord:
    """One mutation the LLM proposed and we performed (or skipped).

    ``applied=False`` always carries a ``skipped_reason``; ``applied=True``
    leaves it as ``None``. The dispatcher stamps both shapes uniformly so
    the gateway / UI can render a single audit-row format.
    """

    tool: str           # "skill_manage" | "memory_write"
    action: str         # "create" | "edit" | "patch" | "append" | "replace" | "delete"
    target: str         # skill name or "MEMORY" / "USER"
    applied: bool       # True if we actually wrote
    skipped_reason: str | None = None  # populated when applied=False


# ─── Whitelisted-tool dispatcher ─────────────────────────────────────


# Strict skill-name guard: alphanumeric + dash + underscore. Crucially no
# '/', no '..', no '.', so even if the LLM hallucinates a path-traversal
# attempt, the resulting name fails the regex and the create call is
# dropped before we touch the filesystem.
_SAFE_SKILL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,127}$")


def _is_safe_skill_name(name: object) -> bool:
    """Return True only if ``name`` is a non-empty bare identifier-style
    string. Used by the dispatcher to refuse path-traversal attempts and
    other malformed names without ever calling ``Path.resolve``.
    """
    return isinstance(name, str) and bool(_SAFE_SKILL_NAME.match(name))


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _atomic_write(path: Path, payload: str) -> None:
    """Write ``payload`` to ``path`` atomically.

    Mirrors :func:`corlinman_skills_registry.parse.write_skill_md`'s
    tempfile + ``os.replace`` pattern so MEMORY.md / USER.md writes share
    the same crash-safety guarantees the skill writer offers.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fp:
            fp.write(payload)
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _bump_patch_version(current: str) -> str:
    """Increment a semver patch level. Tolerates malformed input by
    falling back to ``"1.0.1"`` — the field is best-effort metadata, not
    a contract.
    """
    parts = (current or "").split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1])
        patch = int(parts[2])
    except (IndexError, ValueError):
        return "1.0.1"
    return f"{major}.{minor}.{patch + 1}"


def _skill_dir(profile_root: Path, name: str) -> Path:
    """Resolve the directory for skill ``name`` under ``profile_root``.

    Mirrors hermes' ``skills/<name>/SKILL.md`` layout. The caller has
    already passed ``name`` through :func:`_is_safe_skill_name` so we can
    trust the join here.
    """
    return profile_root / "skills" / name


def _skill_md_path(profile_root: Path, name: str) -> Path:
    return _skill_dir(profile_root, name) / "SKILL.md"


async def _apply_tool_calls(
    *,
    tool_calls: list[dict],
    profile_root: Path,
    registry: SkillRegistry,
    review_origin_tag: str = "background_review",
    now: datetime | None = None,
) -> list[ReviewWriteRecord]:
    """Apply each tool call inside the whitelist; drop the rest.

    Each ``tool_call`` is the OpenAI-shape ``{"id", "type": "function",
    "function": {"name", "arguments"}}`` blob, OR the raw flattened
    ``{"tool", "action", "name", ...}`` shape produced by the mock
    provider's fake tool_call path. We accept both so test harnesses
    don't have to mimic the full OpenAI envelope.

    Path-traversal defence: every disk write resolves through the
    whitelisted skill-name regex + ``profile_root`` join so we can never
    escape the profile root. Skill files go through
    :func:`corlinman_skills_registry.write_skill_md`; MEMORY/USER files
    go through :func:`_atomic_write` (same tempfile pattern).
    """
    now = now or _utc_now()
    records: list[ReviewWriteRecord] = []

    for raw_call in tool_calls or []:
        call = _normalise_tool_call(raw_call)
        if call is None:
            records.append(
                ReviewWriteRecord(
                    tool="unknown",
                    action="unknown",
                    target="",
                    applied=False,
                    skipped_reason="malformed_tool_call",
                )
            )
            continue

        tool = call.get("tool")
        if tool not in WHITELISTED_TOOLS:
            records.append(
                ReviewWriteRecord(
                    tool=str(tool or "unknown"),
                    action=str(call.get("action") or "unknown"),
                    target=str(call.get("name") or call.get("target") or ""),
                    applied=False,
                    skipped_reason="not_whitelisted",
                )
            )
            continue

        if tool == "skill_manage":
            records.append(
                await _apply_skill_manage(
                    call,
                    profile_root=profile_root,
                    registry=registry,
                    now=now,
                )
            )
        elif tool == "memory_write":
            records.append(
                _apply_memory_write(
                    call,
                    profile_root=profile_root,
                    now=now,
                )
            )

    return records


def _normalise_tool_call(raw: Any) -> dict[str, Any] | None:
    """Coerce an OpenAI-shape tool_call OR a flat dict into the flat
    ``{"tool", "action", ...}`` shape the dispatcher consumes.

    Returns ``None`` if the shape is unrecognisable — the dispatcher
    surfaces that as a ``malformed_tool_call`` record.
    """
    if not isinstance(raw, dict):
        return None

    # Already-flat shape: ``{"tool": "skill_manage", "action": "create", ...}``
    if "tool" in raw:
        return dict(raw)

    # OpenAI shape: ``{"type": "function", "function": {"name": "...", "arguments": "..."}}``
    fn = raw.get("function") if isinstance(raw.get("function"), dict) else None
    if fn is None:
        return None
    name = fn.get("name")
    args_raw = fn.get("arguments")
    if isinstance(args_raw, str):
        try:
            args = json.loads(args_raw)
        except (TypeError, json.JSONDecodeError):
            return None
    elif isinstance(args_raw, dict):
        args = args_raw
    else:
        args = {}
    if not isinstance(args, dict):
        return None
    args = dict(args)
    args["tool"] = name
    return args


async def _apply_skill_manage(
    call: dict[str, Any],
    *,
    profile_root: Path,
    registry: SkillRegistry,
    now: datetime,
) -> ReviewWriteRecord:
    """Handle one ``skill_manage`` tool call.

    Refuses on malformed names, refuses to delete pinned / non-agent
    skills, otherwise writes through :func:`write_skill_md`.
    """
    action = str(call.get("action") or "")
    name = call.get("name")

    if not _is_safe_skill_name(name):
        return ReviewWriteRecord(
            tool="skill_manage",
            action=action or "unknown",
            target=str(name or ""),
            applied=False,
            skipped_reason="unsafe_name",
        )

    name = str(name)

    if action == "create":
        content = call.get("content")
        if not isinstance(content, str):
            return ReviewWriteRecord(
                tool="skill_manage",
                action=action,
                target=name,
                applied=False,
                skipped_reason="missing_content",
            )
        md_path = _skill_md_path(profile_root, name)
        if md_path.exists():
            return ReviewWriteRecord(
                tool="skill_manage",
                action=action,
                target=name,
                applied=False,
                skipped_reason="already_exists",
            )
        # Build a minimal Skill with the agent-created provenance.
        skill = Skill(
            name=name,
            description=_extract_description(content) or f"Agent-created skill: {name}",
            requires=SkillRequirements(),
            allowed_tools=[],
            body_markdown=content,
            source_path=md_path,
            version="1.0.0",
            origin="agent-created",
            state="active",
            pinned=False,
            created_at=now,
        )
        try:
            write_skill_md(md_path, skill)
        except OSError as err:
            logger.warning(
                "background_review.skill_create.io_error",
                name=name,
                err=str(err),
            )
            return ReviewWriteRecord(
                tool="skill_manage",
                action=action,
                target=name,
                applied=False,
                skipped_reason=f"io_error: {err}",
            )
        return ReviewWriteRecord(
            tool="skill_manage",
            action=action,
            target=name,
            applied=True,
        )

    if action in ("edit", "patch"):
        md_path = _skill_md_path(profile_root, name)
        existing = registry.get(name)
        if existing is None:
            # Fall back to a fresh load from disk in case the registry
            # was constructed before this skill existed (tests, multi-
            # process). If still missing, refuse.
            if not md_path.exists():
                return ReviewWriteRecord(
                    tool="skill_manage",
                    action=action,
                    target=name,
                    applied=False,
                    skipped_reason="not_found",
                )
            # Re-load just this one file via parse_skill.
            from corlinman_skills_registry.parse import parse_skill

            try:
                existing = parse_skill(md_path, md_path.read_text(encoding="utf-8"))
            except Exception as err:
                return ReviewWriteRecord(
                    tool="skill_manage",
                    action=action,
                    target=name,
                    applied=False,
                    skipped_reason=f"parse_error: {err}",
                )

        if action == "edit":
            content = call.get("content")
            if not isinstance(content, str):
                return ReviewWriteRecord(
                    tool="skill_manage",
                    action=action,
                    target=name,
                    applied=False,
                    skipped_reason="missing_content",
                )
            new_body = content
        else:  # patch
            find = call.get("find")
            replace = call.get("replace")
            if not isinstance(find, str) or not isinstance(replace, str):
                return ReviewWriteRecord(
                    tool="skill_manage",
                    action=action,
                    target=name,
                    applied=False,
                    skipped_reason="missing_find_or_replace",
                )
            if find not in existing.body_markdown:
                return ReviewWriteRecord(
                    tool="skill_manage",
                    action=action,
                    target=name,
                    applied=False,
                    skipped_reason="find_not_in_body",
                )
            new_body = existing.body_markdown.replace(find, replace)

        existing.body_markdown = new_body
        existing.version = _bump_patch_version(existing.version)
        if existing.state == "archived":
            existing.state = "active"
        try:
            write_skill_md(md_path, existing)
        except OSError as err:
            return ReviewWriteRecord(
                tool="skill_manage",
                action=action,
                target=name,
                applied=False,
                skipped_reason=f"io_error: {err}",
            )
        # Bump telemetry for the patch — best-effort.
        with contextlib.suppress(OSError):
            bump_patch(md_path.parent, now=now)
        return ReviewWriteRecord(
            tool="skill_manage",
            action=action,
            target=name,
            applied=True,
        )

    if action == "delete":
        md_path = _skill_md_path(profile_root, name)
        existing = registry.get(name)
        if existing is None and md_path.exists():
            from corlinman_skills_registry.parse import parse_skill

            try:
                existing = parse_skill(md_path, md_path.read_text(encoding="utf-8"))
            except Exception:
                existing = None
        if existing is None:
            return ReviewWriteRecord(
                tool="skill_manage",
                action=action,
                target=name,
                applied=False,
                skipped_reason="not_found",
            )
        if existing.pinned or existing.origin != "agent-created":
            return ReviewWriteRecord(
                tool="skill_manage",
                action=action,
                target=name,
                applied=False,
                skipped_reason="protected",
            )
        try:
            md_path.unlink(missing_ok=True)
        except OSError as err:
            return ReviewWriteRecord(
                tool="skill_manage",
                action=action,
                target=name,
                applied=False,
                skipped_reason=f"io_error: {err}",
            )
        return ReviewWriteRecord(
            tool="skill_manage",
            action=action,
            target=name,
            applied=True,
        )

    return ReviewWriteRecord(
        tool="skill_manage",
        action=action or "unknown",
        target=name,
        applied=False,
        skipped_reason="unknown_action",
    )


def _apply_memory_write(
    call: dict[str, Any],
    *,
    profile_root: Path,
    now: datetime,
) -> ReviewWriteRecord:
    """Handle one ``memory_write`` tool call.

    Writes to ``profile_root/MEMORY.md`` or ``profile_root/USER.md``.
    Anything else is refused.
    """
    target = call.get("target")
    action = str(call.get("action") or "")
    content = call.get("content")

    if target not in ("MEMORY", "USER"):
        return ReviewWriteRecord(
            tool="memory_write",
            action=action or "unknown",
            target=str(target or ""),
            applied=False,
            skipped_reason="invalid_target",
        )
    if action not in ("append", "replace"):
        return ReviewWriteRecord(
            tool="memory_write",
            action=action or "unknown",
            target=target,
            applied=False,
            skipped_reason="invalid_action",
        )
    if not isinstance(content, str) or not content.strip():
        return ReviewWriteRecord(
            tool="memory_write",
            action=action,
            target=target,
            applied=False,
            skipped_reason="missing_content",
        )

    path = profile_root / ("MEMORY.md" if target == "MEMORY" else "USER.md")

    if action == "append":
        # One markdown bullet per append, with a timestamp prefix the
        # hermes UI also uses for memory rows.
        timestamp = now.isoformat(timespec="seconds")
        prefix_existing = ""
        if path.exists():
            try:
                prefix_existing = path.read_text(encoding="utf-8")
            except OSError:
                prefix_existing = ""
            if prefix_existing and not prefix_existing.endswith("\n"):
                prefix_existing += "\n"
        new_line = f"- [{timestamp}] {content.strip()}\n"
        payload = prefix_existing + new_line
    else:  # replace
        payload = content if content.endswith("\n") else content + "\n"

    try:
        _atomic_write(path, payload)
    except OSError as err:
        return ReviewWriteRecord(
            tool="memory_write",
            action=action,
            target=target,
            applied=False,
            skipped_reason=f"io_error: {err}",
        )
    return ReviewWriteRecord(
        tool="memory_write",
        action=action,
        target=target,
        applied=True,
    )


def _extract_description(content: str) -> str | None:
    """Pull a short description from the first non-empty body line.

    Cheap default for ``skill_manage(action="create")`` callers that
    don't bother including ``description:`` frontmatter. We strip ``#``
    so the first ``# Title`` line becomes a sane description.
    """
    for line in content.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped:
            return stripped[:200]
    return None
