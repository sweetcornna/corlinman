"""Anthropic provider unit tests — all offline, no network.

Strategy: monkeypatch ``anthropic.AsyncAnthropic`` with a minimal fake that
emulates the ``messages.stream()`` async context manager and its raw-event
stream. Keeps the provider behaviour under test while dodging the vendor
SDK's heavy HTTP transport.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_providers import AnthropicProvider, ProviderChunk
from corlinman_providers.anthropic_provider import _map_stop_reason, _split_system
from corlinman_providers.registry import ProviderRegistry, resolve


class _FakeStream:
    """Fake ``messages.stream()`` async context manager."""

    def __init__(self, events: list[Any], stop_reason: str = "end_turn") -> None:
        self._events = events
        self._stop_reason = stop_reason

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    def __aiter__(self) -> AsyncIterator[Any]:
        events = self._events

        async def _gen() -> AsyncIterator[Any]:
            for e in events:
                yield e

        return _gen()

    async def get_final_message(self) -> Any:
        return SimpleNamespace(stop_reason=self._stop_reason)


class _FakeMessages:
    def __init__(self, stream: _FakeStream) -> None:
        self._stream = stream

    def stream(self, **_: Any) -> _FakeStream:
        return self._stream


class _FakeClient:
    def __init__(self, events: list[Any], stop_reason: str = "end_turn") -> None:
        self.messages = _FakeMessages(_FakeStream(events, stop_reason))


def _patch_anthropic(monkeypatch: pytest.MonkeyPatch, fake_client: _FakeClient) -> None:
    import anthropic  # type: ignore[import-not-found]

    monkeypatch.setattr(anthropic, "AsyncAnthropic", lambda **_: fake_client)


def _text_event(text: str) -> Any:
    return SimpleNamespace(
        type="content_block_delta",
        index=0,
        delta=SimpleNamespace(type="text_delta", text=text),
    )


def _tool_start_event(index: int, tool_id: str, name: str) -> Any:
    return SimpleNamespace(
        type="content_block_start",
        index=index,
        content_block=SimpleNamespace(type="tool_use", id=tool_id, name=name),
    )


def _tool_delta_event(index: int, partial: str) -> Any:
    return SimpleNamespace(
        type="content_block_delta",
        index=index,
        delta=SimpleNamespace(type="input_json_delta", partial_json=partial),
    )


def _block_stop_event(index: int) -> Any:
    return SimpleNamespace(type="content_block_stop", index=index)


@pytest.mark.asyncio
async def test_no_api_key_raises_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    prov = AnthropicProvider()
    with pytest.raises(RuntimeError, match="API key missing"):
        async for _ in prov.chat_stream(model="claude-sonnet-4-5", messages=[]):
            pass


@pytest.mark.asyncio
async def test_supports_claude_prefix() -> None:
    assert AnthropicProvider.supports("claude-sonnet-4-5")
    assert AnthropicProvider.supports("claude-3-opus")
    assert not AnthropicProvider.supports("gpt-4o")


@pytest.mark.asyncio
async def test_chat_stream_yields_text_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    events = [_text_event("hello "), _text_event("world"), _block_stop_event(0)]
    fake = _FakeClient(events)
    _patch_anthropic(monkeypatch, fake)

    prov = AnthropicProvider()
    chunks: list[ProviderChunk] = []
    async for chunk in prov.chat_stream(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "hi"}],
    ):
        chunks.append(chunk)

    texts = [c.text for c in chunks if c.kind == "token"]
    assert texts == ["hello ", "world"]
    assert chunks[-1].kind == "done"
    assert chunks[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_chat_stream_maps_max_tokens_to_length(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    fake = _FakeClient([_text_event("partial")], stop_reason="max_tokens")
    _patch_anthropic(monkeypatch, fake)

    prov = AnthropicProvider()
    finish: str | None = None
    async for chunk in prov.chat_stream(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "hi"}],
    ):
        if chunk.kind == "done":
            finish = chunk.finish_reason
    assert finish == "length"


@pytest.mark.asyncio
async def test_chat_stream_emits_tool_call_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    """``tool_use`` content blocks translate into tool_call_{start,delta,end}."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    events = [
        _tool_start_event(0, "call_abc", "FooPlugin"),
        _tool_delta_event(0, '{"q":'),
        _tool_delta_event(0, '"hi"}'),
        _block_stop_event(0),
    ]
    fake = _FakeClient(events, stop_reason="tool_use")
    _patch_anthropic(monkeypatch, fake)

    prov = AnthropicProvider()
    chunks: list[ProviderChunk] = []
    async for chunk in prov.chat_stream(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "go"}],
    ):
        chunks.append(chunk)

    kinds = [c.kind for c in chunks]
    assert kinds == ["tool_call_start", "tool_call_delta", "tool_call_delta", "tool_call_end", "done"]
    assert chunks[0].tool_call_id == "call_abc"
    assert chunks[0].tool_name == "FooPlugin"
    assert chunks[1].arguments_delta == '{"q":'
    assert chunks[2].arguments_delta == '"hi"}'
    assert chunks[3].tool_call_id == "call_abc"
    assert chunks[-1].finish_reason == "tool_calls"


def test_split_system_extracts_system_and_keeps_order() -> None:
    system, chat = _split_system(
        [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "system", "content": "also terse"},
        ]
    )
    assert system == "you are helpful\n\nalso terse"
    assert [m["role"] for m in chat] == ["user", "assistant"]
    assert chat[0]["content"] == "hi"


def test_map_stop_reason_defaults_to_stop() -> None:
    assert _map_stop_reason(None) == "stop"
    assert _map_stop_reason("unknown_reason") == "stop"
    assert _map_stop_reason("tool_use") == "tool_calls"


def test_registry_resolves_claude_prefix() -> None:
    # Feature C: resolve() returns a (provider, upstream_model, params) triple.
    reg = ProviderRegistry()
    provider, model, params = reg.resolve("claude-sonnet-4-5")
    assert provider.__class__.__name__ == "AnthropicProvider"
    assert model == "claude-sonnet-4-5"
    assert params == {}


def test_registry_raises_for_unknown() -> None:
    with pytest.raises(KeyError):
        resolve("mystery-llm-9")


def test_split_system_translates_image_url_part_to_anthropic_block() -> None:
    """OpenAI-shape ``image_url`` content part becomes Anthropic's
    ``{"type": "image", "source": {"type": "url", ...}}`` block."""
    _, chat = _split_system(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "look at this"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://cdn/pic.png"},
                    },
                ],
            }
        ]
    )
    assert len(chat) == 1
    content = chat[0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "look at this"}
    assert content[1] == {
        "type": "image",
        "source": {"type": "url", "url": "https://cdn/pic.png"},
    }


def test_split_system_translates_data_url_to_base64_block() -> None:
    """``data:image/png;base64,...`` URI decodes into Anthropic's base64 source."""
    _, chat = _split_system(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/png;base64,iVBORw0KGgo="
                        },
                    },
                ],
            }
        ]
    )
    content = chat[0]["content"]
    assert content[0] == {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": "iVBORw0KGgo=",
        },
    }


def test_split_system_drops_unsupported_file_part() -> None:
    """``file`` part (audio/video) is skipped with a warn — text survives."""
    _, chat = _split_system(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "transcript:"},
                    {
                        "type": "file",
                        "file": {"kind": "audio", "url": "https://x/a.amr"},
                    },
                ],
            }
        ]
    )
    content = chat[0]["content"]
    assert content == [{"type": "text", "text": "transcript:"}]


def test_split_system_translates_tool_calls_to_anthropic_blocks() -> None:
    """An assistant turn carrying OpenAI-shape ``tool_calls`` becomes a
    ``tool_use`` content block, and the following ``role="tool"`` message
    becomes a user turn with a ``tool_result`` block (audit B1).

    Before the fix the assistant turn collapsed to
    ``{"role":"assistant","content":""}`` (tool_calls dropped) and the
    tool result became a bare ``{"role":"user","content":"res"}`` — which
    Anthropic rejects (empty assistant content + orphan tool_result),
    breaking every multi-round tool call.
    """
    _, chat = _split_system(
        [
            {"role": "system", "content": "be helpful"},
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "f", "arguments": '{"x":1}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "res"},
        ]
    )
    assert [m["role"] for m in chat] == ["user", "assistant", "user"]

    # Assistant turn → tool_use block (no empty-string content).
    assistant_content = chat[1]["content"]
    assert isinstance(assistant_content, list)
    tool_use = [b for b in assistant_content if b.get("type") == "tool_use"]
    assert tool_use == [
        {"type": "tool_use", "id": "call_1", "name": "f", "input": {"x": 1}}
    ]

    # Tool result → user turn with a tool_result block.
    result_content = chat[2]["content"]
    assert result_content == [
        {"type": "tool_result", "tool_use_id": "call_1", "content": "res"}
    ]


def test_split_system_tool_call_with_text_keeps_both_blocks() -> None:
    """An assistant turn with both text and tool_calls emits a text block
    *and* a tool_use block."""
    _, chat = _split_system(
        [
            {
                "role": "assistant",
                "content": "let me check",
                "tool_calls": [
                    {
                        "id": "call_9",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    }
                ],
            },
        ]
    )
    content = chat[0]["content"]
    assert content == [
        {"type": "text", "text": "let me check"},
        {"type": "tool_use", "id": "call_9", "name": "lookup", "input": {}},
    ]


def test_split_system_tool_call_malformed_arguments_falls_back_to_empty_input() -> None:
    """Malformed ``arguments`` JSON does not crash — input falls back to {}."""
    _, chat = _split_system(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_bad",
                        "type": "function",
                        "function": {"name": "f", "arguments": "{not json"},
                    }
                ],
            },
        ]
    )
    tool_use = chat[0]["content"]
    assert tool_use == [
        {"type": "tool_use", "id": "call_bad", "name": "f", "input": {}}
    ]


@pytest.mark.asyncio
async def test_chat_stream_tool_round_sends_anthropic_tool_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a multi-round tool exchange reaches the SDK as
    ``tool_use`` / ``tool_result`` blocks, not an empty assistant turn
    (audit B1 live path: feed_tool_result → chat_stream)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    captured: dict[str, Any] = {}

    class _CapturingMessages:
        def stream(self, **kwargs: Any) -> _FakeStream:
            captured.update(kwargs)
            return _FakeStream([_text_event("ok")], stop_reason="end_turn")

    class _CapturingClient:
        def __init__(self) -> None:
            self.messages = _CapturingMessages()

    _patch_anthropic(monkeypatch, _CapturingClient())

    prov = AnthropicProvider()
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "f", "arguments": '{"x":1}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "res"},
    ]
    async for _ in prov.chat_stream(model="claude-sonnet-4-5", messages=messages):
        pass

    sent = captured["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert {"type": "tool_use", "id": "call_1", "name": "f", "input": {"x": 1}} in (
        sent[1]["content"]
    )
    assert sent[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "call_1", "content": "res"}
    ]
    assert sent[1]["content"] != ""  # NOT an empty assistant turn


@pytest.mark.asyncio
async def test_chat_stream_with_image_url_part(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: multipart user content reaches the SDK with Anthropic
    blocks, and the stream still yields token + done chunks."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    captured: dict[str, Any] = {}

    class _CapturingMessages:
        def stream(self, **kwargs: Any) -> _FakeStream:
            captured.update(kwargs)
            return _FakeStream([_text_event("ack")], stop_reason="end_turn")

    class _CapturingClient:
        def __init__(self) -> None:
            self.messages = _CapturingMessages()

    fake = _CapturingClient()
    _patch_anthropic(monkeypatch, fake)

    prov = AnthropicProvider()
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe"},
                {
                    "type": "image_url",
                    "image_url": {"url": "https://cdn/pic.png"},
                },
            ],
        }
    ]
    tokens: list[str] = []
    async for chunk in prov.chat_stream(
        model="claude-sonnet-4-5", messages=messages
    ):
        if chunk.kind == "token":
            tokens.append(chunk.text)

    assert tokens == ["ack"]
    sent_messages = captured["messages"]
    assert len(sent_messages) == 1
    content = sent_messages[0]["content"]
    assert content[0] == {"type": "text", "text": "describe"}
    assert content[1] == {
        "type": "image",
        "source": {"type": "url", "url": "https://cdn/pic.png"},
    }


# ---------------------------------------------------------------------------
# OAuth credential read caching (audit R4-D4 / PERF-003)
# ---------------------------------------------------------------------------
#
# ``_resolve_oauth_token`` runs once per request on the async hot path.
# Before the fix it called ``load_anthropic_credential`` — a blocking
# ``path.read_text()`` + ``json.loads()`` — on EVERY request. The fix
# memoises the parsed credential keyed on the file's ``(mtime_ns, size)``
# and only re-reads when that key changes (``stat()`` is far cheaper than
# read+parse). These two tests pin the contract: (1) repeated resolves
# against an unchanged file read the file exactly once; (2) rewriting the
# file (new mtime) invalidates the cache and forces a fresh read so a
# token refresh is still picked up.


def _write_oauth_credential(
    data_dir: Path, *, access_token: str, expires_at_ms: int | None
) -> Path:
    """Write a minimal Anthropic OAuth credential JSON under ``data_dir``."""
    path = data_dir / ".oauth" / "anthropic.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "provider": "anthropic",
                "access_token": access_token,
                "refresh_token": None,
                "expires_at_ms": expires_at_ms,
                "scope": None,
                "obtained_at_ms": int(time.time() * 1000),
            }
        ),
        encoding="utf-8",
    )
    return path


def test_oauth_credential_read_once_across_repeated_resolves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Repeated ``_resolve_oauth_token`` calls against an UNCHANGED file
    perform the expensive read+parse exactly once (audit R4-D4)."""
    import corlinman_providers.anthropic_provider as ap_mod

    # Far-future expiry so the refresh-on-use branch never fires (it would
    # otherwise rewrite the file and legitimately invalidate the cache).
    _write_oauth_credential(
        tmp_path, access_token="oauth-tok-1", expires_at_ms=int(time.time() * 1000) + 3_600_000
    )

    real_load = ap_mod.load_anthropic_credential
    calls = {"n": 0}

    def _counting_load(data_dir: Path) -> Any:
        calls["n"] += 1
        return real_load(data_dir)

    monkeypatch.setattr(ap_mod, "load_anthropic_credential", _counting_load)

    prov = AnthropicProvider(data_dir=tmp_path)
    tokens = [prov._resolve_oauth_token() for _ in range(5)]

    assert tokens == ["oauth-tok-1"] * 5
    assert calls["n"] == 1, (
        f"expected the credential file to be read+parsed once across 5 "
        f"resolves, but load_anthropic_credential ran {calls['n']} times"
    )


def test_oauth_credential_reread_when_file_mtime_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Rewriting the credential file (new mtime) invalidates the memo so
    the next resolve re-reads it — keeping token-refresh correct (audit
    R4-D4)."""
    import corlinman_providers.anthropic_provider as ap_mod

    far_future = int(time.time() * 1000) + 3_600_000
    cred_path = _write_oauth_credential(
        tmp_path, access_token="oauth-tok-1", expires_at_ms=far_future
    )

    real_load = ap_mod.load_anthropic_credential
    calls = {"n": 0}

    def _counting_load(data_dir: Path) -> Any:
        calls["n"] += 1
        return real_load(data_dir)

    monkeypatch.setattr(ap_mod, "load_anthropic_credential", _counting_load)

    prov = AnthropicProvider(data_dir=tmp_path)

    # First resolve populates the memo from disk.
    assert prov._resolve_oauth_token() == "oauth-tok-1"
    assert calls["n"] == 1

    # Second resolve, file unchanged → served from memo, no extra read.
    assert prov._resolve_oauth_token() == "oauth-tok-1"
    assert calls["n"] == 1

    # Rewrite the file with a new token and bump the mtime so the
    # (mtime_ns, size) cache key changes. ``os.utime`` guarantees a
    # distinct mtime even on coarse-grained filesystems where two quick
    # writes could otherwise share a timestamp.
    _write_oauth_credential(tmp_path, access_token="oauth-tok-2", expires_at_ms=far_future)
    st = cred_path.stat()
    os.utime(cred_path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

    # Third resolve sees the new mtime → re-reads → returns the new token.
    assert prov._resolve_oauth_token() == "oauth-tok-2"
    assert calls["n"] == 2, (
        "a changed file mtime must invalidate the memo and trigger exactly "
        "one fresh read"
    )
