"""Skill-card dataclass — mirrors ``rust/crates/corlinman-skills/src/skill.rs``.

A ``Skill`` is parsed from a ``SKILL.md``-style file: YAML frontmatter
fenced by ``---`` delimiters followed by a Markdown body. The body is
preserved verbatim so downstream prompt injection can paste it without
reformatting surprises.

Only the fields the context assembler actually uses are modelled; other
frontmatter keys are ignored so operators can carry metadata for sister
tooling without breaking our loader.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SkillRequirements:
    """Runtime prerequisites a skill needs before injection is allowed.

    Every list defaults to empty — an unmet item yields a human-readable
    message from :meth:`SkillRegistry.check_requirements`.
    """

    # All binaries in this list must be on ``$PATH``.
    bins: list[str] = field(default_factory=list)
    # Any ONE binary in this list must be on ``$PATH``.
    any_bins: list[str] = field(default_factory=list)
    # Dotted config keys (e.g. ``providers.brave.api_key``) that must
    # resolve to a non-empty string via the caller-supplied lookup.
    config: list[str] = field(default_factory=list)
    # Environment variables that must be set to a non-empty value.
    env: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Skill:
    """A single skill parsed from a ``SKILL.md`` file.

    Attributes
    ----------
    name
        Unique identifier. Agents refer to a skill by this name in their
        ``skill_refs`` list.
    description
        Short human summary shown in listings; not injected into prompts.
    emoji
        Optional glyph used by the CLI/UI.
    requires
        Runtime prerequisites enforced before body injection.
    install
        Optional install hint surfaced when ``requires`` isn't satisfied.
    allowed_tools
        Tools this skill is allowed to invoke at runtime. Enforcement
        happens elsewhere; we just carry the list.
    when_to_use
        Free-text hint the model reads to decide whether to pull this
        skill's body on demand (progressive disclosure). Surfaced in the
        catalog the context assembler builds.
    paths
        Glob/path hints describing where this skill is relevant.
    platforms
        Platforms this skill targets (e.g. ``darwin``/``linux``); lets the
        catalog hide skills that don't apply to the running host.
    model
        Preferred model id for the skill's task (metadata only here).
    effort
        Reasoning-effort hint (``low``/``medium``/``high``).
    hooks
        Skill-scoped hook declarations, carried verbatim as a mapping for
        the hook runner to register.
    disable_model_invocation
        When ``True`` the model must NOT auto-select / auto-inject this
        skill — it is only available when explicitly referenced. The
        catalog / context assembler honours this to keep noisy skills out
        of the model's selectable set.
    body_markdown
        The Markdown body (everything after the closing ``---`` of the
        frontmatter), preserved verbatim for prompt injection.
    source_path
        Absolute path to the file this skill was loaded from; useful for
        error messages and admin tooling.
    """

    name: str
    description: str
    emoji: str | None = None
    requires: SkillRequirements = field(default_factory=SkillRequirements)
    install: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    when_to_use: str | None = None
    paths: list[str] = field(default_factory=list)
    platforms: list[str] = field(default_factory=list)
    model: str | None = None
    effort: str | None = None
    # ``frozen=True`` forbids a plain ``dict`` default would-be-mutated; we
    # still want a per-instance empty mapping, so default_factory it.
    hooks: dict[str, Any] = field(default_factory=dict)
    disable_model_invocation: bool = False
    body_markdown: str = ""
    source_path: Path | None = None

    def catalog_entry(self) -> dict[str, Any]:
        """A compact, model-facing catalog row for progressive disclosure.

        The wiring lane (context assembler / servicer) builds the narrowed
        skill catalog the model sees instead of every full body. This helper
        gives that lane a stable shape to render: identity + the
        selection-relevant metadata, and crucially the
        :attr:`disable_model_invocation` flag so a skill the operator marked
        non-auto-selectable can be filtered out (or rendered greyed) without
        the caller reaching into private fields.
        """
        return {
            "name": self.name,
            "description": self.description,
            "emoji": self.emoji,
            "when_to_use": self.when_to_use,
            "paths": list(self.paths),
            "platforms": list(self.platforms),
            "model": self.model,
            "effort": self.effort,
            "allowed_tools": list(self.allowed_tools),
            "disable_model_invocation": self.disable_model_invocation,
        }


__all__ = ["Skill", "SkillRequirements"]
