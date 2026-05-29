"""Tests for :meth:`SkillRegistry.refresh` — on-demand reload of
``*.md`` skill files dropped into the skills root after boot.

These tests cover the per-turn boundary refresh path: an empty dir is
populated, files are edited, files are deleted, and a no-op refresh
returns an empty delta. The mtime tracking + duplicate-handling edge
cases are also exercised so regressions in the diff logic surface
locally rather than as confusing prompt-assembly mismatches in prod.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from corlinman_agent.skills import RefreshDelta, SkillRegistry

_SKILL_MD_TEMPLATE = (
    "---\n"
    "name: {name}\n"
    "description: {description}\n"
    "---\n"
    "{body}\n"
)


def _write_skill(
    skills_dir: Path,
    filename: str,
    *,
    name: str,
    description: str = "stub description",
    body: str = "stub body",
) -> Path:
    """Write a minimal SKILL.md to ``skills_dir/filename`` and return
    the path. Caller is responsible for nudging mtime if a subsequent
    edit needs to look "modified" on a coarse-grained fs clock."""
    path = skills_dir / filename
    path.write_text(
        _SKILL_MD_TEMPLATE.format(name=name, description=description, body=body),
        encoding="utf-8",
    )
    return path


def _bump_mtime(path: Path) -> None:
    """Force a strictly-later mtime on ``path``.

    HFS+ and ext4 with default mount options keep mtime at 1s resolution,
    so a rewrite-in-place inside the same second can leave mtime equal to
    the previous value. We deliberately stamp the future to make the
    "file was modified" branch exercise itself on every CI runner.
    """
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 2.0))


# --------------------------------------------------------------------------- #
# Happy paths                                                                  #
# --------------------------------------------------------------------------- #


def test_refresh_picks_up_new_md(tmp_path: Path) -> None:
    """Empty dir -> write a SKILL.md -> refresh() reports it as added.

    This is the dropped-file flow: an operator copies a new skill into
    ``~/.corlinman/skills/`` and expects the next chat turn to see it
    without a process restart.
    """
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    registry = SkillRegistry.load_from_dir(skills_dir)
    assert registry.names() == []

    _write_skill(skills_dir, "hello.md", name="hello", body="Say hi.")

    delta = registry.refresh()
    assert isinstance(delta, RefreshDelta)
    assert delta.added == ["hello"]
    assert delta.updated == []
    assert delta.removed == []

    skill = registry.get("hello")
    assert skill is not None
    assert "Say hi." in skill.body_markdown


def test_refresh_detects_updated_mtime(tmp_path: Path) -> None:
    """Edit a tracked file and bump mtime; refresh() reports it as
    updated and the body actually changes in the registry."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    path = _write_skill(skills_dir, "hello.md", name="hello", body="v1 body")

    registry = SkillRegistry.load_from_dir(skills_dir)
    assert registry.get("hello") is not None
    assert "v1 body" in registry.get("hello").body_markdown  # type: ignore[union-attr]

    # Rewrite + force a strictly-later mtime so the registry's cached
    # value is definitely stale.
    path.write_text(
        _SKILL_MD_TEMPLATE.format(name="hello", description="d", body="v2 body"),
        encoding="utf-8",
    )
    _bump_mtime(path)

    delta = registry.refresh()
    assert delta.added == []
    assert delta.updated == ["hello"]
    assert delta.removed == []

    refreshed = registry.get("hello")
    assert refreshed is not None
    assert "v2 body" in refreshed.body_markdown
    assert "v1 body" not in refreshed.body_markdown


def test_refresh_drops_deleted_file(tmp_path: Path) -> None:
    """Unlinking a previously-loaded SKILL.md drops it from the
    registry on the next refresh."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    path = _write_skill(skills_dir, "hello.md", name="hello")

    registry = SkillRegistry.load_from_dir(skills_dir)
    assert "hello" in registry

    path.unlink()

    delta = registry.refresh()
    assert delta.added == []
    assert delta.updated == []
    assert delta.removed == ["hello"]
    assert registry.get("hello") is None
    assert "hello" not in registry


def test_refresh_is_idempotent_when_no_change(tmp_path: Path) -> None:
    """Two refreshes in a row with no fs changes return empty deltas
    on the second call.

    This is the hot-path case — chat turns where the operator hasn't
    touched the skill dir. The delta must be falsy so the structlog
    line is not emitted (we don't want a noisy log on every turn).
    """
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir, "hello.md", name="hello")
    _write_skill(skills_dir, "world.md", name="world")

    registry = SkillRegistry.load_from_dir(skills_dir)
    assert sorted(registry.names()) == ["hello", "world"]

    first = registry.refresh()
    assert first.added == [] and first.updated == [] and first.removed == []
    assert not first  # __bool__ must report falsy on empty delta

    second = registry.refresh()
    assert second.added == [] and second.updated == [] and second.removed == []
    assert not second


# --------------------------------------------------------------------------- #
# Edge cases                                                                   #
# --------------------------------------------------------------------------- #


def test_refresh_handles_root_disappearing(tmp_path: Path) -> None:
    """If the whole skills dir is deleted between turns, every tracked
    skill is reported as removed and the registry empties without
    raising. This lets operators wipe ``~/.corlinman/skills/`` at
    runtime to reset to no-skills mode."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir, "a.md", name="alpha")
    _write_skill(skills_dir, "b.md", name="beta")

    registry = SkillRegistry.load_from_dir(skills_dir)
    assert sorted(registry.names()) == ["alpha", "beta"]

    # Nuke the whole tree.
    for child in skills_dir.iterdir():
        child.unlink()
    skills_dir.rmdir()

    delta = registry.refresh()
    assert sorted(delta.removed) == ["alpha", "beta"]
    assert delta.added == []
    assert delta.updated == []
    assert len(registry) == 0


def test_refresh_recovers_after_directory_recreated(tmp_path: Path) -> None:
    """After the skills dir is removed and re-created with a fresh
    SKILL.md, refresh() picks the new file up as 'added'. This is the
    common 'reset and redeploy' lifecycle."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir, "a.md", name="alpha")

    registry = SkillRegistry.load_from_dir(skills_dir)
    assert "alpha" in registry

    (skills_dir / "a.md").unlink()
    skills_dir.rmdir()
    # ``force=True`` so the debounce gate doesn't suppress the second
    # disk scan inside this test (two refreshes in <30s).
    assert registry.refresh(force=True).removed == ["alpha"]

    skills_dir.mkdir()
    _write_skill(skills_dir, "z.md", name="zeta")

    delta = registry.refresh(force=True)
    assert delta.added == ["zeta"]
    assert delta.updated == []
    assert delta.removed == []
    assert "zeta" in registry


def test_refresh_skips_broken_skill_without_dropping_others(tmp_path: Path) -> None:
    """A malformed new SKILL.md should NOT raise — it must be logged and
    skipped. Other valid files in the same refresh must still apply.

    Guarantees that an operator's mid-edit save (truncated frontmatter,
    typo in ``name:``) cannot brick the chat path.
    """
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir, "good.md", name="good")

    registry = SkillRegistry.load_from_dir(skills_dir)
    assert registry.names() == ["good"]

    # New broken file (no frontmatter fence) + new valid file together.
    (skills_dir / "broken.md").write_text("just some text, no yaml fence\n", encoding="utf-8")
    _write_skill(skills_dir, "fresh.md", name="fresh")

    delta = registry.refresh()
    # The broken file is silently skipped; the valid file is added.
    assert delta.added == ["fresh"]
    assert delta.updated == []
    assert delta.removed == []
    assert "good" in registry
    assert "fresh" in registry


def test_refresh_handles_rename_of_name_field(tmp_path: Path) -> None:
    """If the same file changes its ``name:`` between refreshes, the
    old name is removed and the new name is added. This matches the
    operator-facing model: a skill's identity is its name, not its
    file path.
    """
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    path = _write_skill(skills_dir, "a.md", name="old_name")

    registry = SkillRegistry.load_from_dir(skills_dir)
    assert "old_name" in registry

    path.write_text(
        _SKILL_MD_TEMPLATE.format(name="new_name", description="d", body="x"),
        encoding="utf-8",
    )
    _bump_mtime(path)

    delta = registry.refresh()
    assert delta.added == ["new_name"]
    assert delta.removed == ["old_name"]
    assert delta.updated == []
    assert "new_name" in registry
    assert "old_name" not in registry


def test_refresh_no_op_on_in_memory_registry() -> None:
    """A registry built from an in-memory dict has no ``_root`` and
    refresh() must be a no-op — never touch disk for test fixtures
    that hand in pre-baked skills."""
    registry = SkillRegistry()
    delta = registry.refresh()
    assert delta.added == [] and delta.updated == [] and delta.removed == []
    assert registry.last_refreshed_at_ms is None


# --------------------------------------------------------------------------- #
# Diagnostics surface                                                          #
# --------------------------------------------------------------------------- #


def test_status_summary_reports_last_refreshed_at(tmp_path: Path) -> None:
    """status_summary() exposes the timestamp + skill count so an admin
    page (or a log line) can render registry health without poking at
    private attributes."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir, "a.md", name="alpha")

    before = int(time.time() * 1000)
    registry = SkillRegistry.load_from_dir(skills_dir)
    after = int(time.time() * 1000)

    summary = registry.status_summary()
    assert summary["skill_count"] == 1
    assert summary["names"] == ["alpha"]
    assert summary["root"] == str(skills_dir)
    ts = summary["last_refreshed_at_ms"]
    assert isinstance(ts, int)
    assert before <= ts <= after

    # After a refresh that adds a skill, the timestamp must advance and
    # the count must follow.
    time.sleep(0.005)  # cheap nudge so the ms timestamp can change
    _write_skill(skills_dir, "b.md", name="beta")
    registry.refresh()
    summary2 = registry.status_summary()
    assert summary2["skill_count"] == 2
    assert summary2["names"] == ["alpha", "beta"]
    assert summary2["last_refreshed_at_ms"] >= ts


# --------------------------------------------------------------------------- #
# Perf — debounce + force kwarg                                                #
# --------------------------------------------------------------------------- #


def test_refresh_debounces_within_interval(tmp_path: Path) -> None:
    """Two consecutive refresh() calls within the min interval: the
    second one short-circuits to an empty delta and does not touch the
    skills dir.

    Hot path guarantee — the chat handler invokes refresh() at every
    turn boundary; without debounce a hot session pays an rglob + stat
    per turn for a directory that almost never changes between
    adjacent turns.
    """
    from corlinman_agent.skills.registry import SkillRegistry

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir, "hello.md", name="hello")

    # Build with a long interval so the second refresh hits the debounce.
    registry = SkillRegistry.load_from_dir(skills_dir)
    # Override min_interval_ms on the loaded registry. load_from_dir
    # builds via __init__() which reads env; reach in directly to pin a
    # known value for this assertion.
    registry._min_interval_ms = 30_000  # type: ignore[attr-defined]

    # First refresh — no changes since load → empty delta, but it WILL
    # scan and stamp the monotonic clock.
    first = registry.refresh()
    assert not first

    # Drop a brand-new SKILL.md between refreshes. If debounce works,
    # the very next refresh() must NOT see it — gating it on the
    # 30s interval defers the disk scan.
    _write_skill(skills_dir, "fresh.md", name="fresh")

    second = registry.refresh()
    assert not second, (
        "debounce violation: refresh() within min_interval_ms must "
        f"return an empty delta, got {second!r}"
    )
    # The new file is intentionally invisible until the interval elapses
    # or a force=True call lands.
    assert "fresh" not in registry


def test_refresh_force_bypasses_debounce(tmp_path: Path) -> None:
    """``force=True`` walks the skills dir even when the debounce
    window says "skip" — used by the future "force refresh" admin
    button and by tests that need deterministic disk reads.
    """
    from corlinman_agent.skills.registry import SkillRegistry

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir, "hello.md", name="hello")

    registry = SkillRegistry.load_from_dir(skills_dir)
    registry._min_interval_ms = 30_000  # type: ignore[attr-defined]

    # Prime the debounce clock with a real scan.
    registry.refresh()

    # New file dropped after the prime. A plain refresh() would skip;
    # force=True must scan and pick it up.
    _write_skill(skills_dir, "forced.md", name="forced")

    delta = registry.refresh(force=True)
    assert delta.added == ["forced"]
    assert "forced" in registry


def test_refresh_runs_after_interval_elapsed(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """When the debounce interval elapses, refresh() resumes its normal
    disk scan. Simulated by advancing ``time.monotonic_ns`` via a
    monkeypatched stub so the test doesn't actually sleep 30s.
    """
    from corlinman_agent.skills import registry as registry_mod
    from corlinman_agent.skills.registry import SkillRegistry

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir, "hello.md", name="hello")

    reg = SkillRegistry.load_from_dir(skills_dir)
    reg._min_interval_ms = 30_000  # type: ignore[attr-defined]

    # Prime the monotonic clock with the first scan.
    reg.refresh()

    # Drop a new skill that the next refresh would normally skip.
    _write_skill(skills_dir, "later.md", name="later")

    # Fake the clock: jump 31 seconds forward. ``time.monotonic_ns``
    # returns nanoseconds; the registry converts to ms internally.
    last_ms = reg._last_refresh_monotonic_ms  # type: ignore[attr-defined]
    fake_now_ns = int((last_ms + 31_000) * 1_000_000)
    monkeypatch.setattr(
        registry_mod.time, "monotonic_ns", lambda: fake_now_ns
    )

    delta = reg.refresh()
    assert delta.added == ["later"], (
        f"expected 'later' to be added after the interval elapsed, "
        f"got delta={delta!r}"
    )
    assert "later" in reg


def test_refresh_zero_interval_disables_debounce(tmp_path: Path) -> None:
    """``min_interval_ms = 0`` opts out of debouncing entirely —
    operators who want the legacy "scan every turn" behaviour can set
    ``CORLINMAN_SKILL_REFRESH_INTERVAL_MS=0``.
    """
    from corlinman_agent.skills.registry import SkillRegistry

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    _write_skill(skills_dir, "hello.md", name="hello")

    reg = SkillRegistry.load_from_dir(skills_dir)
    reg._min_interval_ms = 0  # type: ignore[attr-defined]

    reg.refresh()  # prime
    _write_skill(skills_dir, "right_now.md", name="right_now")

    # With debounce off, the immediate follow-up refresh must scan.
    delta = reg.refresh()
    assert delta.added == ["right_now"]


def test_refresh_interval_env_override(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """``CORLINMAN_SKILL_REFRESH_INTERVAL_MS`` controls the default
    interval used by :meth:`SkillRegistry.load_from_dir`-built
    registries (the production code path).
    """
    from corlinman_agent.skills.registry import SkillRegistry

    monkeypatch.setenv("CORLINMAN_SKILL_REFRESH_INTERVAL_MS", "12345")
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    reg = SkillRegistry.load_from_dir(skills_dir)
    assert reg._min_interval_ms == 12345  # type: ignore[attr-defined]
