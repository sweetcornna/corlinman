"""Google provider stream tests — offline, monkeypatching ``google.genai``."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from corlinman_providers import GoogleProvider, ProviderChunk


class _FakeAsyncIter:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def __aiter__(self) -> AsyncIterator[Any]:
        items = self._items

        async def _gen() -> AsyncIterator[Any]:
            for item in items:
                yield item

        return _gen()


class _FakeModels:
    def __init__(self, chunks: list[Any], calls: list[dict[str, Any]]) -> None:
        self._chunks = chunks
        self._calls = calls

    async def generate_content_stream(self, **kwargs: Any) -> _FakeAsyncIter:
        self._calls.append(kwargs)
        return _FakeAsyncIter(self._chunks)


class _FakeClient:
    def __init__(self, chunks: list[Any], calls: list[dict[str, Any]]) -> None:
        self.aio = SimpleNamespace(models=_FakeModels(chunks, calls))


def _patch_google(monkeypatch: pytest.MonkeyPatch, chunks: list[Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    # Reuse the real ``google.genai.types`` module so the structured
    # multimodal Part/Content construction is exercised against the actual
    # SDK types; only the network-touching ``Client`` is replaced.
    from google.genai import types as real_types

    google_mod = ModuleType("google")
    genai_mod = ModuleType("google.genai")
    genai_mod.Client = lambda **_: _FakeClient(chunks, calls)  # type: ignore[attr-defined]
    genai_mod.types = real_types  # type: ignore[attr-defined]
    google_mod.genai = genai_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.genai", genai_mod)
    monkeypatch.setitem(sys.modules, "google.genai.types", real_types)
    return calls


def _text_chunk(text: str) -> Any:
    return SimpleNamespace(text=text)


def _function_call_chunk(*, call_id: str | None, name: str, args: dict[str, Any]) -> Any:
    function_call = SimpleNamespace(id=call_id, name=name, args=args)
    return SimpleNamespace(
        text=None,
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(parts=[SimpleNamespace(function_call=function_call)])
            )
        ],
    )


@pytest.mark.asyncio
async def test_chat_stream_yields_text_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_google(monkeypatch, [_text_chunk("hello "), _text_chunk("world")])

    prov = GoogleProvider(api_key="test-key")
    chunks: list[ProviderChunk] = []
    async for chunk in prov.chat_stream(
        model="gemini-2.0-flash",
        messages=[{"role": "user", "content": "hi"}],
    ):
        chunks.append(chunk)

    assert [c.text for c in chunks if c.kind == "token"] == ["hello ", "world"]
    assert chunks[-1].kind == "done"
    assert chunks[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_chat_stream_emits_tool_call_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_google(
        monkeypatch,
        [
            _function_call_chunk(
                call_id="call_weather",
                name="get_weather",
                args={"city": "Shanghai", "units": "celsius"},
            )
        ],
    )

    prov = GoogleProvider(api_key="test-key")
    chunks: list[ProviderChunk] = []
    async for chunk in prov.chat_stream(
        model="gemini-2.0-flash",
        messages=[{"role": "user", "content": "weather?"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    ):
        chunks.append(chunk)

    assert [c.kind for c in chunks] == [
        "tool_call_start",
        "tool_call_delta",
        "tool_call_end",
        "done",
    ]
    assert chunks[0].tool_call_id == "call_weather"
    assert chunks[0].tool_name == "get_weather"
    assert json.loads(chunks[1].arguments_delta or "") == {
        "city": "Shanghai",
        "units": "celsius",
    }
    assert chunks[2].tool_call_id == "call_weather"
    assert chunks[-1].finish_reason == "tool_calls"
    assert calls[0]["config"]["tools"] == [
        {
            "function_declarations": [
                {
                    "name": "get_weather",
                    "parameters": {"type": "object", "properties": {}},
                }
            ]
        }
    ]


@pytest.mark.asyncio
async def test_chat_stream_content_parts_become_structured_multimodal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAI content-parts (text + image_url) must reach Gemini as real
    structured parts, never a repr-flattened Python list inside a string.

    Reproduces the multimodal-garbling bug: ``_inject_attachments`` produces
    a content-parts ``list`` and the old prompt builder embedded its repr
    into ``f"{role}: {content}"``, so the image was never sent as a part.
    """
    calls = _patch_google(monkeypatch, [_text_chunk("ok")])

    data_url = "data:image/png;base64,aGVsbG8="  # b"hello"
    prov = GoogleProvider(api_key="test-key")
    async for _ in prov.chat_stream(
        model="gemini-2.0-flash",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "describe this"},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    ):
        pass

    contents = calls[0]["contents"]

    # Regression guard: the Python-list repr must never leak into a flat
    # string handed to the SDK.
    flat = repr(contents)
    assert "'type': 'image_url'" not in flat
    assert "'type': 'text'" not in flat

    # The request must carry one structured user turn whose parts are a real
    # text part + a real inline image part (bytes decoded from the data URL).
    assert len(contents) == 1
    turn = contents[0]
    assert turn.role == "user"
    assert len(turn.parts) == 2
    text_part, image_part = turn.parts
    assert text_part.text == "describe this"
    assert text_part.inline_data is None
    assert image_part.text is None
    assert image_part.inline_data is not None
    assert image_part.inline_data.mime_type == "image/png"
    assert image_part.inline_data.data == b"hello"


@pytest.mark.asyncio
async def test_chat_stream_string_history_maps_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plain-string multi-turn history maps to structured Content turns with
    correct user/model roles (no flat ``role: content`` concatenation)."""
    calls = _patch_google(monkeypatch, [_text_chunk("ok")])

    prov = GoogleProvider(api_key="test-key")
    async for _ in prov.chat_stream(
        model="gemini-2.0-flash",
        messages=[
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "again"},
        ],
    ):
        pass

    contents = calls[0]["contents"]
    assert [turn.role for turn in contents] == ["user", "model", "user"]
    assert [turn.parts[0].text for turn in contents] == ["hi", "hello", "again"]


@pytest.mark.asyncio
async def test_chat_stream_synthesises_unique_tool_call_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_google(
        monkeypatch,
        [
            _function_call_chunk(call_id=None, name="first_tool", args={}),
            _function_call_chunk(call_id=None, name="second_tool", args={}),
        ],
    )

    prov = GoogleProvider(api_key="test-key")
    chunks: list[ProviderChunk] = []
    async for chunk in prov.chat_stream(
        model="gemini-2.0-flash",
        messages=[{"role": "user", "content": "call tools"}],
        tools=[
            {"type": "function", "function": {"name": "first_tool"}},
            {"type": "function", "function": {"name": "second_tool"}},
        ],
    ):
        chunks.append(chunk)

    starts = [chunk for chunk in chunks if chunk.kind == "tool_call_start"]
    ends = [chunk for chunk in chunks if chunk.kind == "tool_call_end"]
    assert [chunk.tool_call_id for chunk in starts] == ["call_0", "call_1"]
    assert [chunk.tool_call_id for chunk in ends] == ["call_0", "call_1"]
