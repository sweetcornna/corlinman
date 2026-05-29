"""Tests for the frontmatter-MD agent-card parser.

The parser is intentionally permissive about whitespace and BOM
prefixes (real-world editors stamp those in) but strict about the
two hard contracts: ``description`` must be present and the body
must be non-empty.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from corlinman_agent.agents.markdown import parse_markdown_card
from corlinman_agent.agents.registry import AgentCardLoadError


def _make(path: Path, text: str) -> Path:
    """Write ``text`` to ``path`` and return it for chaining."""
    path.write_text(text, encoding="utf-8")
    return path


def test_minimal_card_parses(tmp_path: Path) -> None:
    """description + non-empty body is the smallest valid card."""
    text = """\
---
description: A small agent.
---

You are a small helper.
"""
    p = _make(tmp_path / "tiny.md", text)
    card = parse_markdown_card(text, name="tiny", source_path=p, source="user")

    assert card.name == "tiny"
    assert card.description == "A small agent."
    assert card.system_prompt == "You are a small helper."
    assert card.variables == {}
    assert card.tools_allowed == []
    assert card.skill_refs == []
    assert card.model is None
    assert card.provider is None
    assert card.source == "user"
    assert card.source_path == p


def test_full_frontmatter_populates_every_field(tmp_path: Path) -> None:
    """Every known scalar / list / mapping field round-trips."""
    text = """\
---
description: Heavyweight test card.
model: claude-sonnet-4-6
provider: anthropic
show_action_trace: false
tools:
  - read_file
  - web_search
skills:
  - test-driven-development
variables:
  PROJECT_NAME: corlinman
  COUNT: 5
---

# Body becomes system_prompt

Multi-paragraph
body content.
"""
    p = _make(tmp_path / "heavy.md", text)
    card = parse_markdown_card(
        text, name="heavy", source_path=p, source="project"
    )

    assert card.model == "claude-sonnet-4-6"
    assert card.provider == "anthropic"
    assert card.show_action_trace is False
    assert card.tools_allowed == ["read_file", "web_search"]
    assert card.skill_refs == ["test-driven-development"]
    # ``5`` stringified — keeps the expander's substitution type-safe.
    assert card.variables == {"PROJECT_NAME": "corlinman", "COUNT": "5"}
    assert card.source == "project"
    assert "# Body becomes system_prompt" in card.system_prompt
    assert "Multi-paragraph" in card.system_prompt


def test_empty_body_is_rejected(tmp_path: Path) -> None:
    """system_prompt is required; whitespace-only body fails."""
    text = """\
---
description: Has frontmatter but no body.
---


"""
    p = _make(tmp_path / "empty.md", text)
    with pytest.raises(AgentCardLoadError) as exc:
        parse_markdown_card(text, name="empty", source_path=p, source="user")
    assert "system_prompt" in exc.value.reason or "non-empty" in exc.value.reason


def test_missing_description_is_rejected(tmp_path: Path) -> None:
    """description is the operator-facing summary and must be set."""
    text = """\
---
model: claude-sonnet-4-6
---

Body without a description.
"""
    p = _make(tmp_path / "nodesc.md", text)
    with pytest.raises(AgentCardLoadError) as exc:
        parse_markdown_card(
            text, name="nodesc", source_path=p, source="user"
        )
    assert "description" in exc.value.reason


def test_unknown_fields_are_dropped(tmp_path: Path) -> None:
    """Claude Code-specific keys (maxTurns, background) and totally
    unknown keys are silently dropped — older / newer files coexist."""
    text = """\
---
description: Tolerant card.
maxTurns: 50
background: false
exoticField: ignore-me
---

Body.
"""
    p = _make(tmp_path / "tolerant.md", text)
    card = parse_markdown_card(
        text, name="tolerant", source_path=p, source="user"
    )

    assert card.description == "Tolerant card."
    assert card.system_prompt == "Body."
    # No errors raised even though the parser doesn't know maxTurns /
    # background / exoticField.


def test_bom_prefix_parses(tmp_path: Path) -> None:
    """A BOM (\\ufeff) at the start of the file must not break the
    frontmatter fence detection."""
    text = "﻿---\ndescription: BOM-prefixed.\n---\n\nBody after BOM.\n"
    p = _make(tmp_path / "bom.md", text)
    card = parse_markdown_card(text, name="bom", source_path=p, source="user")
    assert card.description == "BOM-prefixed."
    assert "Body after BOM" in card.system_prompt


def test_closing_fence_trailing_whitespace_tolerated(tmp_path: Path) -> None:
    """Editors that auto-pad trailing whitespace on the closing fence
    line must not break loading."""
    text = "---  \ndescription: padded fence.\n---   \n\nBody.\n"
    p = _make(tmp_path / "pad.md", text)
    card = parse_markdown_card(text, name="pad", source_path=p, source="user")
    assert card.description == "padded fence."
    assert card.system_prompt == "Body."


def test_missing_opening_fence_is_rejected(tmp_path: Path) -> None:
    """No opening ``---`` → load error (we're not a raw-md parser)."""
    text = "description: no fence.\n\nBody.\n"
    p = _make(tmp_path / "nofence.md", text)
    with pytest.raises(AgentCardLoadError) as exc:
        parse_markdown_card(
            text, name="nofence", source_path=p, source="user"
        )
    assert "fence" in exc.value.reason


def test_missing_closing_fence_is_rejected(tmp_path: Path) -> None:
    """Opening fence but no closing fence is malformed."""
    text = "---\ndescription: half-fenced.\n\nBody without closing.\n"
    p = _make(tmp_path / "halffence.md", text)
    with pytest.raises(AgentCardLoadError) as exc:
        parse_markdown_card(
            text, name="halffence", source_path=p, source="user"
        )
    assert "closing" in exc.value.reason


def test_name_mismatch_is_rejected(tmp_path: Path) -> None:
    """If the frontmatter carries ``name:`` it must match the filename."""
    text = """\
---
name: other-name
description: card with conflicting name.
---

Body.
"""
    p = _make(tmp_path / "stem.md", text)
    with pytest.raises(AgentCardLoadError) as exc:
        parse_markdown_card(text, name="stem", source_path=p, source="user")
    assert "name" in exc.value.reason


def test_malformed_yaml_frontmatter_raises(tmp_path: Path) -> None:
    """A syntactically invalid frontmatter must raise, not corrupt the
    parsed card with partial data."""
    text = """\
---
description: : : :
---

Body.
"""
    p = _make(tmp_path / "bad.md", text)
    with pytest.raises(AgentCardLoadError) as exc:
        parse_markdown_card(text, name="bad", source_path=p, source="user")
    # The error reason carries the underlying yaml error verbatim — we
    # only assert that it mentions "yaml" / "frontmatter" so we don't
    # over-fit to libyaml's wording.
    assert "yaml" in exc.value.reason.lower() or "frontmatter" in exc.value.reason
