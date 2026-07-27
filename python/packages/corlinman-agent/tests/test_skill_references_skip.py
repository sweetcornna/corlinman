"""Reference-markdown skip in :class:`SkillRegistry`.

Giant bundled skills split into ``<name>/SKILL.md + references/*.md``
(progressive disclosure — the model pulls a reference lazily via the
``Skill`` tool's ``file`` argument). Reference files carry no YAML
frontmatter, so the registry walk must NOT try to parse them as skills:
before this skip, a single ``references/workflow.md`` under the skills
root made ``load_from_dir`` raise ``SkillLoadError`` and brick boot.
"""

from __future__ import annotations

from pathlib import Path

from corlinman_agent.skills import SkillRegistry

_SKILL_MD = (
    "---\n"
    "name: {name}\n"
    "description: d\n"
    "---\n"
    "body of {name}\n"
)


def _nested_skill(root: Path, dirname: str, name: str) -> Path:
    d = root / dirname
    (d / "references").mkdir(parents=True)
    (d / "SKILL.md").write_text(_SKILL_MD.format(name=name), encoding="utf-8")
    (d / "references" / "topic.md").write_text(
        "# Reference prose\n\nNo frontmatter here, on purpose.\n",
        encoding="utf-8",
    )
    return d


def test_load_from_dir_skips_reference_markdown(tmp_path: Path) -> None:
    """references/*.md under a nested skill must not be parsed as skills."""
    _nested_skill(tmp_path, "giant", "giant")
    # A frontmatter-less sibling right next to SKILL.md is also reference
    # material (claude-code convention: only SKILL.md is the entrypoint).
    (tmp_path / "giant" / "NOTES.md").write_text("stray notes\n", encoding="utf-8")
    # Flat skills at the root keep working unchanged.
    (tmp_path / "flat.md").write_text(_SKILL_MD.format(name="flat"), encoding="utf-8")

    registry = SkillRegistry.load_from_dir(tmp_path)

    assert registry.names() == ["flat", "giant"]
    giant = registry.get("giant")
    assert giant is not None
    assert giant.source_path == tmp_path / "giant" / "SKILL.md"


def test_refresh_ignores_reference_markdown_changes(tmp_path: Path) -> None:
    """Adding / deleting a reference file must never surface in the delta."""
    _nested_skill(tmp_path, "giant", "giant")
    registry = SkillRegistry.load_from_dir(tmp_path)
    assert registry.names() == ["giant"]

    # Drop a brand-new frontmatter-less reference after boot.
    (tmp_path / "giant" / "references" / "extra.md").write_text(
        "more reference prose\n", encoding="utf-8"
    )
    delta = registry.refresh(force=True)
    assert not delta
    assert registry.names() == ["giant"]

    # Deleting it is equally invisible.
    (tmp_path / "giant" / "references" / "extra.md").unlink()
    delta = registry.refresh(force=True)
    assert not delta
    assert registry.names() == ["giant"]
