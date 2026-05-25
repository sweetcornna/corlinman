"""Tests for :meth:`AgentCardRegistry.load_from_dir_stack` — the
stacked-directory loader that gives operators a built-in / user /
project precedence chain.

Last-tier-wins semantics: a project card shadows the user card,
which shadows the built-in. Bad files in user/project tiers are
logged and skipped instead of breaking the registry boot.
"""

from __future__ import annotations

import logging
from pathlib import Path

from corlinman_agent.agents import AgentCardRegistry


def _yaml(name: str, description: str = "desc", body: str = "you are a helper") -> str:
    return f"""\
name: {name}
description: {description}
system_prompt: |
  {body}
"""


def _md(description: str = "desc", body: str = "You are a helper.") -> str:
    return f"""\
---
description: {description}
---

{body}
"""


def _stack(
    *tiers: tuple[Path, str],
) -> list[tuple[Path, str]]:
    """Tiny helper — typing convenience. Mirrors what the entrypoint
    composes at boot."""
    return [(p, s) for p, s in tiers]


def test_empty_overlays_load_only_builtins(tmp_path: Path) -> None:
    """User + project dirs empty → only the built-in tier shows up."""
    built_in = tmp_path / "built"
    user = tmp_path / "user"
    project = tmp_path / "project"
    built_in.mkdir()
    user.mkdir()
    project.mkdir()
    (built_in / "mentor.yaml").write_text(_yaml("mentor"), encoding="utf-8")

    reg = AgentCardRegistry.load_from_dir_stack(
        _stack((built_in, "built-in"), (user, "user"), (project, "project"))
    )

    assert reg.names() == ["mentor"]
    mentor = reg.get("mentor")
    assert mentor is not None
    assert mentor.source == "built-in"


def test_user_overlay_shadows_builtin(
    tmp_path: Path, caplog: object
) -> None:
    """A user card with the same name overrides the built-in; the
    shadow is logged so operators know they overrode a default."""
    import pytest

    assert isinstance(caplog, pytest.LogCaptureFixture)

    built_in = tmp_path / "built"
    user = tmp_path / "user"
    built_in.mkdir()
    user.mkdir()
    (built_in / "mentor.yaml").write_text(
        _yaml("mentor", description="builtin"), encoding="utf-8"
    )
    (user / "mentor.yaml").write_text(
        _yaml("mentor", description="user-override"), encoding="utf-8"
    )

    with caplog.at_level(logging.WARNING, logger="corlinman_agent.agents.registry"):
        reg = AgentCardRegistry.load_from_dir_stack(
            _stack((built_in, "built-in"), (user, "user"))
        )

    mentor = reg.get("mentor")
    assert mentor is not None
    assert mentor.description == "user-override"
    assert mentor.source == "user"
    # Operator-visible warning that we shadowed a built-in.
    assert any("shadow" in record.message for record in caplog.records)


def test_project_overlay_shadows_user(tmp_path: Path) -> None:
    """Three tiers: project beats user beats built-in."""
    built_in = tmp_path / "built"
    user = tmp_path / "user"
    project = tmp_path / "project"
    for d in (built_in, user, project):
        d.mkdir()
    (built_in / "mentor.yaml").write_text(
        _yaml("mentor", description="builtin"), encoding="utf-8"
    )
    (user / "mentor.yaml").write_text(
        _yaml("mentor", description="user"), encoding="utf-8"
    )
    (project / "mentor.yaml").write_text(
        _yaml("mentor", description="project"), encoding="utf-8"
    )

    reg = AgentCardRegistry.load_from_dir_stack(
        _stack((built_in, "built-in"), (user, "user"), (project, "project"))
    )

    mentor = reg.get("mentor")
    assert mentor is not None
    assert mentor.description == "project"
    assert mentor.source == "project"


def test_broken_yaml_in_user_overlay_is_skipped(
    tmp_path: Path, caplog: object
) -> None:
    """A malformed user-overlay card must be logged but must NOT break
    the built-in tier — operators can keep working with defaults while
    they fix their custom card."""
    import pytest

    assert isinstance(caplog, pytest.LogCaptureFixture)

    built_in = tmp_path / "built"
    user = tmp_path / "user"
    built_in.mkdir()
    user.mkdir()
    (built_in / "mentor.yaml").write_text(_yaml("mentor"), encoding="utf-8")
    (user / "broken.yaml").write_text("nope: : :", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="corlinman_agent.agents.registry"):
        reg = AgentCardRegistry.load_from_dir_stack(
            _stack((built_in, "built-in"), (user, "user"))
        )

    # Built-in still loaded.
    assert "mentor" in reg
    # Broken card not registered.
    assert "broken" not in reg
    assert any(
        "load_error" in record.message or "broken.yaml" in record.message
        for record in caplog.records
    )


def test_md_overlay_loads(tmp_path: Path) -> None:
    """``.md`` files with frontmatter are loaded alongside ``.yaml``."""
    user = tmp_path / "user"
    user.mkdir()
    (user / "researcher.md").write_text(_md(), encoding="utf-8")

    reg = AgentCardRegistry.load_from_dir_stack(_stack((user, "user")))

    researcher = reg.get("researcher")
    assert researcher is not None
    assert researcher.source == "user"
    assert researcher.system_prompt == "You are a helper."


def test_md_with_missing_description_is_skipped(
    tmp_path: Path, caplog: object
) -> None:
    """A frontmatter MD without ``description`` is logged + skipped in
    overlay tiers (matches yaml-with-missing-required behavior)."""
    import pytest

    assert isinstance(caplog, pytest.LogCaptureFixture)

    user = tmp_path / "user"
    user.mkdir()
    (user / "incomplete.md").write_text(
        "---\nmodel: x\n---\n\nbody\n", encoding="utf-8"
    )

    with caplog.at_level(logging.WARNING, logger="corlinman_agent.agents.registry"):
        reg = AgentCardRegistry.load_from_dir_stack(_stack((user, "user")))

    assert "incomplete" not in reg
    assert any("load_error" in r.message for r in caplog.records)


def test_md_with_unknown_fields_is_accepted(tmp_path: Path) -> None:
    """Claude Code-specific keys silently pass through — the card still
    loads, the unknown fields are dropped."""
    user = tmp_path / "user"
    user.mkdir()
    (user / "tolerant.md").write_text(
        "---\n"
        "description: tolerant\n"
        "maxTurns: 50\n"
        "background: true\n"
        "---\n\n"
        "Body.\n",
        encoding="utf-8",
    )

    reg = AgentCardRegistry.load_from_dir_stack(_stack((user, "user")))

    card = reg.get("tolerant")
    assert card is not None
    assert card.description == "tolerant"


def test_underscore_prefixed_files_are_ignored(tmp_path: Path) -> None:
    """Files starting with ``_`` are treated as fragments and skipped."""
    user = tmp_path / "user"
    user.mkdir()
    (user / "_partial.yaml").write_text(_yaml("partial"), encoding="utf-8")
    (user / "kept.yaml").write_text(_yaml("kept"), encoding="utf-8")

    reg = AgentCardRegistry.load_from_dir_stack(_stack((user, "user")))

    assert reg.names() == ["kept"]


def test_hidden_files_are_ignored(tmp_path: Path) -> None:
    """Dotfiles (.DS_Store, .gitkeep, …) are skipped."""
    user = tmp_path / "user"
    user.mkdir()
    (user / ".DS_Store").write_text("garbage", encoding="utf-8")
    (user / ".hidden.yaml").write_text(_yaml("hidden"), encoding="utf-8")
    (user / "visible.yaml").write_text(_yaml("visible"), encoding="utf-8")

    reg = AgentCardRegistry.load_from_dir_stack(_stack((user, "user")))

    assert reg.names() == ["visible"]


def test_collision_within_tier_yaml_wins(
    tmp_path: Path, caplog: object
) -> None:
    """When a tier has both ``foo.yaml`` and ``foo.md``, the sorted scan
    + first-seen-wins rule gives ``foo.md`` priority over ``foo.yaml``
    (alphabetical), and the loser is logged. We assert the *behaviour*
    (one card wins, one is logged) rather than the specific tie-break,
    so future refactors can shift policy without churning this test."""
    import pytest

    assert isinstance(caplog, pytest.LogCaptureFixture)

    user = tmp_path / "user"
    user.mkdir()
    (user / "foo.yaml").write_text(
        _yaml("foo", description="yaml-version"), encoding="utf-8"
    )
    (user / "foo.md").write_text(
        _md(description="md-version"), encoding="utf-8"
    )

    with caplog.at_level(logging.WARNING, logger="corlinman_agent.agents.registry"):
        reg = AgentCardRegistry.load_from_dir_stack(_stack((user, "user")))

    foo = reg.get("foo")
    assert foo is not None
    # Sorted iterdir → foo.md comes before foo.yaml; first one wins.
    assert foo.description == "md-version"
    assert any(
        "duplicate_in_tier" in r.message or "foo.yaml" in r.message
        for r in caplog.records
    )


def test_nonexistent_dir_is_silently_ignored(tmp_path: Path) -> None:
    """A user/project overlay that hasn't been created yet must not
    break boot — the registry just skips it."""
    built_in = tmp_path / "built"
    user = tmp_path / "doesnotexist"
    built_in.mkdir()
    (built_in / "mentor.yaml").write_text(_yaml("mentor"), encoding="utf-8")

    reg = AgentCardRegistry.load_from_dir_stack(
        _stack((built_in, "built-in"), (user, "user"))
    )

    assert "mentor" in reg
    assert len(reg) == 1


def test_load_from_dir_still_tags_builtin_source(tmp_path: Path) -> None:
    """Legacy :meth:`load_from_dir` keeps working; cards default to the
    ``"built-in"`` tier so pre-W1.2 callers see no behaviour change."""
    (tmp_path / "mentor.yaml").write_text(_yaml("mentor"), encoding="utf-8")
    reg = AgentCardRegistry.load_from_dir(tmp_path)
    mentor = reg.get("mentor")
    assert mentor is not None
    assert mentor.source == "built-in"


def test_cards_helper_returns_sorted_list(tmp_path: Path) -> None:
    """``AgentCardRegistry.cards()`` is the admin list endpoint's input."""
    user = tmp_path / "user"
    user.mkdir()
    (user / "b.yaml").write_text(_yaml("b"), encoding="utf-8")
    (user / "a.yaml").write_text(_yaml("a"), encoding="utf-8")
    reg = AgentCardRegistry.load_from_dir_stack(_stack((user, "user")))
    assert [c.name for c in reg.cards()] == ["a", "b"]
