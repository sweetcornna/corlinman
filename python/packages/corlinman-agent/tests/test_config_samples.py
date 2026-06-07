"""B2-BE5: sanity-check the repository's sample ``skills/``, ``agents/``,
and ``TVStxt/`` trees.

These tests load the real on-disk samples (not tmp fixtures) so that an
inadvertent rename or broken frontmatter is caught before a release.

The Python-side skill loader does not exist yet (B2-BE4 ships the Rust
version first); the test that exercises it is skipped with a TODO.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from corlinman_agent.agents import AgentCardRegistry
from corlinman_agent.variables import VariableCascade


def _repo_root() -> Path:
    """Walk up from this test file to the repo root.

    Layout: repo/python/packages/corlinman-agent/tests/<this file>.
    """
    return Path(__file__).resolve().parents[4]


# --------------------------------------------------------------------- #
# Fixture surface                                                        #
# --------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def repo_root() -> Path:
    root = _repo_root()
    # A sanity check that we resolved the right directory; if this
    # assertion fires, `parents[4]` is wrong for the new layout and the
    # test file itself is the bug, not the samples.
    assert (root / "docs" / "config.example.toml").is_file(), (
        f"repo_root() resolved to {root}, which does not contain docs/config.example.toml"
    )
    return root


# --------------------------------------------------------------------- #
# 1. Directory shape                                                     #
# --------------------------------------------------------------------- #


def test_skills_dir_has_minimum_samples(repo_root: Path) -> None:
    skills = repo_root / "skills"
    md_files = sorted(p.name for p in skills.glob("*.md"))
    # B2-BE5 ships three reference skills; more is fine, fewer is not.
    assert len(md_files) >= 3, f"expected >=3 skill files, got {md_files}"
    for required in ("web_search.md", "code_review.md", "memory.md"):
        assert required in md_files, f"missing sample skill: {required}"


def test_tvstxt_has_minimum_samples(repo_root: Path) -> None:
    tar = repo_root / "TVStxt" / "tar"
    sar = repo_root / "TVStxt" / "sar"
    fixed = repo_root / "TVStxt" / "fixed"

    tar_files = sorted(p.name for p in tar.glob("*.txt"))
    sar_files = sorted(p.name for p in sar.glob("*.txt"))

    assert len(tar_files) >= 1, f"expected >=1 tar file, got {tar_files}"
    assert "CurrentProject.txt" in tar_files
    assert "SarPrompt1.txt" in sar_files
    # fixed/ ships a README today; the on-disk tier itself is not yet
    # wired, so we only assert the directory exists.
    assert fixed.is_dir()


def test_fish_audio_provider_doc_snippet_is_valid_toml(repo_root: Path) -> None:
    text = (repo_root / "docs" / "providers.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```toml\n(.*?)\n```", text, re.DOTALL)
    matches = [block for block in blocks if 'base_url = "https://api.fish.audio"' in block]
    assert len(matches) == 1

    parsed = tomllib.loads(matches[0])

    fish = parsed["providers"]["fish_audio"]
    assert fish["params"]["tts_backend"] == "fish"
    assert fish["params"]["reference_id"] == "your-fish-reference-id"


# --------------------------------------------------------------------- #
# 2. Agent cards                                                         #
# --------------------------------------------------------------------- #


def test_agent_cards_load(repo_root: Path) -> None:
    reg = AgentCardRegistry.load_from_dir(repo_root / "agents")
    names = reg.names()
    assert len(names) >= 3, f"expected >=3 agent cards, got {names}"
    for required in ("mentor", "researcher", "editor"):
        assert required in names, f"missing sample agent card: {required}"


def test_mentor_card_shape(repo_root: Path) -> None:
    reg = AgentCardRegistry.load_from_dir(repo_root / "agents")
    mentor = reg.get("mentor")
    assert mentor is not None

    # Declared persona surface.
    assert "senior software engineer" in mentor.system_prompt.lower()
    # Placeholder references — these must survive into the card body
    # verbatim so the placeholder engine can expand them at turn time.
    assert "{{TimeVar}}" in mentor.system_prompt
    assert "{{TarCurrentProject}}" in mentor.system_prompt

    # Tool allowlist is exactly what the sample declares.
    assert "web.search" in mentor.tools_allowed
    assert "file.read" in mentor.tools_allowed

    # Skill linkage: mentor references code_review, which exists on disk.
    assert "code_review" in mentor.skill_refs
    assert (repo_root / "skills" / "code_review.md").is_file()


# --------------------------------------------------------------------- #
# 3. Variable cascade — Tar tier via real file                           #
# --------------------------------------------------------------------- #


def test_cascade_reads_tar_current_project(repo_root: Path) -> None:
    cascade = VariableCascade(
        tar_dir=repo_root / "TVStxt" / "tar",
        var_dir=repo_root / "TVStxt" / "var",
        sar_dir=repo_root / "TVStxt" / "sar",
        fixed_dir=repo_root / "TVStxt" / "fixed",
        hot_reload=False,
    )
    value = cascade.resolve("TarCurrentProject", model_name="any")
    assert value is not None
    assert "corlinman" in value
    # The DirLoader strips exactly one trailing newline, so the stored
    # value should not end with `\n`.
    assert not value.endswith("\n")


# --------------------------------------------------------------------- #
# 4. Python-side SkillRegistry (TODO)                                    #
# --------------------------------------------------------------------- #


@pytest.mark.skip(
    reason=(
        "TODO(B2-BE4 Python mirror): no Python SkillRegistry exists yet. "
        "Remove this skip once corlinman_agent.skills.registry is in place."
    )
)
def test_web_search_skill_loads_via_python_registry(repo_root: Path) -> None:  # pragma: no cover
    # Target shape once the Python mirror lands:
    #
    #   from corlinman_agent.skills import SkillRegistry
    #   reg = SkillRegistry.load_from_dir(repo_root / "skills")
    #   skill = reg.get("web_search")
    #   assert skill is not None
    #   assert "web.search" in skill.allowed_tools
    #   assert "providers.brave.api_key" in skill.requires.config
    #   assert "Brave Search" in skill.body_markdown
    raise AssertionError("placeholder — see skip reason")
