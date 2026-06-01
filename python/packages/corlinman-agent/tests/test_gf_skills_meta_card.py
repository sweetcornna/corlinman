"""Gap-fill (lane-skills-meta) tests: in-agent skill card/registry fidelity.

The agent-side ``Skill`` card and ``SkillRegistry`` must surface the
progressive-disclosure metadata (``whenToUse`` / ``paths`` / ``platforms`` /
``model`` / ``effort`` / ``hooks`` / ``disable-model-invocation``) parsed from
SKILL.md frontmatter, and the registry's model-facing catalog must honour
``disable_model_invocation`` by excluding such skills from the selectable set.

Uniquely named ``test_gf_skills_meta_*`` to avoid sibling-lane collisions.
"""

from __future__ import annotations

from pathlib import Path

from corlinman_agent.skills.card import Skill
from corlinman_agent.skills.registry import SkillRegistry, _parse_skill


def _write(tmp_path: Path, rel: str, text: str) -> Path:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_gf_skills_meta_card_carries_new_fields(tmp_path: Path) -> None:
    text = (
        "---\n"
        "name: meta\n"
        "description: d\n"
        "allowed-tools:\n"
        "  - web.search\n"
        "whenToUse: when the user asks about X\n"
        "paths:\n"
        "  - src/**/*.py\n"
        "platforms:\n"
        "  - darwin\n"
        "  - linux\n"
        "model: claude-opus-4-8\n"
        "effort: high\n"
        "hooks:\n"
        "  pre_tool:\n"
        "    - match: Bash\n"
        "      deny: true\n"
        "disable-model-invocation: true\n"
        "---\n"
        "body\n"
    )
    path = _write(tmp_path, "meta/SKILL.md", text)
    skill = _parse_skill(path, text)

    assert skill.when_to_use == "when the user asks about X"
    assert skill.paths == ["src/**/*.py"]
    assert skill.platforms == ["darwin", "linux"]
    assert skill.model == "claude-opus-4-8"
    assert skill.effort == "high"
    assert skill.hooks == {"pre_tool": [{"match": "Bash", "deny": True}]}
    assert skill.disable_model_invocation is True


def test_gf_skills_meta_legacy_card_defaults() -> None:
    text = "---\nname: legacy\ndescription: d\n---\nbody\n"
    skill = _parse_skill(Path("/tmp/legacy.md"), text)
    assert skill.when_to_use is None
    assert skill.paths == []
    assert skill.platforms == []
    assert skill.model is None
    assert skill.effort is None
    assert skill.hooks == {}
    assert skill.disable_model_invocation is False


def test_gf_skills_meta_catalog_entry_shape() -> None:
    skill = Skill(
        name="x",
        description="d",
        when_to_use="hint",
        paths=["a"],
        platforms=["linux"],
        model="m",
        effort="low",
        allowed_tools=["t"],
        disable_model_invocation=True,
    )
    entry = skill.catalog_entry()
    assert entry["name"] == "x"
    assert entry["when_to_use"] == "hint"
    assert entry["paths"] == ["a"]
    assert entry["platforms"] == ["linux"]
    assert entry["disable_model_invocation"] is True
    # The body is intentionally NOT in the compact catalog row (progressive
    # disclosure pulls it on demand, not up front).
    assert "body_markdown" not in entry


def test_gf_skills_meta_catalog_excludes_disabled(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "auto/SKILL.md",
        "---\nname: auto\ndescription: selectable\n---\nbody\n",
    )
    _write(
        tmp_path,
        "manual/SKILL.md",
        (
            "---\nname: manual\ndescription: explicit-only\n"
            "disable-model-invocation: true\n---\nbody\n"
        ),
    )
    reg = SkillRegistry.load_from_dir(tmp_path)

    # Both skills are loaded and reachable by name...
    assert set(reg.names()) == {"auto", "manual"}
    assert reg.get("manual") is not None

    # ...but only the auto-selectable one shows in the model-facing surfaces.
    assert reg.model_invokable_names() == ["auto"]
    catalog_names = [row["name"] for row in reg.catalog()]
    assert catalog_names == ["auto"]


def test_gf_skills_meta_registry_rejects_bad_hooks_shape(tmp_path: Path) -> None:
    import pytest
    from corlinman_agent.skills.registry import SkillLoadError

    path = _write(
        tmp_path,
        "bad/SKILL.md",
        "---\nname: bad\ndescription: d\nhooks: not-a-mapping\n---\nbody\n",
    )
    with pytest.raises(SkillLoadError):
        _parse_skill(path, path.read_text(encoding="utf-8"))
