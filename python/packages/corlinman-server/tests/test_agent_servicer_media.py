"""W4 — tool-produced media registered into the web file store.

``_register_tool_media`` rewrites local media paths in a builtin tool's
result JSON to ``/v1/files/{id}`` urls (browsers can't fetch server
filesystem paths) and collects slim metadata for the final assistant
journal row. Best-effort: anything unrecognised passes through verbatim.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from corlinman_agent.reasoning_loop import Attachment
from corlinman_server.agent_servicer import (
    _attachment_meta_for_journal,
    _register_tool_media,
)


@pytest.fixture(autouse=True)
def _data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from corlinman_server.gateway.routes import files as files_route

    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    # Clear any entrypoint-stamped configured dir left by sibling tests
    # so the env override above is the one that resolves.
    monkeypatch.setattr(files_route, "_CONFIGURED_DATA_DIR", None)
    return tmp_path


def _make_png(tmp_path: Path, name: str = "gen.png") -> Path:
    p = tmp_path / name
    p.write_bytes(b"\x89PNG-fake")
    return p


def test_image_path_rewritten_to_url(tmp_path: Path) -> None:
    img = _make_png(tmp_path)
    media: list[dict[str, object]] = []
    out = _register_tool_media(json.dumps({"path": str(img)}), media)

    parsed = json.loads(out)
    assert parsed["path"] == str(img)
    assert parsed["url"].startswith("/v1/files/")
    assert "![" in parsed["display_note"]
    assert len(media) == 1
    assert media[0]["kind"] == "image"
    assert media[0]["url"] == parsed["url"]
    assert media[0]["mime"] == "image/png"
    assert media[0]["size"] == len(b"\x89PNG-fake")
    # The registered blob actually exists in the store.
    file_id = parsed["url"].rsplit("/", 1)[-1]
    assert (tmp_path / "files" / f"{file_id}.blob").read_bytes() == b"\x89PNG-fake"


def test_attachment_meta_for_journal_records_byte_size() -> None:
    meta = _attachment_meta_for_journal(
        [
            Attachment(
                kind="file",
                url="/v1/files/f-123",
                bytes_=b"hello-bytes",
                mime="text/plain",
                file_name="notes.txt",
            )
        ]
    )

    assert meta == [
        {
            "kind": "file",
            "url": "/v1/files/f-123",
            "mime": "text/plain",
            "name": "notes.txt",
            "size": 11,
        }
    ]


def test_paths_list_rewritten(tmp_path: Path) -> None:
    a = _make_png(tmp_path, "a.png")
    b = _make_png(tmp_path, "b.webp")
    media: list[dict[str, object]] = []
    out = _register_tool_media(json.dumps({"paths": [str(a), str(b)]}), media)
    parsed = json.loads(out)
    assert len(parsed["urls"]) == 2
    assert len(media) == 2


def test_non_media_results_pass_through(tmp_path: Path) -> None:
    media: list[dict[str, object]] = []
    for raw in (
        json.dumps({"stdout": "ok"}),
        json.dumps({"path": str(tmp_path / "report.json")}),
        json.dumps({"path": str(tmp_path / "missing.png")}),
        json.dumps([1, 2, 3]),
        "not json at all",
        "",
    ):
        assert _register_tool_media(raw, media) == raw
    assert media == []


def test_audio_note_avoids_image_markdown(tmp_path: Path) -> None:
    """Non-image media must NOT instruct the model to embed ![…](…) —
    that renders a broken <img> for an .mp3 (Codex review follow-up)."""
    p = tmp_path / "tts.mp3"
    p.write_bytes(b"ID3-fake-audio")
    media: list[dict[str, object]] = []
    out = _register_tool_media(json.dumps({"path": str(p)}), media)
    parsed = json.loads(out)
    assert parsed["url"].startswith("/v1/files/")
    assert "![" not in parsed["display_note"]
    assert "audio" in parsed["display_note"]
    assert media[0]["kind"] == "audio"


def test_text_file_with_media_suffix_only(tmp_path: Path) -> None:
    """Suffix gate: a .txt the tool wrote is never registered even though
    the file exists."""
    p = tmp_path / "notes.txt"
    p.write_text("hello", encoding="utf-8")
    media: list[dict[str, object]] = []
    raw = json.dumps({"path": str(p)})
    assert _register_tool_media(raw, media) == raw
    assert media == []
