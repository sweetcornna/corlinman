"""Reasoning-summary body rerouting — offline, monkeypatching ``AsyncOpenAI``.

Some Responses→chat.completions shims (observed: gpt-5.x behind
OpenAI-compatible relays) split each reasoning-summary part across two
fields: the bold headline arrives as a single ``delta.reasoning_content``
chunk (``**…**``) while the part's body streams as plain ``delta.content``.
Without rerouting, the planning prose renders as the assistant's answer.

The heuristic under test (openai_provider stream loop): after a
headline-only reasoning chunk, buffer content; tool calls (or
``finish=tool_calls``) flush the buffer into the reasoning block, a plain
stop (or outgrowing the summary-size cap) flushes it back as content.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_providers import OpenAIProvider, ProviderChunk


def _text(text: str) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=text, tool_calls=None),
                finish_reason=None,
            )
        ]
    )


def _reasoning(text: str) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=None, tool_calls=None, reasoning_content=text
                ),
                finish_reason=None,
            )
        ]
    )


def _tool(index: int = 0) -> Any:
    td = SimpleNamespace(
        index=index,
        id=f"call_{index}",
        function=SimpleNamespace(name="read_file", arguments='{"path":"."}'),
    )
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=None, tool_calls=[td]),
                finish_reason=None,
            )
        ]
    )


def _finish(reason: str) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=None, tool_calls=None),
                finish_reason=reason,
            )
        ]
    )


class _FakeAsyncIter:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def __aiter__(self) -> AsyncIterator[Any]:
        items = self._items

        async def _gen() -> AsyncIterator[Any]:
            for it in items:
                yield it

        return _gen()


class _FakeOpenAI:
    def __init__(self, chunks: list[Any]) -> None:
        completions = SimpleNamespace(
            create=self._make_create(chunks),
        )
        self.chat = SimpleNamespace(completions=completions)

    @staticmethod
    def _make_create(chunks: list[Any]) -> Any:
        async def _create(**_: Any) -> _FakeAsyncIter:
            return _FakeAsyncIter(chunks)

        return _create


def _patch_openai(monkeypatch: pytest.MonkeyPatch, chunks: list[Any]) -> None:
    import openai  # type: ignore[import-not-found]

    monkeypatch.setattr(openai, "AsyncOpenAI", lambda **_: _FakeOpenAI(chunks))


async def _run(monkeypatch: pytest.MonkeyPatch, chunks: list[Any]) -> list[ProviderChunk]:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    _patch_openai(monkeypatch, chunks)
    prov = OpenAIProvider()
    out: list[ProviderChunk] = []
    async for c in prov.chat_stream(
        model="gpt-4o-mini", messages=[{"role": "user", "content": "x"}]
    ):
        out.append(c)
    return out


def _reasoning_text(chunks: list[ProviderChunk]) -> str:
    return "".join(c.text or "" for c in chunks if c.kind == "token" and c.is_reasoning)


def _content_text(chunks: list[ProviderChunk]) -> str:
    return "".join(
        c.text or "" for c in chunks if c.kind == "token" and not c.is_reasoning
    )


@pytest.mark.asyncio
async def test_summary_body_before_tools_reroutes_to_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gpt-5.6 relay shape: headline via reasoning_content, body via
    content, then tool calls — the body must land in the reasoning block."""
    out = await _run(
        monkeypatch,
        [
            _reasoning("**Listing initial inspection tasks**"),
            _text("I'll trace the repository"),
            _text(" from its entry points."),
            _tool(0),
            _finish("tool_calls"),
        ],
    )
    assert _content_text(out) == ""
    reasoning = _reasoning_text(out)
    assert reasoning.startswith("**Listing initial inspection tasks**")
    assert "\n\nI'll trace the repository from its entry points." in reasoning
    # tool call still streams normally
    assert [c.kind for c in out if c.kind.startswith("tool_call")] == [
        "tool_call_start",
        "tool_call_delta",
        "tool_call_end",
    ]


@pytest.mark.asyncio
async def test_summary_body_flushes_before_finish_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Body buffered with no tool DELTA still routes to reasoning when the
    step ends with finish_reason=tool_calls."""
    out = await _run(
        monkeypatch,
        [
            _reasoning("**Planning**"),
            _text("Check the manifest first."),
            _finish("tool_calls"),
        ],
    )
    assert _content_text(out) == ""
    assert "Check the manifest first." in _reasoning_text(out)


@pytest.mark.asyncio
async def test_plain_stop_keeps_buffered_text_as_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """headline → text → stop: the text was the real answer, not summary."""
    out = await _run(
        monkeypatch,
        [
            _reasoning("**Weighing the options**"),
            _text("The answer is 42."),
            _finish("stop"),
        ],
    )
    assert _content_text(out) == "The answer is 42."
    assert _reasoning_text(out) == "**Weighing the options**"


@pytest.mark.asyncio
async def test_r1_style_reasoning_then_answer_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DeepSeek-R1 style multi-chunk reasoning prose never triggers the
    heuristic — the answer streams live as content."""
    out = await _run(
        monkeypatch,
        [
            _reasoning("Let me think about"),
            _reasoning(" this problem step by step."),
            _text("The answer"),
            _text(" is 42."),
            _finish("stop"),
        ],
    )
    assert _content_text(out) == "The answer is 42."
    assert "this problem step by step." in _reasoning_text(out)


@pytest.mark.asyncio
async def test_oversized_buffer_flushes_back_to_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A buffer outgrowing the summary cap is treated as the real answer."""
    big = "x" * 1500
    out = await _run(
        monkeypatch,
        [
            _reasoning("**Header**"),
            _text(big),
            _text(big),
            _text(big),
            _text("tail"),
            _finish("stop"),
        ],
    )
    content = _content_text(out)
    assert content == big * 3 + "tail"
    assert _reasoning_text(out) == "**Header**"


@pytest.mark.asyncio
async def test_multiple_summary_parts_join_with_separators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = await _run(
        monkeypatch,
        [
            _reasoning("**Part one**"),
            _text("First body."),
            _reasoning("**Part two**"),
            _text("Second body."),
            _tool(0),
            _finish("tool_calls"),
        ],
    )
    assert _content_text(out) == ""
    assert _reasoning_text(out) == (
        "**Part one**\n\nFirst body.\n\n**Part two**\n\nSecond body."
    )
