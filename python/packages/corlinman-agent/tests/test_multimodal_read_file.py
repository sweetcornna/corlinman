"""Tests for multimodal read_file — image content-block return path.

When ``dispatch_read_file`` is called on an image file (extension in
IMAGE_EXTENSIONS) it should return a ``list[dict]`` with a single
``image_url`` content block, not a JSON text envelope.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from corlinman_agent.coding.files import IMAGE_EXTENSIONS, dispatch_read_file


def _args(**kw: object) -> bytes:
    return json.dumps(kw).encode("utf-8")


# ---------------------------------------------------------------------------
# Minimal valid PNG header (8-byte PNG magic + partial IHDR chunk)
# ---------------------------------------------------------------------------

_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n"  # PNG magic
    b"\x00\x00\x00\rIHDR"  # IHDR chunk length + type
    b"\x00\x00\x00\x01"    # width = 1
    b"\x00\x00\x00\x01"    # height = 1
    b"\x08\x02"             # bit depth = 8, color type = 2 (RGB)
    b"\x00\x00\x00"         # compression, filter, interlace
    b"\x90wS\xde"           # CRC
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
)

_TINY_JPEG = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"\x00\x10JFIF\x00" + b"\x00" * 20


# ---------------------------------------------------------------------------
# IMAGE_EXTENSIONS constant
# ---------------------------------------------------------------------------


def test_image_extensions_frozenset() -> None:
    assert isinstance(IMAGE_EXTENSIONS, frozenset)
    assert ".png" in IMAGE_EXTENSIONS
    assert ".jpg" in IMAGE_EXTENSIONS
    assert ".jpeg" in IMAGE_EXTENSIONS
    assert ".gif" in IMAGE_EXTENSIONS
    assert ".webp" in IMAGE_EXTENSIONS


# ---------------------------------------------------------------------------
# Happy path — PNG file returns content-block list
# ---------------------------------------------------------------------------


def test_read_png_returns_content_block_list(tmp_path: Path) -> None:
    img = tmp_path / "photo.png"
    img.write_bytes(_TINY_PNG)

    result = dispatch_read_file(args_json=_args(path="photo.png"), workspace=tmp_path)

    assert isinstance(result, list), f"Expected list, got {type(result)}: {result!r}"
    assert len(result) == 1
    block = result[0]
    assert block["type"] == "image_url"
    assert "image_url" in block
    url = block["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    # Verify base64 decodes back to original bytes.
    b64_part = url.split(",", 1)[1]
    assert base64.b64decode(b64_part) == _TINY_PNG


def test_read_jpg_returns_content_block_list(tmp_path: Path) -> None:
    img = tmp_path / "shot.jpg"
    img.write_bytes(_TINY_JPEG)

    result = dispatch_read_file(args_json=_args(path="shot.jpg"), workspace=tmp_path)

    assert isinstance(result, list)
    block = result[0]
    url = block["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")


def test_read_jpeg_extension(tmp_path: Path) -> None:
    img = tmp_path / "pic.jpeg"
    img.write_bytes(_TINY_JPEG)

    result = dispatch_read_file(args_json=_args(path="pic.jpeg"), workspace=tmp_path)

    assert isinstance(result, list)
    url = result[0]["image_url"]["url"]
    assert "image/jpeg" in url


def test_read_gif_returns_content_block_list(tmp_path: Path) -> None:
    # Minimal GIF89a header
    gif_bytes = b"GIF89a\x01\x00\x01\x00\x00\x00\x00\x3b"
    img = tmp_path / "anim.gif"
    img.write_bytes(gif_bytes)

    result = dispatch_read_file(args_json=_args(path="anim.gif"), workspace=tmp_path)

    assert isinstance(result, list)
    url = result[0]["image_url"]["url"]
    assert "image/gif" in url


def test_read_webp_returns_content_block_list(tmp_path: Path) -> None:
    webp_bytes = b"RIFF\x00\x00\x00\x00WEBPVP8 "
    img = tmp_path / "image.webp"
    img.write_bytes(webp_bytes)

    result = dispatch_read_file(args_json=_args(path="image.webp"), workspace=tmp_path)

    assert isinstance(result, list)
    url = result[0]["image_url"]["url"]
    assert "image/webp" in url


# ---------------------------------------------------------------------------
# Text file is still returned as JSON str
# ---------------------------------------------------------------------------


def test_text_file_still_returns_str(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hello world\n")

    result = dispatch_read_file(
        args_json=_args(path="notes.txt"), workspace=tmp_path
    )

    assert isinstance(result, str)
    data = json.loads(result)
    assert "content" in data
    assert "hello world" in data["content"]


def test_py_file_still_returns_str(tmp_path: Path) -> None:
    (tmp_path / "script.py").write_text("print('hi')\n")

    result = dispatch_read_file(
        args_json=_args(path="script.py"), workspace=tmp_path
    )

    assert isinstance(result, str)
    data = json.loads(result)
    assert "print" in data["content"]


# ---------------------------------------------------------------------------
# Error cases still return str envelopes
# ---------------------------------------------------------------------------


def test_missing_image_file_returns_error_str(tmp_path: Path) -> None:
    result = dispatch_read_file(
        args_json=_args(path="missing.png"), workspace=tmp_path
    )
    assert isinstance(result, str)
    data = json.loads(result)
    assert data["error"] == "file_not_found"


def test_image_directory_returns_not_a_file_error(tmp_path: Path) -> None:
    (tmp_path / "mydir.png").mkdir()
    result = dispatch_read_file(
        args_json=_args(path="mydir.png"), workspace=tmp_path
    )
    assert isinstance(result, str)
    data = json.loads(result)
    assert data["error"] == "not_a_file"
