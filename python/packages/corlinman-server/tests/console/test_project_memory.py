"""CORLINMAN.md project-memory discovery, includes, and assembly."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from corlinman_server.console.commands import dispatch
from corlinman_server.console.project_memory import (
    discover_memory_files,
    expand_includes,
    load_project_memory,
)


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    """A directory that bounds the upward walk (has a .git dir)."""
    repo = tmp_path / name
    (repo / ".git").mkdir(parents=True)
    return repo


# ── discovery ─────────────────────────────────────────────────────────


def test_discovery_order_global_then_root_down_to_cwd(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "CORLINMAN.md").write_text("global", encoding="utf-8")

    repo = _repo(tmp_path)
    (repo / "CORLINMAN.md").write_text("root", encoding="utf-8")
    nested = repo / "pkg" / "api"
    nested.mkdir(parents=True)
    (repo / "pkg" / "CORLINMAN.md").write_text("mid", encoding="utf-8")
    (nested / "CORLINMAN.md").write_text("leaf", encoding="utf-8")
    (nested / "CORLINMAN.local.md").write_text("leaf-local", encoding="utf-8")

    files = discover_memory_files(nested, data_dir)
    assert files == [
        data_dir / "CORLINMAN.md",
        repo / "CORLINMAN.md",
        repo / "pkg" / "CORLINMAN.md",
        nested / "CORLINMAN.md",
        nested / "CORLINMAN.local.md",
    ]


def test_local_md_sorts_after_plain_md_in_same_dir(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "CORLINMAN.local.md").write_text("local", encoding="utf-8")
    (repo / "CORLINMAN.md").write_text("plain", encoding="utf-8")
    files = discover_memory_files(repo, tmp_path / "no-data")
    assert files == [repo / "CORLINMAN.md", repo / "CORLINMAN.local.md"]


def test_walk_stops_at_git_root(tmp_path: Path) -> None:
    (tmp_path / "CORLINMAN.md").write_text("outside the repo", encoding="utf-8")
    repo = _repo(tmp_path)
    (repo / "CORLINMAN.md").write_text("inside", encoding="utf-8")
    sub = repo / "src"
    sub.mkdir()

    files = discover_memory_files(sub, tmp_path / "no-data")
    assert files == [repo / "CORLINMAN.md"]
    assert tmp_path / "CORLINMAN.md" not in files


def test_empty_everything_loads_nothing(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    text, files = load_project_memory(repo, tmp_path / "no-data")
    assert text is None
    assert files == []


# ── @include expansion ────────────────────────────────────────────────


def test_include_expands_relative_file_in_place(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "extra.md").write_text("included body", encoding="utf-8")
    (repo / "CORLINMAN.md").write_text("before\n@./extra.md\nafter\n", encoding="utf-8")

    text, files = load_project_memory(repo, tmp_path / "no-data")
    assert text is not None
    assert "included body" in text
    assert "@./extra.md" not in text
    assert text.index("before") < text.index("included body") < text.index("after")
    assert files == [repo / "CORLINMAN.md"]  # includes don't join the file list


def test_missing_include_becomes_marker(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "CORLINMAN.md").write_text("@./nope.md\n", encoding="utf-8")
    text, _ = load_project_memory(repo, tmp_path / "no-data")
    assert text is not None
    assert "<!-- missing include: ./nope.md -->" in text


def test_include_cycle_is_broken_not_infinite(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "CORLINMAN.md").write_text("top\n@./a.md\n", encoding="utf-8")
    (repo / "a.md").write_text("in-a\n@./b.md\n", encoding="utf-8")
    (repo / "b.md").write_text("in-b\n@./a.md\n", encoding="utf-8")

    text, _ = load_project_memory(repo, tmp_path / "no-data")
    assert text is not None
    assert text.count("in-a") == 1
    assert text.count("in-b") == 1
    assert "cyclic include" in text


def test_include_depth_capped_at_five(tmp_path: Path) -> None:
    for i in range(7):
        (tmp_path / f"f{i}.md").write_text(f"level-{i}\n@./f{i + 1}.md\n", encoding="utf-8")
    out = expand_includes("@./f0.md", base_dir=tmp_path)
    assert "level-4" in out
    assert "level-5" not in out
    assert "include depth limit reached" in out


def test_include_tilde_expands_to_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "shared.md").write_text("from home", encoding="utf-8")
    out = expand_includes("@~/shared.md", base_dir=tmp_path / "elsewhere")
    assert out == "from home"


# ── assembly ──────────────────────────────────────────────────────────


def test_block_has_header_and_from_separators(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "CORLINMAN.md").write_text("root rules", encoding="utf-8")
    sub = repo / "sub"
    sub.mkdir()
    (sub / "CORLINMAN.md").write_text("sub rules", encoding="utf-8")

    text, files = load_project_memory(sub, tmp_path / "no-data")
    assert text is not None
    assert text.startswith("Project memory (CORLINMAN.md)")
    assert f"# from: {repo / 'CORLINMAN.md'}" in text
    assert f"# from: {sub / 'CORLINMAN.md'}" in text
    assert text.index("root rules") < text.index("sub rules")
    assert files == [repo / "CORLINMAN.md", sub / "CORLINMAN.md"]


def test_total_capped_at_64kb_with_marker(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "CORLINMAN.md").write_text("x" * 100_000, encoding="utf-8")
    text, _ = load_project_memory(repo, tmp_path / "no-data")
    assert text is not None
    assert text.endswith("<!-- truncated: project memory exceeds 64KB -->")
    assert len(text.encode("utf-8")) <= 64 * 1024 + 100


# ── /memory command ───────────────────────────────────────────────────


class _MemoryStubApp:
    def __init__(self, files: list[Path]) -> None:
        self.project_memory_files = files


async def test_memory_command_lists_files_and_sizes(tmp_path: Path) -> None:
    path = tmp_path / "CORLINMAN.md"
    path.write_text("12345", encoding="utf-8")
    app: Any = _MemoryStubApp([path])
    text = await dispatch(app, "/memory") or ""
    assert str(path) in text
    assert "5 bytes" in text


async def test_memory_command_empty_hint() -> None:
    app: Any = _MemoryStubApp([])
    text = await dispatch(app, "/memory") or ""
    assert "no project memory loaded" in text
    assert "CORLINMAN.md" in text
