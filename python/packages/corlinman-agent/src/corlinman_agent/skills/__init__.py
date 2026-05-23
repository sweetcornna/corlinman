"""Skill-card subsystem — Python mirror of the Rust ``corlinman-skills`` crate.

A **skill** is a reusable Markdown fragment (with YAML frontmatter) that an
agent card may reference via ``skill_refs``. The context assembler injects
the ``body_markdown`` of each referenced skill into the system prompt as
part of prompt assembly.

This package is the Python-side mirror of the Rust implementation in
``rust/crates/corlinman-skills``. The Rust copy is authoritative for the
gateway admin API; this Python copy exists because prompt assembly runs
in-process with the reasoning loop, and a single in-process call beats a
round-trip gRPC hop over UDS for every request.

Design constraints:

* Shape matches the Rust ``Skill`` / ``SkillRequirements`` structs so the
  two implementations can diverge only via explicit porting, not
  accidental drift.
* No network / disk I/O beyond :meth:`SkillRegistry.load_from_dir`, so
  ``check_requirements`` is cheap enough to call on every assembly.
"""

from __future__ import annotations

from corlinman_agent.skills.card import Skill, SkillRequirements
from corlinman_agent.skills.registry import (
    RefreshDelta,
    SkillLoadError,
    SkillRegistry,
)

__all__ = [
    "RefreshDelta",
    "Skill",
    "SkillLoadError",
    "SkillRegistry",
    "SkillRequirements",
]
