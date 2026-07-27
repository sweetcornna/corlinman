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


# ---------------------------------------------------------------------------
# factory-pristine refresh path (post-#184 upgrade story)
# ---------------------------------------------------------------------------


def _make_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the seeder at a private bundle with one nested + one flat skill."""
    bundle = tmp_path / "bundle"
    nested = bundle / "demo-skill"
    (nested / "references").mkdir(parents=True)
    (nested / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: v2\n---\n# v2 body\n",
        encoding="utf-8",
    )
    (nested / "references" / "deep.md").write_text("deep detail v2\n", encoding="utf-8")
    (bundle / "flat-skill.md").write_text(
        "---\nname: flat-skill\ndescription: v2\n---\n# v2 flat\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CORLINMAN_BUNDLED_SKILLS_DIR", str(bundle))
    return bundle


def _register_factory_hash(
    monkeypatch: pytest.MonkeyPatch, name: str, body: str
) -> None:
    """Record ``body`` as a historical factory revision of skill ``name``."""
    import hashlib  # noqa: PLC0415

    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    table = dict(starter_skills._FACTORY_SKILL_MD_SHA256)
    table[name] = table.get(name, frozenset()) | {digest}
    monkeypatch.setattr(starter_skills, "_FACTORY_SKILL_MD_SHA256", table)


def test_seed_refreshes_pristine_legacy_nested_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nested skill whose SKILL.md matches a recorded factory revision
    is refreshed whole — new SKILL.md lands AND the references/ subtree
    appears, which is exactly the #184 upgrade gap."""
    _make_bundle(tmp_path, monkeypatch)
    old_body = "---\nname: demo-skill\ndescription: v1\n---\n# v1 body\n"
    _register_factory_hash(monkeypatch, "demo-skill", old_body)

    target = tmp_path / "skills"
    (target / "demo-skill").mkdir(parents=True)
    (target / "demo-skill" / "SKILL.md").write_text(old_body, encoding="utf-8")

    report = starter_skills.seed_starter_skills(target)

    assert "demo-skill" in report.refreshed
    assert "demo-skill" not in report.skipped
    assert "v2 body" in (target / "demo-skill" / "SKILL.md").read_text("utf-8")
    assert (
        target / "demo-skill" / "references" / "deep.md"
    ).read_text("utf-8") == "deep detail v2\n"


def test_seed_refresh_preserves_operator_edits_in_nested_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator-edited SKILL.md never matches a factory hash, so the
    whole directory is left alone — no new references, no overwrite."""
    _make_bundle(tmp_path, monkeypatch)
    edited = "---\nname: demo-skill\ndescription: v1\n---\n# OPERATOR EDIT\n"

    target = tmp_path / "skills"
    (target / "demo-skill").mkdir(parents=True)
    (target / "demo-skill" / "SKILL.md").write_text(edited, encoding="utf-8")

    report = starter_skills.seed_starter_skills(target)

    assert "demo-skill" in report.skipped
    assert report.refreshed == ()
    assert (target / "demo-skill" / "SKILL.md").read_text("utf-8") == edited
    assert not (target / "demo-skill" / "references").exists()


def test_seed_refresh_is_idempotent_and_preserves_usage_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a refresh the target matches the bundle, so the next boot
    skips it; runtime ``.usage.json`` bookkeeping survives the rmtree."""
    _make_bundle(tmp_path, monkeypatch)
    old_body = "---\nname: demo-skill\ndescription: v1\n---\n# v1 body\n"
    _register_factory_hash(monkeypatch, "demo-skill", old_body)

    target = tmp_path / "skills"
    (target / "demo-skill").mkdir(parents=True)
    (target / "demo-skill" / "SKILL.md").write_text(old_body, encoding="utf-8")
    usage = '{"uses": 7}'
    (target / "demo-skill" / ".usage.json").write_text(usage, encoding="utf-8")

    first = starter_skills.seed_starter_skills(target)
    assert "demo-skill" in first.refreshed
    assert (target / "demo-skill" / ".usage.json").read_text("utf-8") == usage

    second = starter_skills.seed_starter_skills(target)
    assert second.refreshed == ()
    assert "demo-skill" in second.skipped


def test_seed_refreshes_pristine_flat_skill_and_keeps_edited_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flat-layout skills follow the same contract: recorded factory
    revisions are upgraded in place, operator edits stay put."""
    _make_bundle(tmp_path, monkeypatch)
    old_body = "---\nname: flat-skill\ndescription: v1\n---\n# v1 flat\n"
    _register_factory_hash(monkeypatch, "flat-skill", old_body)

    target = tmp_path / "skills"
    target.mkdir()
    (target / "flat-skill.md").write_text(old_body, encoding="utf-8")

    report = starter_skills.seed_starter_skills(target)
    assert "flat-skill.md" in report.refreshed
    assert "v2 flat" in (target / "flat-skill.md").read_text("utf-8")

    # Now the operator edits it — the next seed must not clobber.
    edited = "# my own notes\n"
    (target / "flat-skill.md").write_text(edited, encoding="utf-8")
    report2 = starter_skills.seed_starter_skills(target)
    assert "flat-skill.md" in report2.skipped
    assert (target / "flat-skill.md").read_text("utf-8") == edited


def test_seed_migrates_pristine_legacy_flat_to_nested_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A skill that used to seed flat but now ships nested: the pristine
    flat body is removed so the nested subtree can land without a
    duplicate registry entry."""
    _make_bundle(tmp_path, monkeypatch)
    old_body = "---\nname: demo-skill\ndescription: v1\n---\n# v1 body\n"
    _register_factory_hash(monkeypatch, "demo-skill", old_body)

    target = tmp_path / "skills"
    target.mkdir()
    (target / "demo-skill.md").write_text(old_body, encoding="utf-8")

    report = starter_skills.seed_starter_skills(target)

    assert not (target / "demo-skill.md").exists()
    assert "demo-skill" in report.copied
    assert (target / "demo-skill" / "SKILL.md").is_file()


def test_factory_hash_table_covers_the_four_split_skills() -> None:
    """The hardcoded history table must keep an entry per split skill,
    every hash a 64-char hex sha256 — a typo here silently disables the
    upgrade path for existing deployments."""
    table = starter_skills._FACTORY_SKILL_MD_SHA256
    for name in ("configure-persona", "darwin-skill", "huashu-design", "nuwa-skill"):
        assert name in table, f"missing factory-hash entry for {name}"
        assert table[name], f"empty factory-hash set for {name}"
        for digest in table[name]:
            assert len(digest) == 64 and set(digest) <= set(
                "0123456789abcdef"
            ), f"{name}: malformed sha256 {digest!r}"


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
