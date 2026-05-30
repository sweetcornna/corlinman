"""Tests for the ``corlinman-md2pdf`` renderer (v1.12.3 — the reliable
Markdown→PDF pipeline the document-generator skill uses).

The HTML-conversion tests are engine-independent; the PDF smoke test is
skipped when no rendering engine (Chrome/Chromium/WeasyPrint) is available
on the box so CI without a browser stays green.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from corlinman_server.tools.doc_render import (
    _find_chrome,
    _markdown_to_html,
    _minimal_markdown,
    render_markdown_to_pdf,
)

_SAMPLE = """# 报告标题

一段中文说明。

## 小节

- **加粗项** 普通文本
- 第二项 `code`

| 列A | 列B |
|-----|-----|
| 单元1 | 单元2 |

> 引用一句话

1. 第一
2. 第二
"""


def test_minimal_markdown_covers_report_elements() -> None:
    html = _minimal_markdown(_SAMPLE)
    assert "<h1>报告标题</h1>" in html
    assert "<h2>小节</h2>" in html
    assert "<strong>加粗项</strong>" in html
    assert "<code>code</code>" in html
    assert "<table>" in html and "<th>列A</th>" in html and "<td>单元1</td>" in html
    assert "<blockquote>" in html
    assert "<ol>" in html and "<li>第一</li>" in html


def test_markdown_to_html_handles_cjk_without_spacing() -> None:
    """CJK text must survive verbatim (no inserted spaces) — the exact
    failure of the hand-rolled raw-PDF path."""
    html = _markdown_to_html(_SAMPLE)
    assert "报告标题" in html
    assert "报 告 标 题" not in html  # no letter-spacing corruption


def test_minimal_markdown_escapes_html() -> None:
    html = _minimal_markdown("a < b & c > d")
    assert "&lt;" in html and "&amp;" in html and "&gt;" in html


@pytest.mark.skipif(_find_chrome() is None, reason="no chrome/chromium engine on this box")
def test_render_produces_valid_pdf(tmp_path: Path) -> None:
    md = tmp_path / "r.md"
    md.write_text(_SAMPLE, encoding="utf-8")
    out = render_markdown_to_pdf(md, tmp_path / "r.pdf", title="测试")
    assert out.exists()
    data = out.read_bytes()
    assert data[:5] == b"%PDF-"
    assert len(data) > 1000  # a real rendered page, not an empty stub
