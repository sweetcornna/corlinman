"""Reference-markdown skip in the gateway-side ``SkillRegistry``.

Nested skills split into ``<name>/SKILL.md + references/*.md`` for
progressive disclosure. The reference files carry no YAML frontmatter,
so the walk must exclude them from both the parse list and the load
cache fingerprint — before this skip a single ``references/*.md`` file
made ``load_from_dir`` raise ``MissingFieldError`` and broke the
curator / admin skill listings.
"""

from __future__ import annotations

from pathlib import Path

from corlinman_skills_registry import SkillRegistry

_SKILL_MD = (
    "---\n"
    "name: {name}\n"
    "description: d\n"
    "---\n"
    "body of {name}\n"
)


def _make_tree(root: Path) -> None:
    d = root / "giant"
    (d / "references").mkdir(parents=True)
    (d / "SKILL.md").write_text(_SKILL_MD.format(name="giant"), encoding="utf-8")
    (d / "references" / "topic.md").write_text(
        "# Reference prose\n\nNo frontmatter here, on purpose.\n",
        encoding="utf-8",
    )
    # Frontmatter-less sibling next to SKILL.md is reference material too.
    (d / "NOTES.md").write_text("stray notes\n", encoding="utf-8")
    (root / "flat.md").write_text(_SKILL_MD.format(name="flat"), encoding="utf-8")


def test_load_from_dir_skips_reference_markdown(tmp_path: Path) -> None:
    _make_tree(tmp_path)

    reg = SkillRegistry.load_from_dir(tmp_path)

    assert reg.names() == ["flat", "giant"]
    giant = reg.get("giant")
    assert giant is not None
    assert Path(giant.source_path).name == "SKILL.md"


def test_reference_edit_does_not_invalidate_cache_fingerprint(
    tmp_path: Path,
) -> None:
    """Reference files are excluded from the PERF-03 fingerprint: editing
    one cannot change the parse output, so the cached parse stays valid."""
    _make_tree(tmp_path)
    first = SkillRegistry.load_from_dir(tmp_path)

    ref = tmp_path / "giant" / "references" / "topic.md"
    ref.write_text("edited reference prose\n", encoding="utf-8")

    second = SkillRegistry.load_from_dir(tmp_path)
    assert second.names() == first.names() == ["flat", "giant"]
