"""Markdown-with-frontmatter agent-card parser.

W1.2 adds a second on-disk format for ``agents/<name>``: a Markdown
file whose YAML frontmatter carries the per-card metadata and whose
body becomes the ``system_prompt``. The format mirrors Claude Code's
``agents/<name>.md`` shape so operators can copy cards between the
two systems.

Format::

    ---
    description: One-line description.
    model: claude-sonnet-4-6
    provider: anthropic
    tools: ["read_file", "web_search"]
    maxTurns: 50        # silently dropped (not wired yet)
    background: false   # silently dropped (W1.3 wires it)
    skills: ["test-driven-development"]
    variables:
      PROJECT_NAME: corlinman
    ---

    Body becomes system_prompt.

The parser is intentionally permissive about formatting:

* BOM-prefixed files parse cleanly.
* Trailing whitespace on the ``---`` fences is tolerated.
* Frontmatter keys the schema does not know about (``maxTurns``,
  ``background``, anything else) are silently dropped — future waves
  can wire them without breaking this loader.

Hard contracts:

* ``description`` must be present (the operator-facing summary).
* The body (after the closing ``---``) must be non-empty (becomes
  ``system_prompt``).

The filename stem is authoritative for the ``name`` — callers pass it
in explicitly so the parser doesn't have to know about the directory
layout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

from corlinman_agent.agents.card import AgentCard, AgentSource
from corlinman_agent.agents.registry import (
    AgentCardLoadError,
    _as_optional_str,
    _as_str_dict,
    _as_str_list,
)

# Frontmatter keys the parser understands. Anything else is dropped
# silently so older / newer file formats coexist without breaking the
# loader.
_KNOWN_KEYS = {
    "description",
    "model",
    "provider",
    "tools",
    "skills",
    "variables",
    # Tolerated-but-ignored Claude Code fields — listed here so future
    # readers know they're known unknowns rather than typos.
    "maxTurns",
    "background",
    "name",
}


_BOM = "﻿"


def _strip_bom(text: str) -> str:
    """Return ``text`` with a leading UTF-8 BOM removed if present.

    Some editors silently insert a BOM on first save; we don't want
    that to break the frontmatter fence detection.
    """
    if text.startswith(_BOM):
        return text[len(_BOM) :]
    return text


def _split_frontmatter(text: str, *, source_path: Path) -> tuple[str, str]:
    """Split ``---\\nfrontmatter\\n---\\nbody`` into ``(frontmatter, body)``.

    Whitespace-tolerant on the fence lines (``---  `` with trailing
    spaces still counts). Raises :class:`AgentCardLoadError` when no
    closing fence is found or the file does not start with one.
    """
    text = _strip_bom(text)
    # ``splitlines(keepends=False)`` because we re-emit body with ``\n``.
    lines = text.split("\n")
    if not lines or lines[0].rstrip() != "---":
        raise AgentCardLoadError(
            source_path,
            "markdown card must start with a '---' frontmatter fence",
        )
    # Locate the closing fence. Allow trailing whitespace on the fence
    # line so editors that auto-trim or auto-pad don't break loading.
    closing_idx: int | None = None
    for i, line in enumerate(lines[1:], start=1):
        if line.rstrip() == "---":
            closing_idx = i
            break
    if closing_idx is None:
        raise AgentCardLoadError(
            source_path,
            "markdown card has no closing '---' frontmatter fence",
        )
    frontmatter = "\n".join(lines[1:closing_idx])
    body = "\n".join(lines[closing_idx + 1 :])
    return frontmatter, body


def parse_markdown_card(
    text: str,
    *,
    name: str,
    source_path: Path,
    source: AgentSource,
) -> AgentCard:
    """Parse a frontmatter-MD agent definition into an :class:`AgentCard`.

    Parameters
    ----------
    text
        Raw file contents (UTF-8 decoded).
    name
        Authoritative agent name — usually the filename stem.
    source_path
        Path the file came from; threaded through for error messages
        and registry diagnostics.
    source
        Which tier of the stacked-directory loader produced this file.

    Raises
    ------
    AgentCardLoadError
        On missing ``description``, missing body, malformed YAML
        frontmatter, or wrong scalar shapes.
    """
    frontmatter_text, body = _split_frontmatter(text, source_path=source_path)

    try:
        raw: Any = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        raise AgentCardLoadError(
            source_path, f"yaml frontmatter parse error: {exc}"
        ) from exc

    # Empty frontmatter is a dict-less ``None`` — treat as empty mapping
    # so a card with ``description:`` in the body alone is *not* allowed
    # (description is required) but a card whose frontmatter is just
    # comments still produces a clean error.
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise AgentCardLoadError(
            source_path, "frontmatter must be a yaml mapping"
        )
    fm = cast(dict[str, Any], raw)

    # ``name`` in the frontmatter is optional but must agree with the
    # caller-supplied authoritative name (filename stem).
    declared_name = fm.get("name")
    if declared_name is not None:
        if not isinstance(declared_name, str):
            raise AgentCardLoadError(source_path, "name must be a string")
        if declared_name != name:
            raise AgentCardLoadError(
                source_path,
                f"declared name {declared_name!r} does not match {name!r}",
            )

    description = fm.get("description")
    if not isinstance(description, str) or not description.strip():
        raise AgentCardLoadError(
            source_path,
            "description is required and must be a non-empty string",
        )

    system_prompt = body.strip("\n")
    if not system_prompt.strip():
        raise AgentCardLoadError(
            source_path,
            "markdown body (system_prompt) is required and must be non-empty",
        )

    variables = _as_str_dict(fm.get("variables"), "variables", source_path)
    # Claude Code uses ``tools`` / ``skills`` (plural list keys);
    # corlinman's yaml format uses ``tools_allowed`` / ``skill_refs``.
    # Accept both spellings — the MD format mirrors Claude Code.
    tools_allowed = _as_str_list(fm.get("tools"), "tools", source_path)
    skill_refs = _as_str_list(fm.get("skills"), "skills", source_path)

    model = _as_optional_str(fm.get("model"), "model", source_path)
    provider = _as_optional_str(fm.get("provider"), "provider", source_path)

    return AgentCard(
        name=name,
        description=description,
        system_prompt=system_prompt,
        variables=variables,
        tools_allowed=tools_allowed,
        skill_refs=skill_refs,
        source_path=source_path,
        model=model,
        provider=provider,
        source=source,
    )


__all__ = ["parse_markdown_card"]
