"""Tests for :mod:`corlinman_server.gateway.lifecycle.starter_skills`.

Covers the first-boot starter-SKILL.md bundle that lets a freshly-
installed gateway boot with a working library of procedural skills
("plan", "test-driven-development", "deep-research", …) under the
default profile's ``skills/`` directory without operator copy-paste.

Tests assert:

* :func:`bundled_skills_root` resolves the in-wheel package data when
  no env override is set.
* :func:`seed_starter_skills` copies every bundled ``*.md`` into an
  empty target on first call and is idempotent on the second call
  (existing files are left in place, not overwritten).
* Pre-existing skill files in the target are listed under ``skipped``
  and their bodies are preserved — operator edits stick across reboots.
* ``CORLINMAN_BUNDLED_SKILLS_DIR`` env override takes precedence; a
  pointing-at-nothing override falls back to the packaged bundle so
  boot never silently runs with an empty registry.
* Missing source (env override pointing nowhere AND package data
  absent) returns a no-op report instead of raising — degraded boot
  is acceptable, crashing is not.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from corlinman_server.gateway.lifecycle import starter_skills

# ---------------------------------------------------------------------------
# bundled_skills_root
# ---------------------------------------------------------------------------


def test_bundled_skills_root_resolves_package_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no override, we should resolve the in-wheel bundle dir."""
    monkeypatch.delenv("CORLINMAN_BUNDLED_SKILLS_DIR", raising=False)
    root = starter_skills.bundled_skills_root()
    assert root is not None
    assert root.is_dir()
    # Spot-check a few canonical bundled skills exist.
    for name in (
        "plan.md",
        "test-driven-development.md",
        "memory.md",
        "visual-output-quality.md",
    ):
        assert (root / name).is_file(), f"missing bundled skill: {name}"
    # W1 third-party bundle: huashu-design / nuwa-skill / darwin-skill
    # ship as nested <name>/SKILL.md alongside the flat starter skills.
    for nested in ("huashu-design", "nuwa-skill", "darwin-skill"):
        assert (root / nested / "SKILL.md").is_file(), (
            f"missing third-party bundled skill: {nested}/SKILL.md"
        )


def test_bundled_third_party_skills_are_loadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 3 third-party skills (huashu-design / nuwa-skill /
    darwin-skill) must parse cleanly through ``SkillRegistry`` and
    surface under their declared frontmatter names. This is the
    regression net against a future re-bundle that drops a stray
    non-skill ``.md`` into one of the trimmed subdirs — the
    registry's walk is recursive and treats every ``*.md`` as a
    skill, so a stray README anywhere under ``huashu-design/`` would
    crash the loader."""
    monkeypatch.delenv("CORLINMAN_BUNDLED_SKILLS_DIR", raising=False)
    from corlinman_skills_registry import SkillRegistry  # noqa: PLC0415

    root = starter_skills.bundled_skills_root()
    assert root is not None
    reg = SkillRegistry.load_from_dir(root)
    names = set(reg.names())
    # NOTE: nuwa-skill's frontmatter declares ``name: huashu-nuwa``
    # (it's part of the 花叔 family); the directory name and the
    # registry key intentionally differ.
    for expected in ("huashu-design", "huashu-nuwa", "darwin-skill"):
        assert expected in names, f"{expected} not registered (got: {sorted(names)})"
        skill = reg.get(expected)
        assert skill is not None
        assert skill.description.strip(), (
            f"{expected} has empty description — won't trigger on user queries"
        )


def test_bundled_visual_output_quality_skill_is_loadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compact visual guardrail skill must ship in the starter bundle.

    Without this package data the always-on ref degrades into a logged
    missing-skill warning, and main-chat image/PDF generation falls back to
    improvising layout without screenshot checks.
    """

    monkeypatch.delenv("CORLINMAN_BUNDLED_SKILLS_DIR", raising=False)
    from corlinman_skills_registry import SkillRegistry  # noqa: PLC0415

    root = starter_skills.bundled_skills_root()
    assert root is not None
    reg = SkillRegistry.load_from_dir(root)
    skill = reg.get("visual-output-quality")

    assert skill is not None
    assert "PDF" in skill.description
    body = (root / "visual-output-quality.md").read_text("utf-8")
    assert "overlap" in body
    assert "Playwright" in body


def test_bundled_skills_root_env_override_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``CORLINMAN_BUNDLED_SKILLS_DIR`` selects an alternate bundle."""
    custom = tmp_path / "private_bundle"
    custom.mkdir()
    (custom / "only-here.md").write_text(
        "---\nname: only-here\ndescription: x\n---\n# body\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CORLINMAN_BUNDLED_SKILLS_DIR", str(custom))

    root = starter_skills.bundled_skills_root()
    assert root == custom
    assert (root / "only-here.md").is_file()


def test_bundled_skills_root_missing_env_falls_back_to_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An env path pointing at nowhere should not crash boot.

    We log a warning and fall back to the packaged bundle so the
    operator's typo doesn't silently leave the default profile with
    zero skills.
    """
    monkeypatch.setenv(
        "CORLINMAN_BUNDLED_SKILLS_DIR", str(tmp_path / "does_not_exist")
    )
    root = starter_skills.bundled_skills_root()
    # We fell back; the packaged bundle is non-None on a normal install.
    assert root is not None
    assert root.is_dir()


# ---------------------------------------------------------------------------
# seed_starter_skills
# ---------------------------------------------------------------------------


def test_seed_starter_skills_copies_every_bundled_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First call into an empty target copies every bundled skill.

    Two layouts ship in the bundle:

    * **Flat** — top-level ``<name>.md`` — copied as a single file.
    * **Nested** — ``<name>/SKILL.md`` plus siblings — copied as a
      subtree so scripts / references ride along.

    Each entry in ``report.copied`` lands as either a file or a
    directory on disk; nested skills must keep their SKILL.md.
    """
    monkeypatch.delenv("CORLINMAN_BUNDLED_SKILLS_DIR", raising=False)
    target = tmp_path / "profiles" / "default" / "skills"

    report = starter_skills.seed_starter_skills(target)

    assert report.source is not None
    assert report.target == target
    assert len(report.copied) > 0
    assert len(report.skipped) == 0
    # Every reported copy actually landed on disk — as a file (flat
    # layout) or as a directory containing a SKILL.md (nested layout).
    for name in report.copied:
        path = target / name
        if path.is_file():
            continue
        assert path.is_dir(), f"copied entry {name} is neither file nor dir"
        assert (path / "SKILL.md").is_file(), (
            f"nested skill {name} missing SKILL.md after seed"
        )


def test_seed_starter_skills_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running on a populated target copies nothing and skips all."""
    monkeypatch.delenv("CORLINMAN_BUNDLED_SKILLS_DIR", raising=False)
    target = tmp_path / "skills"

    first = starter_skills.seed_starter_skills(target)
    assert len(first.copied) > 0

    second = starter_skills.seed_starter_skills(target)
    assert second.copied == ()
    assert set(second.skipped) == set(first.copied)


def test_seed_starter_skills_preserves_operator_edits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files already present in the target must not be overwritten.

    Operators sometimes edit a bundled skill body (e.g. to tune the
    `code_review` rubric to their team's house style). The seed
    routine reports the file under ``skipped`` and leaves the bytes
    on disk untouched.
    """
    monkeypatch.delenv("CORLINMAN_BUNDLED_SKILLS_DIR", raising=False)
    target = tmp_path / "skills"
    target.mkdir(parents=True)

    sentinel_body = "---\nname: code_review\ndescription: edited\n---\n# OPERATOR EDIT\n"
    (target / "code_review.md").write_text(sentinel_body, encoding="utf-8")

    report = starter_skills.seed_starter_skills(target)

    assert "code_review.md" in report.skipped
    assert "code_review.md" not in report.copied
    assert (
        (target / "code_review.md").read_text(encoding="utf-8") == sentinel_body
    )


def test_seed_starter_skills_ships_references_for_split_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The four giant skills were split into ``SKILL.md +
    references/*.md``; the seeder's subtree copy must land the
    references in the data dir byte-for-byte, or the slim SKILL.md
    routes the model to files that don't exist."""
    monkeypatch.delenv("CORLINMAN_BUNDLED_SKILLS_DIR", raising=False)
    source = starter_skills.bundled_skills_root()
    assert source is not None
    target = tmp_path / "profiles" / "default" / "skills"

    starter_skills.seed_starter_skills(target)

    for skill in ("huashu-design", "configure-persona", "nuwa-skill", "darwin-skill"):
        src_refs = sorted((source / skill / "references").glob("*.md"))
        assert src_refs, f"{skill}: bundle has no references/*.md"
        for src in src_refs:
            dst = target / skill / "references" / src.name
            assert dst.is_file(), f"{skill}: {src.name} not seeded"
            assert dst.read_text(encoding="utf-8") == src.read_text(
                encoding="utf-8"
            ), f"{skill}: {src.name} content drifted during seed"


def test_split_bundled_skills_stay_slim_with_valid_frontmatter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression net for the giant-skill split: the four main SKILL.md
    files must stay under 150 lines (progressive disclosure — detail
    lives in references/), keep description/when_to_use ≤ 200 chars,
    and keep routing to at least one existing reference file."""
    monkeypatch.delenv("CORLINMAN_BUNDLED_SKILLS_DIR", raising=False)
    from corlinman_skills_registry import SkillRegistry  # noqa: PLC0415

    root = starter_skills.bundled_skills_root()
    assert root is not None
    reg = SkillRegistry.load_from_dir(root)
    dirs_to_names = {
        "huashu-design": "huashu-design",
        "configure-persona": "configure-persona",
        "nuwa-skill": "huashu-nuwa",
        "darwin-skill": "darwin-skill",
    }
    for dirname, name in dirs_to_names.items():
        main = root / dirname / "SKILL.md"
        lines = main.read_text(encoding="utf-8").splitlines()
        assert len(lines) < 150, f"{dirname}/SKILL.md is {len(lines)} lines (>=150)"

        skill = reg.get(name)
        assert skill is not None, f"{name} not registered"
        assert len(skill.description.strip()) <= 200, f"{name}: description > 200 chars"
        assert skill.when_to_use, f"{name}: when_to_use missing"
        assert len(skill.when_to_use.strip()) <= 200, f"{name}: when_to_use > 200 chars"

        body = main.read_text(encoding="utf-8")
        routed = [
            rel
            for rel in {
                seg.split("`")[0]
                for seg in body.split("references/")[1:]
            }
            if (root / dirname / "references" / rel).is_file()
        ]
        assert routed, f"{dirname}: SKILL.md routes to no existing reference"


def test_seed_starter_skills_copies_nested_skill_subtree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nested ``<name>/SKILL.md`` layouts are copied as a whole subtree.

    Covers the W8 ``configure-persona`` skill (which ships as a
    directory because future revisions may grow asset siblings under
    it), and asserts that any sibling files are pulled over too —
    partial copies would leave SKILL.md referring to missing scripts.
    """
    monkeypatch.delenv("CORLINMAN_BUNDLED_SKILLS_DIR", raising=False)
    custom = tmp_path / "private_bundle"
    custom.mkdir()
    skill_dir = custom / "demo-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: x\n---\n# body\n",
        encoding="utf-8",
    )
    (skill_dir / "extra.txt").write_text("ride-along\n", encoding="utf-8")
    # __pycache__ subdir should be ignored.
    (skill_dir / "__pycache__").mkdir()
    (skill_dir / "__pycache__" / "noise.pyc").write_bytes(b"\x00")
    monkeypatch.setenv("CORLINMAN_BUNDLED_SKILLS_DIR", str(custom))

    target = tmp_path / "skills"
    report = starter_skills.seed_starter_skills(target)

    assert "demo-skill" in report.copied
    assert (target / "demo-skill" / "SKILL.md").is_file()
    assert (target / "demo-skill" / "extra.txt").is_file()
    assert not (target / "demo-skill" / "__pycache__").exists()


def test_seed_starter_skills_seeds_configure_persona(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W8 contract: the ``configure-persona`` skill auto-seeds into the
    default profile on first boot — without it, the /persona wizard
    skill is invisible to the agent.
    """
    monkeypatch.delenv("CORLINMAN_BUNDLED_SKILLS_DIR", raising=False)
    target = tmp_path / "skills"

    report = starter_skills.seed_starter_skills(target)

    assert "configure-persona" in report.copied
    assert (target / "configure-persona" / "SKILL.md").is_file()
    body = (target / "configure-persona" / "SKILL.md").read_text("utf-8")
    assert "name: configure-persona" in body
    # Spot-check the SKILL.md mentions the persona.* tool family the
    # wizard drives — guards against a future edit that strips the
    # tool list from the playbook.
    assert "persona_create" in body
    assert "ask_user" in body


def test_seed_starter_skills_seeds_visual_output_quality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh profiles get the always-on visual quality guardrail."""

    monkeypatch.delenv("CORLINMAN_BUNDLED_SKILLS_DIR", raising=False)
    target = tmp_path / "skills"

    report = starter_skills.seed_starter_skills(target)

    assert "visual-output-quality.md" in report.copied
    body = (target / "visual-output-quality.md").read_text("utf-8")
    assert "name: visual-output-quality" in body
    assert "no overlap" in body


def test_seed_starter_skills_no_bundle_source_is_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If neither env nor package resolves, seeding is a quiet no-op.

    We simulate this by monkey-patching the two resolver helpers to
    return ``None``; the real boot path would log "no_bundle_source"
    and let the operator drop SKILL.md files into the profile by
    hand. The point is: boot must not crash on a degraded install.
    """
    monkeypatch.setattr(starter_skills, "_resolve_from_env", lambda: None)
    monkeypatch.setattr(starter_skills, "_resolve_from_package", lambda: None)

    target = tmp_path / "skills"
    report = starter_skills.seed_starter_skills(target)

    assert report.source is None
    assert report.copied == ()
    assert report.skipped == ()
