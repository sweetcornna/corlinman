"""Gap-fill lane-search: ripgrep-parity tests for ``search_files``.

Covers the new content-mode capabilities (output_mode, case-insensitive,
context lines, verbatim/indent-preserving lines, glob/type pre-filter) and
the name-mode newest-first ordering + truncation flag. Uniquely named so it
never collides with sibling gap-fill lanes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from corlinman_agent.coding.search import (
    dispatch_search_files,
    search_files_tool_schema,
)


def _args(**kw: object) -> bytes:
    return json.dumps(kw).encode("utf-8")


def _run(tmp: Path, **kw: object) -> dict:
    return json.loads(dispatch_search_files(args_json=_args(**kw), workspace=tmp))


# ---------------------------------------------------------------------------
# schema surface
# ---------------------------------------------------------------------------


def test_gf_search_schema_advertises_new_params() -> None:
    props = search_files_tool_schema()["function"]["parameters"]["properties"]
    for key in (
        "output_mode",
        "case_insensitive",
        "before",
        "after",
        "context",
        "glob",
        "type",
    ):
        assert key in props, key
    assert props["output_mode"]["enum"] == [
        "content",
        "files_with_matches",
        "count",
    ]


# ---------------------------------------------------------------------------
# backward compatibility — default call still returns matched lines
# ---------------------------------------------------------------------------


def test_gf_search_default_content_unchanged(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def foo():\n    return 1\n")
    res = _run(tmp_path, pattern=r"def \w+")
    assert res["mode"] == "content"
    matched = {(m["path"], m["line"]) for m in res["matches"]}
    assert ("a.py", 1) in matched
    # No context keys when none requested.
    assert "before_context" not in res["matches"][0]


# ---------------------------------------------------------------------------
# output_mode variants
# ---------------------------------------------------------------------------


def test_gf_search_files_with_matches(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("alpha\nneedle\n")
    (tmp_path / "b.py").write_text("beta\nneedle\nneedle\n")
    (tmp_path / "c.py").write_text("nothing here\n")
    res = _run(tmp_path, pattern="needle", output_mode="files_with_matches")
    assert res["output_mode"] == "files_with_matches"
    assert "files" in res and "matches" not in res
    assert set(res["files"]) == {"a.py", "b.py"}
    assert "c.py" not in res["files"]


def test_gf_search_count_mode(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("needle\nneedle\nx\n")
    (tmp_path / "b.py").write_text("needle\n")
    res = _run(tmp_path, pattern="needle", output_mode="count")
    assert res["output_mode"] == "count"
    counts = {c["path"]: c["count"] for c in res["counts"]}
    assert counts == {"a.py": 2, "b.py": 1}
    assert res["total_matches"] == 3


def test_gf_search_bad_output_mode_errors(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x\n")
    res = _run(tmp_path, pattern="x", output_mode="bogus")
    assert "args_invalid" in res["error"]


# ---------------------------------------------------------------------------
# case-insensitive
# ---------------------------------------------------------------------------


def test_gf_search_case_insensitive(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("HELLO world\n")
    miss = _run(tmp_path, pattern="hello")
    assert miss["matches"] == []
    hit = _run(tmp_path, pattern="hello", case_insensitive=True)
    assert len(hit["matches"]) == 1
    assert hit["matches"][0]["line"] == 1


# ---------------------------------------------------------------------------
# context lines (-B/-A/-C)
# ---------------------------------------------------------------------------


def test_gf_search_context_before_after(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("L1\nL2\nMATCH\nL4\nL5\n")
    res = _run(tmp_path, pattern="MATCH", before=2, after=1)
    hit = res["matches"][0]
    assert hit["before_context"] == ["L1", "L2"]
    assert hit["after_context"] == ["L4"]


def test_gf_search_context_C_overrides(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("L1\nL2\nMATCH\nL4\nL5\n")
    res = _run(tmp_path, pattern="MATCH", context=1, before=99, after=99)
    hit = res["matches"][0]
    assert hit["before_context"] == ["L2"]
    assert hit["after_context"] == ["L4"]


def test_gf_search_context_at_file_edges(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("MATCH\ntail\n")
    res = _run(tmp_path, pattern="MATCH", context=3)
    hit = res["matches"][0]
    assert hit["before_context"] == []
    assert hit["after_context"] == ["tail"]


# ---------------------------------------------------------------------------
# verbatim lines — indentation preserved, no 300-char strip; long-line marker
# ---------------------------------------------------------------------------


def test_gf_search_preserves_indentation(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("class C:\n        deeply_indented_match = 1\n")
    res = _run(tmp_path, pattern="deeply_indented_match")
    text = res["matches"][0]["text"]
    assert text.startswith("        deeply_indented_match")
    assert text == "        deeply_indented_match = 1"


def test_gf_search_long_line_truncation_marker(tmp_path: Path) -> None:
    long_line = "x" * 5000 + " needle"
    (tmp_path / "a.py").write_text(long_line + "\n")
    res = _run(tmp_path, pattern="needle")
    text = res["matches"][0]["text"]
    assert text.endswith("…(line truncated)")
    assert len(text) <= 2000 + len(" …(line truncated)")


def test_gf_search_short_line_not_truncated(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("short needle line\n")
    res = _run(tmp_path, pattern="needle")
    assert res["matches"][0]["text"] == "short needle line"


# ---------------------------------------------------------------------------
# glob / type pre-filter
# ---------------------------------------------------------------------------


def test_gf_search_glob_prefilter(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("needle\n")
    (tmp_path / "b.txt").write_text("needle\n")
    res = _run(tmp_path, pattern="needle", glob="*.py")
    paths = {m["path"] for m in res["matches"]}
    assert paths == {"a.py"}


def test_gf_search_glob_with_subdir(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("needle\n")
    (tmp_path / "b.py").write_text("needle\n")
    res = _run(tmp_path, pattern="needle", glob="src/**/*.py")
    paths = {m["path"] for m in res["matches"]}
    assert paths == {os.path.join("src", "a.py")}


def test_gf_search_type_prefilter(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("needle\n")
    (tmp_path / "b.md").write_text("needle\n")
    res = _run(tmp_path, pattern="needle", type="md")
    paths = {m["path"] for m in res["matches"]}
    assert paths == {"b.md"}


def test_gf_search_unknown_type_errors(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("needle\n")
    res = _run(tmp_path, pattern="needle", type="cobol")
    assert "args_invalid" in res["error"]
    assert "unknown type" in res["error"]


# ---------------------------------------------------------------------------
# name mode — newest-first ordering + truncation flag
# ---------------------------------------------------------------------------


def test_gf_search_name_mode_mtime_desc(tmp_path: Path) -> None:
    old = tmp_path / "old.py"
    new = tmp_path / "new.py"
    old.write_text("")
    new.write_text("")
    # Make ``new`` strictly newer than ``old``.
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))
    res = _run(tmp_path, pattern="*.py", mode="name")
    assert res["matches"][0] == "new.py"
    assert res["matches"][1] == "old.py"
    assert res["truncated"] is False


def test_gf_search_name_mode_single_match_compat(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("")
    (tmp_path / "y.txt").write_text("")
    res = _run(tmp_path, pattern="*.py", mode="name")
    assert res["matches"] == ["x.py"]
