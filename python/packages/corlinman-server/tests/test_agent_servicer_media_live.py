"""Live attachment delivery — agent-servicer half.

Covers the two servicer-side pieces of the feature:

* ``_register_tool_media(..., force=, registered=)`` — ``force`` lifts
  the media-suffix gate so ``send_attachment`` can ship a .pdf / .zip /
  anything; ``registered`` is the per-turn dedup cache so two tools
  shipping the same file produce ONE gallery entry, not two.
* the ``send_attachment`` builtin dispatch result now carries
  ``path`` / ``filename`` / ``caption`` so the media-registration hook
  at the call site can pick the file up (channel handlers keep
  intercepting the ToolCall *frame*, not this result).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from corlinman_agent.reasoning_loop import ChatStart, ToolCallEvent
from corlinman_providers.base import ProviderChunk
from corlinman_server.agent_servicer import (
    SEND_ATTACHMENT_TOOL,
    CorlinmanAgentServicer,
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


# ─── _register_tool_media: force ──────────────────────────────────────


def test_force_registers_non_media_suffix(tmp_path: Path) -> None:
    """``force=True`` lifts the suffix gate: a .pdf the agent wants to
    send_attachment is registered even though it's not in
    ``_MEDIA_SUFFIXES``."""
    p = tmp_path / "report.pdf"
    p.write_bytes(b"%PDF-fake")
    media: list[dict[str, str]] = []
    out = _register_tool_media(json.dumps({"path": str(p)}), media, force=True)
    parsed = json.loads(out)
    assert parsed["url"].startswith("/v1/files/")
    assert len(media) == 1
    assert media[0]["kind"] == "file"
    assert media[0]["mime"] == "application/pdf"
    assert media[0]["name"] == "report.pdf"


def test_default_still_gated_by_suffix(tmp_path: Path) -> None:
    """Without ``force`` the suffix gate is unchanged (other call sites
    keep default behavior)."""
    p = tmp_path / "report.pdf"
    p.write_bytes(b"%PDF-fake")
    media: list[dict[str, str]] = []
    raw = json.dumps({"path": str(p)})
    assert _register_tool_media(raw, media) == raw
    assert media == []


# ─── _register_tool_media: per-turn dedup cache ───────────────────────


def test_registered_cache_dedups_same_path(tmp_path: Path) -> None:
    """Second registration of the same file this turn reuses the cached
    meta — the result still gains ``url`` / ``display_note``, but no
    duplicate ``turn_media`` gallery entry is appended."""
    p = tmp_path / "gen.png"
    p.write_bytes(b"\x89PNG-fake")
    media: list[dict[str, str]] = []
    registered: dict[str, dict[str, str]] = {}

    first = json.loads(
        _register_tool_media(
            json.dumps({"path": str(p)}), media, registered=registered
        )
    )
    assert len(media) == 1

    # Same file again (e.g. send_attachment after image_generate) — note
    # the unresolved relative spelling still lands on the same cache key.
    second = json.loads(
        _register_tool_media(
            json.dumps({"path": str(p)}),
            media,
            force=True,
            registered=registered,
        )
    )
    assert len(media) == 1, "duplicate gallery entry appended"
    assert second["url"] == first["url"]
    assert "display_note" in second


def test_registered_cache_distinct_paths_both_register(tmp_path: Path) -> None:
    a = tmp_path / "a.png"
    b = tmp_path / "b.png"
    a.write_bytes(b"\x89PNG-a")
    b.write_bytes(b"\x89PNG-b")
    media: list[dict[str, str]] = []
    registered: dict[str, dict[str, str]] = {}
    _register_tool_media(json.dumps({"path": str(a)}), media, registered=registered)
    _register_tool_media(json.dumps({"path": str(b)}), media, registered=registered)
    assert len(media) == 2
    assert media[0]["url"] != media[1]["url"]


def test_force_missing_file_is_noop(tmp_path: Path) -> None:
    """Best-effort posture survives ``force``: a path that doesn't exist
    passes through verbatim (never raises)."""
    media: list[dict[str, str]] = []
    raw = json.dumps({"path": str(tmp_path / "missing.zip")})
    assert _register_tool_media(raw, media, force=True, registered={}) == raw
    assert media == []


# ─── send_attachment dispatch result ──────────────────────────────────


class _FakeProvider:
    def __init__(self, chunks: list[ProviderChunk] | None = None) -> None:
        self._chunks = chunks or []

    async def chat_stream(self, **_: Any) -> AsyncIterator[ProviderChunk]:
        for c in self._chunks:
            yield c


def _send_attachment_event(args: dict[str, Any]) -> ToolCallEvent:
    return ToolCallEvent(
        call_id="c1",
        plugin="x",
        tool=SEND_ATTACHMENT_TOOL,
        args_json=json.dumps(args).encode("utf-8"),
    )


@pytest.mark.asyncio
async def test_send_attachment_result_includes_path_and_filename() -> None:
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    start = ChatStart(model="m", messages=[], tools=[], session_key="s::1")

    result = await servicer._dispatch_builtin(
        _send_attachment_event(
            {"path": "/tmp/out/report.pdf", "caption": "here"}
        ),
        start,
        _FakeProvider([]),
    )
    payload = json.loads(result)
    assert payload["ok"] is True
    assert payload["deferred_to_channel"] is True
    assert payload["path"] == "/tmp/out/report.pdf"
    assert payload["filename"] == "report.pdf"  # basename default
    assert payload["caption"] == "here"


@pytest.mark.asyncio
async def test_send_attachment_explicit_filename_wins() -> None:
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    start = ChatStart(model="m", messages=[], tools=[], session_key="s::1")

    result = await servicer._dispatch_builtin(
        _send_attachment_event(
            {"path": "/tmp/out/report.pdf", "filename": "Q2 report.pdf"}
        ),
        start,
        _FakeProvider([]),
    )
    payload = json.loads(result)
    assert payload["filename"] == "Q2 report.pdf"
    assert payload["caption"] == ""


@pytest.mark.asyncio
async def test_send_attachment_missing_path_still_errors() -> None:
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider([]))
    start = ChatStart(model="m", messages=[], tools=[], session_key="s::1")

    result = await servicer._dispatch_builtin(
        _send_attachment_event({}), start, _FakeProvider([])
    )
    payload = json.loads(result)
    assert payload["ok"] is False
    assert "path" in payload["error"]
