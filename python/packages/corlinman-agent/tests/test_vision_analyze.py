"""Tests for the ``vision_analyze`` builtin tool.

Covers:
* Tool name wire stability
* OpenAI schema shape
* Happy-path: workspace PNG → base64 data URL content block
* Happy-path: HTTPS URL → forwarded as-is
* Optional ``question`` annotation prepended as text part
* Error envelopes (missing both args, both args, bad URL, missing file)
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from corlinman_agent.image import (
    VISION_ANALYZE_TOOL,
    dispatch_vision_analyze,
    vision_analyze_tool_schema,
)

# ---------------------------------------------------------------------------
# Minimal PNG for workspace tests
# ---------------------------------------------------------------------------

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01"
    b"\x00\x00\x00\x01"
    b"\x08\x02"
    b"\x00\x00\x00"
    b"\x90wS\xde"
)


def _args(**kw: object) -> bytes:
    return json.dumps(kw).encode("utf-8")


# ---------------------------------------------------------------------------
# Wire stability + schema shape
# ---------------------------------------------------------------------------


def test_tool_name_wire_stable() -> None:
    assert VISION_ANALYZE_TOOL == "vision_analyze"


def test_schema_openai_shape() -> None:
    schema = vision_analyze_tool_schema()
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == "vision_analyze"
    props = fn["parameters"]["properties"]
    assert "path" in props
    assert "url" in props
    assert "question" in props
    # Neither 'path' nor 'url' is required — the dispatcher validates at runtime.
    assert fn["parameters"]["required"] == []


# ---------------------------------------------------------------------------
# Happy path: workspace file → base64 content block
# ---------------------------------------------------------------------------


def test_workspace_png_returns_content_block(tmp_path: Path) -> None:
    (tmp_path / "snap.png").write_bytes(_TINY_PNG)

    result = dispatch_vision_analyze(
        args_json=_args(path="snap.png"), workspace=tmp_path
    )

    assert isinstance(result, list), f"Expected list, got {type(result)}: {result!r}"
    assert len(result) == 1
    block = result[0]
    assert block["type"] == "image_url"
    url = block["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    b64_part = url.split(",", 1)[1]
    assert base64.b64decode(b64_part) == _TINY_PNG


def test_workspace_jpg_returns_jpeg_mime(tmp_path: Path) -> None:
    jpeg_bytes = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"\x00\x10JFIF" + b"\x00" * 20
    (tmp_path / "photo.jpg").write_bytes(jpeg_bytes)

    result = dispatch_vision_analyze(
        args_json=_args(path="photo.jpg"), workspace=tmp_path
    )

    assert isinstance(result, list)
    url = result[0]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")


# ---------------------------------------------------------------------------
# Optional question annotation
# ---------------------------------------------------------------------------


def test_question_prepended_as_text_part(tmp_path: Path) -> None:
    (tmp_path / "chart.png").write_bytes(_TINY_PNG)

    result = dispatch_vision_analyze(
        args_json=_args(path="chart.png", question="What does this chart show?"),
        workspace=tmp_path,
    )

    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["type"] == "text"
    assert result[0]["text"] == "What does this chart show?"
    assert result[1]["type"] == "image_url"


def test_empty_question_not_prepended(tmp_path: Path) -> None:
    (tmp_path / "img.png").write_bytes(_TINY_PNG)

    result = dispatch_vision_analyze(
        args_json=_args(path="img.png", question="   "),
        workspace=tmp_path,
    )

    assert isinstance(result, list)
    assert len(result) == 1  # no text part for whitespace-only question
    assert result[0]["type"] == "image_url"


# ---------------------------------------------------------------------------
# URL path: forwarded as-is
# ---------------------------------------------------------------------------


def test_https_url_forwarded_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    # SEC-08 fix runs is_safe_host() (real DNS) on forwarded URLs; this test
    # exercises the safe-host forward-block shape, so stub the SSRF guard to
    # accept. The reject path is covered by test_fix_SEC08_vision_ssrf.py.
    monkeypatch.setattr("corlinman_agent.image.analyze.is_safe_host", lambda url: None)
    url = "https://example.com/image.png"
    result = dispatch_vision_analyze(args_json=_args(url=url))

    assert isinstance(result, list)
    assert len(result) == 1
    block = result[0]
    assert block["type"] == "image_url"
    assert block["image_url"]["url"] == url


def test_https_url_with_question(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("corlinman_agent.image.analyze.is_safe_host", lambda url: None)
    url = "https://example.com/diagram.jpg"
    result = dispatch_vision_analyze(
        args_json=_args(url=url, question="Describe the architecture")
    )

    assert isinstance(result, list)
    assert len(result) == 2
    assert result[0]["type"] == "text"
    assert result[1]["image_url"]["url"] == url


# ---------------------------------------------------------------------------
# Error envelopes
# ---------------------------------------------------------------------------


def test_error_no_path_no_url() -> None:
    result = dispatch_vision_analyze(args_json=_args())
    assert isinstance(result, str)
    data = json.loads(result)
    assert data["ok"] is False
    assert data["error"] == "invalid_args"


def test_error_both_path_and_url(tmp_path: Path) -> None:
    (tmp_path / "img.png").write_bytes(_TINY_PNG)
    result = dispatch_vision_analyze(
        args_json=_args(path="img.png", url="https://example.com/x.png"),
        workspace=tmp_path,
    )
    assert isinstance(result, str)
    data = json.loads(result)
    assert data["ok"] is False
    assert data["error"] == "invalid_args"


def test_error_non_https_url() -> None:
    result = dispatch_vision_analyze(args_json=_args(url="ftp://example.com/x.png"))
    assert isinstance(result, str)
    data = json.loads(result)
    assert data["ok"] is False
    assert data["error"] == "invalid_args"


def test_error_missing_workspace_file(tmp_path: Path) -> None:
    result = dispatch_vision_analyze(
        args_json=_args(path="nonexistent.png"), workspace=tmp_path
    )
    assert isinstance(result, str)
    data = json.loads(result)
    assert data["ok"] is False
    assert data["error"] == "file_not_found"


def test_error_workspace_escape() -> None:
    result = dispatch_vision_analyze(args_json=_args(path="../../etc/passwd"))
    assert isinstance(result, str)
    data = json.loads(result)
    assert data["ok"] is False
    # workspace_escape or file_not_found depending on env
    assert data["error"] in ("workspace_escape", "file_not_found")


def test_error_malformed_args_json() -> None:
    result = dispatch_vision_analyze(args_json=b"{not json")
    assert isinstance(result, str)
    data = json.loads(result)
    assert data["ok"] is False
    assert data["error"] == "invalid_args"
