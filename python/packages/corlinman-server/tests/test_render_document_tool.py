"""Tests for the ``render_document`` builtin tool.

The tool-shaped successor to the always-on ``document-generator`` skill:
its interface must make the v1.12.3 failure modes (hand-rolled PDF bytes,
mislabeled artifacts, out-of-workspace writes) impossible in shape. The
render-heavy path is mocked for the dispatch tests; a real end-to-end
render runs only when a PDF engine is present on the box (same policy as
``test_doc_render.py``).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from corlinman_server.tools import render_document as rd
from corlinman_server.tools.doc_render import _find_chrome

_SAMPLE = """# 测试报告

一段中文说明，包含 **加粗** 与 `代码`。

## 数据

| 列A | 列B |
|-----|-----|
| 单元1 | 单元2 |

```python
print("hello 世界")
```
"""


def _fake_renderer(md_text: str, pdf_path: Path, *, title: str = "") -> Path:
    """Stand-in for the md2pdf pipeline: writes a tiny valid-magic file."""
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.4 fake\n" + md_text.encode("utf-8")[:64])
    return pdf_path


# ---------------------------------------------------------------------------
# Schema / registration surface
# ---------------------------------------------------------------------------


def test_schema_shape_prevents_freeform_output() -> None:
    schema = rd.render_document_tool_schema()
    fn = schema["function"]
    assert fn["name"] == rd.RENDER_DOCUMENT_TOOL == "render_document"
    params = fn["parameters"]
    # Markdown source is the ONLY required input — no engine, no flags,
    # no raw-bytes channel.
    assert params["required"] == ["content"]
    assert params["additionalProperties"] is False
    assert set(params["properties"]) == {"content", "title", "path", "format"}
    assert params["properties"]["format"]["enum"] == list(rd.SUPPORTED_FORMATS)


def test_tool_is_registered_and_advertised() -> None:
    from corlinman_server.agent_servicer import (
        BUILTIN_TOOLS,
        _builtin_tool_schemas,
    )

    assert rd.RENDER_DOCUMENT_TOOL in BUILTIN_TOOLS
    names = {s["function"]["name"] for s in _builtin_tool_schemas()}
    assert rd.RENDER_DOCUMENT_TOOL in names


def test_tool_is_mutating_for_permission_gate() -> None:
    from corlinman_agent.permission import MUTATING_TOOLS

    assert rd.RENDER_DOCUMENT_TOOL in MUTATING_TOOLS


# ---------------------------------------------------------------------------
# Argument validation — the shape guards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        ({}, "content"),
        ({"content": "   "}, "content"),
        ({"content": 42}, "content"),
        ({"content": "%PDF-1.7 raw bytes"}, "Markdown source"),
        ({"content": "# ok", "format": "docx"}, "format"),
        ({"content": "# ok", "title": 7}, "title"),
        ({"content": "# ok", "path": 3}, "path"),
    ],
)
async def test_dispatch_rejects_malformed_args(
    tmp_path: Path, args: dict[str, Any], fragment: str
) -> None:
    out = json.loads(
        await rd.dispatch_render_document(
            args_json=json.dumps(args), workspace=tmp_path
        )
    )
    assert out["error"].startswith("args_invalid:")
    assert fragment in out["error"]


@pytest.mark.asyncio
async def test_dispatch_treats_explicit_null_like_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Models routinely emit ``"title": null`` — every OPTIONAL param must
    read explicit JSON null the same as an absent key (``path`` always
    did; ``title``/``format`` were hard-rejected, costing a round-trip)."""
    monkeypatch.setattr(rd, "render_markdown_text_to_pdf", _fake_renderer)
    out = json.loads(
        await rd.dispatch_render_document(
            args_json=json.dumps(
                {"content": _SAMPLE, "title": None, "format": None, "path": None}
            ),
            workspace=tmp_path,
        )
    )
    assert "error" not in out
    assert out["format"] == "pdf"


@pytest.mark.asyncio
async def test_servicer_routes_render_document_to_dispatcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the servicer's ``_dispatch_builtin`` with a render_document
    tool call — registration/schema tests alone would stay green with a
    missing dispatch branch (the call would fall through to the
    unknown-tool envelope)."""
    from corlinman_agent.reasoning_loop import ChatStart, ToolCallEvent
    from corlinman_server.agent_servicer import CorlinmanAgentServicer

    monkeypatch.setenv("CORLINMAN_AGENT_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(rd, "render_markdown_text_to_pdf", _fake_renderer)

    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: None)
    start = ChatStart(model="m", messages=[], tools=[], session_key="t::s1")
    event = ToolCallEvent(
        call_id="c1",
        plugin="builtin",
        tool=rd.RENDER_DOCUMENT_TOOL,
        args_json=json.dumps({"content": _SAMPLE, "path": "routed.pdf"}).encode(),
    )
    out = json.loads(await servicer._dispatch_builtin(event, start, None))
    assert "error" not in out
    assert out["path"] == "routed.pdf"
    assert (tmp_path / "routed.pdf").read_bytes().startswith(b"%PDF")


@pytest.mark.asyncio
async def test_dispatch_rejects_oversized_content(tmp_path: Path) -> None:
    big = "x" * (rd.MAX_CONTENT_BYTES + 1)
    out = json.loads(
        await rd.dispatch_render_document(
            args_json=json.dumps({"content": big}), workspace=tmp_path
        )
    )
    assert out["error"].startswith("args_invalid:")
    assert "too large" in out["error"]


@pytest.mark.asyncio
async def test_dispatch_confines_output_to_workspace(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    out = json.loads(
        await rd.dispatch_render_document(
            args_json=json.dumps(
                {"content": "# t", "path": "../escape.pdf"}
            ),
            workspace=ws,
        )
    )
    assert out["error"].startswith("workspace_escape:")
    assert not (tmp_path / "escape.pdf").exists()


# ---------------------------------------------------------------------------
# Dispatch — success paths (pipeline mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_success_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rd, "render_markdown_text_to_pdf", _fake_renderer)
    out = json.loads(
        await rd.dispatch_render_document(
            args_json=json.dumps(
                {"content": _SAMPLE, "path": "report.pdf", "title": "测试"}
            ),
            workspace=tmp_path,
        )
    )
    assert "error" not in out
    assert out["path"] == "report.pdf"
    assert out["format"] == "pdf"
    assert out["title"] == "测试"
    assert out["bytes"] > 0
    assert (tmp_path / "report.pdf").read_bytes().startswith(b"%PDF")


@pytest.mark.asyncio
async def test_dispatch_forces_format_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mislabeled artifact (``.txt`` "PDF") must be impossible."""
    monkeypatch.setattr(rd, "render_markdown_text_to_pdf", _fake_renderer)
    out = json.loads(
        await rd.dispatch_render_document(
            args_json=json.dumps({"content": "# t", "path": "report"}),
            workspace=tmp_path,
        )
    )
    assert out["path"] == "report.pdf"
    assert (tmp_path / "report.pdf").exists()


@pytest.mark.asyncio
async def test_dispatch_derives_filename_from_cjk_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rd, "render_markdown_text_to_pdf", _fake_renderer)
    out = json.loads(
        await rd.dispatch_render_document(
            args_json=json.dumps({"content": "# 周报 第 3 期\n\n内容"}),
            workspace=tmp_path,
        )
    )
    assert out["path"].endswith(".pdf")
    # CJK survives; separators are folded to underscores; no path traversal.
    assert "周报" in out["path"]
    assert "/" not in out["path"]
    assert (tmp_path / out["path"]).exists()


@pytest.mark.asyncio
async def test_dispatch_default_filename_without_heading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rd, "render_markdown_text_to_pdf", _fake_renderer)
    out = json.loads(
        await rd.dispatch_render_document(
            args_json=json.dumps({"content": "just a paragraph"}),
            workspace=tmp_path,
        )
    )
    assert out["path"] == "document.pdf"


@pytest.mark.asyncio
async def test_dispatch_creates_nested_output_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rd, "render_markdown_text_to_pdf", _fake_renderer)
    out = json.loads(
        await rd.dispatch_render_document(
            args_json=json.dumps({"content": "# t", "path": "out/r.pdf"}),
            workspace=tmp_path,
        )
    )
    assert out["path"] == "out/r.pdf"
    assert (tmp_path / "out" / "r.pdf").exists()


# ---------------------------------------------------------------------------
# Dispatch — failure wrapping (never raises)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_wraps_pipeline_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(md_text: str, pdf_path: Path, *, title: str = "") -> Path:
        raise RuntimeError("no working PDF engine")

    monkeypatch.setattr(rd, "render_markdown_text_to_pdf", _boom)
    out = json.loads(
        await rd.dispatch_render_document(
            args_json=json.dumps({"content": "# t", "path": "r.pdf"}),
            workspace=tmp_path,
        )
    )
    assert out["error"].startswith("render_failed:")
    assert "no working PDF engine" in out["error"]


@pytest.mark.asyncio
async def test_dispatch_bounds_render_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _slow(md_text: str, pdf_path: Path, *, title: str = "") -> Path:
        time.sleep(0.5)
        return pdf_path

    monkeypatch.setattr(rd, "render_markdown_text_to_pdf", _slow)
    monkeypatch.setattr(rd, "RENDER_TIMEOUT_SECS", 0.05)
    out = json.loads(
        await rd.dispatch_render_document(
            args_json=json.dumps({"content": "# t", "path": "r.pdf"}),
            workspace=tmp_path,
        )
    )
    assert out["error"].startswith("render_failed:")
    assert "budget" in out["error"]


@pytest.mark.asyncio
async def test_dispatch_rejects_non_json_args(tmp_path: Path) -> None:
    out = json.loads(
        await rd.dispatch_render_document(
            args_json=b"not json at all", workspace=tmp_path
        )
    )
    assert out["error"].startswith("args_invalid:")


# ---------------------------------------------------------------------------
# End-to-end (engine-dependent, mirrors test_doc_render's skip policy)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(
    _find_chrome() is None, reason="no chrome/chromium engine on this box"
)
async def test_dispatch_renders_real_cjk_pdf(tmp_path: Path) -> None:
    out = json.loads(
        await rd.dispatch_render_document(
            args_json=json.dumps(
                {"content": _SAMPLE, "path": "real.pdf", "title": "测试报告"}
            ),
            workspace=tmp_path,
        )
    )
    assert "error" not in out, out
    data = (tmp_path / "real.pdf").read_bytes()
    assert data[:5] == b"%PDF-"
    assert len(data) > 1000
