"""Gap-fill (lane-edit-files) tests for raised read/edit fidelity in
``coding/files.py``.

Covers the parity gaps:
- read-multimodal-image-pdf-notebook (PDF + .ipynb halves)
- edit-quote-crlf-normalization (curly quotes, CRLF, BOM round-trip)
- edit-block-anchor-tier (tier-4 first/last-line anchor, unique only)
- compact unified-diff snippet on edit_file / write_file results

The IMAGE half is exercised by test_multimodal_read_file.py and is not
duplicated here.

Uniquely named ``test_gf_edit_files_*`` so it never collides with sibling
gap-fill lanes.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

from corlinman_agent.coding.files import (
    dispatch_edit_file,
    dispatch_read_file,
    dispatch_write_file,
)


def _args(**kw: object) -> bytes:
    return json.dumps(kw).encode("utf-8")


# ---------------------------------------------------------------------------
# edit-quote-crlf-normalization: CRLF preservation
# ---------------------------------------------------------------------------


def test_gf_edit_files_crlf_file_stays_crlf(tmp_path: Path) -> None:
    p = tmp_path / "crlf.txt"
    p.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")

    result = dispatch_edit_file(
        args_json=_args(path="crlf.txt", old_string="beta", new_string="BETA"),
        workspace=tmp_path,
    )
    data = json.loads(result)
    assert data["replacements"] == 1
    # The on-disk bytes must keep CRLF line endings.
    assert p.read_bytes() == b"alpha\r\nBETA\r\ngamma\r\n"


def test_gf_edit_files_lf_file_stays_lf(tmp_path: Path) -> None:
    p = tmp_path / "lf.txt"
    p.write_bytes(b"one\ntwo\nthree\n")

    dispatch_edit_file(
        args_json=_args(path="lf.txt", old_string="two", new_string="TWO"),
        workspace=tmp_path,
    )
    assert p.read_bytes() == b"one\nTWO\nthree\n"


def test_gf_edit_files_crlf_old_string_matches_lf_file(tmp_path: Path) -> None:
    # Model supplies a multi-line old_string with CRLF; the file is LF.
    p = tmp_path / "mix.txt"
    p.write_bytes(b"first line\nsecond line\nthird line\n")

    result = dispatch_edit_file(
        args_json=_args(
            path="mix.txt",
            old_string="first line\r\nsecond line",
            new_string="FIRST\nSECOND",
        ),
        workspace=tmp_path,
    )
    data = json.loads(result)
    assert "error" not in data, data
    assert p.read_bytes() == b"FIRST\nSECOND\nthird line\n"


# ---------------------------------------------------------------------------
# edit-quote-crlf-normalization: BOM round-trip
# ---------------------------------------------------------------------------


def test_gf_edit_files_utf8_bom_preserved(tmp_path: Path) -> None:
    p = tmp_path / "bom.txt"
    p.write_bytes(b"\xef\xbb\xbfhello world\n")

    dispatch_edit_file(
        args_json=_args(path="bom.txt", old_string="world", new_string="there"),
        workspace=tmp_path,
    )
    assert p.read_bytes() == b"\xef\xbb\xbfhello there\n"


def test_gf_edit_files_utf16_bom_preserved(tmp_path: Path) -> None:
    p = tmp_path / "u16.txt"
    p.write_bytes("hello world\n".encode("utf-16"))

    result = dispatch_edit_file(
        args_json=_args(path="u16.txt", old_string="world", new_string="there"),
        workspace=tmp_path,
    )
    assert "error" not in json.loads(result), result
    # Still decodes as UTF-16 and carries the edit.
    assert p.read_bytes().decode("utf-16") == "hello there\n"


# ---------------------------------------------------------------------------
# edit-quote-crlf-normalization: curly -> straight quotes
# ---------------------------------------------------------------------------


def test_gf_edit_files_curly_quotes_match_straight(tmp_path: Path) -> None:
    p = tmp_path / "q.txt"
    p.write_text('value = "plain"\n', encoding="utf-8")

    # Model emits smart quotes that never appear in the source.
    result = dispatch_edit_file(
        args_json=_args(
            path="q.txt",
            old_string="value = “plain”",
            new_string="value = OK",
        ),
        workspace=tmp_path,
    )
    data = json.loads(result)
    assert "error" not in data, data
    assert data.get("match_tier") == "normalized"
    assert p.read_text(encoding="utf-8") == "value = OK\n"


# ---------------------------------------------------------------------------
# edit-block-anchor-tier: tier-4 first/last line anchor
# ---------------------------------------------------------------------------


def test_gf_edit_files_block_anchor_tolerates_interior_drift(
    tmp_path: Path,
) -> None:
    p = tmp_path / "anchor.py"
    p.write_text(
        "def foo():\n    a = 1\n    b = 2\n    return a + b\n",
        encoding="utf-8",
    )

    # First + last line are correct; the interior the model remembered is
    # wrong. Tiers 1-3 cannot match; tier-4 anchors on first/last.
    result = dispatch_edit_file(
        args_json=_args(
            path="anchor.py",
            old_string="def foo():\n    WRONG INTERIOR LINE\n    return a + b",
            new_string="def foo():\n    return 99",
        ),
        workspace=tmp_path,
    )
    data = json.loads(result)
    assert data.get("match_tier") == "block-anchor", data
    assert p.read_text(encoding="utf-8") == "def foo():\n    return 99\n"


def test_gf_edit_files_block_anchor_rejected_when_ambiguous(
    tmp_path: Path,
) -> None:
    # Two spans share the same first/last anchor lines -> ambiguous, refuse.
    p = tmp_path / "amb.py"
    p.write_text(
        "BEGIN\n    x\nEND\nfiller\nBEGIN\n    y\nEND\n",
        encoding="utf-8",
    )

    result = dispatch_edit_file(
        args_json=_args(
            path="amb.py",
            old_string="BEGIN\n    DRIFT\nEND",
            new_string="BEGIN\n    z\nEND",
        ),
        workspace=tmp_path,
    )
    data = json.loads(result)
    assert "old_string_not_unique" in data.get("error", ""), data
    # File untouched.
    assert "DRIFT" not in p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# diff snippet present on edit_file / write_file
# ---------------------------------------------------------------------------


def test_gf_edit_files_diff_snippet_present_on_edit(tmp_path: Path) -> None:
    p = tmp_path / "d.txt"
    p.write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = dispatch_edit_file(
        args_json=_args(path="d.txt", old_string="two", new_string="TWO"),
        workspace=tmp_path,
    )
    data = json.loads(result)
    assert "diff" in data
    assert "-two" in data["diff"] and "+TWO" in data["diff"]


def test_gf_edit_files_diff_snippet_present_on_write_overwrite(
    tmp_path: Path,
) -> None:
    p = tmp_path / "w.txt"
    p.write_text("old content\n", encoding="utf-8")

    result = dispatch_write_file(
        args_json=_args(path="w.txt", content="new content\n"),
        workspace=tmp_path,
    )
    data = json.loads(result)
    assert data["action"] == "overwritten"
    assert "diff" in data
    assert "-old content" in data["diff"]
    assert "+new content" in data["diff"]


def test_gf_edit_files_diff_snippet_present_on_write_new_file(
    tmp_path: Path,
) -> None:
    result = dispatch_write_file(
        args_json=_args(path="fresh.txt", content="line a\nline b\n"),
        workspace=tmp_path,
    )
    data = json.loads(result)
    assert data["action"] == "created"
    # New file: the diff is the content as additions.
    assert "diff" in data
    assert "+line a" in data["diff"]


# ---------------------------------------------------------------------------
# .ipynb notebook parsing
# ---------------------------------------------------------------------------


def test_gf_edit_files_ipynb_parses_cells_and_outputs(tmp_path: Path) -> None:
    nb = {
        "cells": [
            {"cell_type": "markdown", "source": ["# Heading\n", "body"]},
            {
                "cell_type": "code",
                "source": ["print('hi')\n"],
                "outputs": [
                    {"output_type": "stream", "text": ["hi\n"]},
                    {
                        "output_type": "execute_result",
                        "data": {"text/plain": ["42"]},
                    },
                ],
            },
        ],
        "nbformat": 4,
    }
    p = tmp_path / "nb.ipynb"
    p.write_text(json.dumps(nb), encoding="utf-8")

    result = dispatch_read_file(args_json=_args(path="nb.ipynb"), workspace=tmp_path)
    assert isinstance(result, str)
    data = json.loads(result)
    assert data["kind"] == "notebook"
    assert data["cells"] == 2
    content = data["content"]
    assert "[cell 1] markdown" in content
    assert "# Heading" in content
    assert "[cell 2] code" in content
    assert "print('hi')" in content
    assert "hi" in content  # stream output
    assert "42" in content  # execute_result text/plain


def test_gf_edit_files_ipynb_truncates_huge_output(tmp_path: Path) -> None:
    nb = {
        "cells": [
            {
                "cell_type": "code",
                "source": ["x"],
                "outputs": [
                    {"output_type": "stream", "text": ["A" * 50_000]},
                ],
            }
        ]
    }
    p = tmp_path / "big.ipynb"
    p.write_text(json.dumps(nb), encoding="utf-8")

    result = dispatch_read_file(args_json=_args(path="big.ipynb"), workspace=tmp_path)
    data = json.loads(result)
    assert "[output truncated]" in data["content"]


def test_gf_edit_files_ipynb_invalid_json(tmp_path: Path) -> None:
    p = tmp_path / "bad.ipynb"
    p.write_text("not json at all", encoding="utf-8")

    result = dispatch_read_file(args_json=_args(path="bad.ipynb"), workspace=tmp_path)
    data = json.loads(result)
    assert data["error"] == "ipynb_invalid_json"


# ---------------------------------------------------------------------------
# .pdf read — fallback path (no optional lib) returns base64 file block
# ---------------------------------------------------------------------------


def test_gf_edit_files_pdf_fallback_returns_file_block(
    tmp_path: Path, monkeypatch
) -> None:
    # Force the no-library path by hiding any installed parser.
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *a, **kw):
        if name == "pypdf" or name.startswith("pdfminer"):
            raise ImportError(f"blocked {name}")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _blocked)

    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4 minimal")

    result = dispatch_read_file(args_json=_args(path="doc.pdf"), workspace=tmp_path)
    assert isinstance(result, list), result
    file_block = result[0]
    assert file_block["type"] == "file"
    assert file_block["file"]["filename"] == "doc.pdf"
    assert file_block["file"]["file_data"].startswith("data:application/pdf;base64,")
    # A human-readable note explains the fallback.
    assert any(
        b.get("type") == "text" and "library" in b.get("text", "") for b in result
    )


def test_gf_edit_files_pdf_with_lib_extracts_text(
    tmp_path: Path, monkeypatch
) -> None:
    # Inject a fake pypdf so the tier-1 extraction branch runs without a
    # real (heavy) dependency.
    fake = types.ModuleType("pypdf")

    class _Page:
        def __init__(self, text: str) -> None:
            self._text = text

        def extract_text(self) -> str:
            return self._text

    class _Reader:
        def __init__(self, _path: str) -> None:
            self.pages = [_Page("Alpha page"), _Page("Beta page"), _Page("Gamma")]

    fake.PdfReader = _Reader  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pypdf", fake)

    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4 stub")

    # All pages.
    result = dispatch_read_file(args_json=_args(path="doc.pdf"), workspace=tmp_path)
    assert isinstance(result, str), result
    data = json.loads(result)
    assert data["kind"] == "pdf"
    assert data["engine"] == "pypdf"
    assert data["pages_total"] == 3
    assert "Alpha page" in data["content"]
    assert "Gamma" in data["content"]

    # pages='2-3' selects a subrange.
    sub = dispatch_read_file(
        args_json=_args(path="doc.pdf", pages="2-3"), workspace=tmp_path
    )
    sub_data = json.loads(sub)
    assert sub_data["pages_shown"] == [2, 3]
    assert "Alpha page" not in sub_data["content"]
    assert "Beta page" in sub_data["content"]


def test_gf_edit_files_pdf_single_page_select(tmp_path: Path, monkeypatch) -> None:
    fake = types.ModuleType("pypdf")

    class _Page:
        def __init__(self, text: str) -> None:
            self._text = text

        def extract_text(self) -> str:
            return self._text

    class _Reader:
        def __init__(self, _path: str) -> None:
            self.pages = [_Page("P1"), _Page("P2"), _Page("P3")]

    fake.PdfReader = _Reader  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pypdf", fake)

    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4")
    result = dispatch_read_file(
        args_json=_args(path="doc.pdf", pages="2"), workspace=tmp_path
    )
    data = json.loads(result)
    assert data["pages_shown"] == [2]
    assert "P2" in data["content"]
    assert "P1" not in data["content"]
