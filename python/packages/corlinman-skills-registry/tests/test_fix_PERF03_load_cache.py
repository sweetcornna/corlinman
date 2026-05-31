"""PERF-03 repro: ``SkillRegistry.load_from_dir`` re-reads + re-parses
every SKILL.md on every call, with no mtime-gated cache.

The curator ``/admin/curator/profiles`` endpoint is UI-polled, and each
poll rebuilds a fresh ``SkillRegistry`` per profile — so an unchanged tree
should cost a stat-only scan, not a full ``read_text`` + ``yaml.safe_load``
of every file.

Acceptance: a second poll of an unchanged tree does NO file reads beyond
the stat walk.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import corlinman_skills_registry.registry as registry_mod
from corlinman_skills_registry import SkillRegistry

MakeDir = Callable[[list[tuple[str, str]]], Path]


def _skill_md(name: str) -> str:
    return f"---\nname: {name}\ndescription: a {name} skill\n---\nbody for {name}\n"


def test_unchanged_tree_poll_does_no_reads(
    make_dir: MakeDir, monkeypatch
) -> None:
    root = make_dir(
        [
            ("a.md", _skill_md("alpha")),
            ("b.md", _skill_md("bravo")),
            ("c.md", _skill_md("charlie")),
        ]
    )

    # Warm: first load populates whatever cache exists.
    reg1 = SkillRegistry.load_from_dir(root)
    assert reg1.names() == ["alpha", "bravo", "charlie"]

    # Now count read_text calls on the SECOND (unchanged-tree) poll.
    real_read_text = Path.read_text
    reads: list[str] = []

    def _counting_read_text(self: Path, *args, **kwargs):  # noqa: ANN002, ANN003
        reads.append(str(self))
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _counting_read_text)

    reg2 = SkillRegistry.load_from_dir(root)
    assert reg2.names() == ["alpha", "bravo", "charlie"]

    # The unchanged poll must not re-read any SKILL.md (stat-only).
    assert reads == [], f"unchanged-tree poll re-read files: {reads}"


def test_changed_tree_poll_reloads(make_dir: MakeDir) -> None:
    """A real content edit must still be picked up — the cache is
    mtime/size-gated, not permanent."""
    root = make_dir([("a.md", _skill_md("alpha"))])

    reg1 = SkillRegistry.load_from_dir(root)
    assert reg1.get("alpha") is not None
    assert reg1.get("alpha").description == "a alpha skill"

    # Rewrite with a different name + bump mtime forward so the stat
    # fingerprint changes regardless of filesystem mtime granularity.
    import os
    import time

    target = root / "a.md"
    target.write_text(
        "---\nname: alpha2\ndescription: edited skill\n---\nnew body\n",
        encoding="utf-8",
    )
    future = time.time() + 10
    os.utime(target, (future, future))

    reg2 = SkillRegistry.load_from_dir(root)
    assert reg2.get("alpha2") is not None
    assert reg2.get("alpha2").description == "edited skill"
    assert reg2.get("alpha") is None


def test_module_has_no_stale_global_state_between_roots(
    make_dir: MakeDir,
) -> None:
    """Two distinct roots must not collide in the cache."""
    root_a = make_dir([("a.md", _skill_md("alpha"))])
    # second tmp dir
    root_b = root_a.parent / "other"
    root_b.mkdir()
    (root_b / "b.md").write_text(_skill_md("bravo"), encoding="utf-8")

    reg_a = SkillRegistry.load_from_dir(root_a)
    reg_b = SkillRegistry.load_from_dir(root_b)
    assert reg_a.names() == ["alpha"]
    assert reg_b.names() == ["bravo"]
    # sanity: module exposes the cache so tests above are meaningful
    assert hasattr(registry_mod, "SkillRegistry")
