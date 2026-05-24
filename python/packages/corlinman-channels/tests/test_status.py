"""Tests for ``corlinman_channels._status`` — shared mutable-spinner
helpers extracted from ``service.py``.

The high-level state machine is exercised end-to-end through the
``handle_one_*`` tests in :mod:`tests.test_service`; this file pins
the pure helpers (renderers, truncation, parse) and the
:class:`MutableSpinner` contract directly so a regression in either
layer surfaces immediately.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest
from corlinman_channels._status import (
    REASONING_PREVIEW_CHARS,
    SEND_ATTACHMENT_TOOL,
    STATUS_GENERATING,
    STATUS_REASONING_PREFIX,
    STATUS_THINKING,
    TEXT_LIMIT,
    TRUNCATION_MARKER,
    MutableSpinner,
    format_tool_result,
    format_tool_status,
    parse_send_attachment_args,
    tool_arg_preview,
    truncate_reply,
)


@dataclass(slots=True)
class _Ev:
    """Lightweight fake event matching what the helpers read off it."""

    plugin: str = ""
    tool: str = ""
    args_json: bytes = b""
    duration_ms: int = 0
    is_error: bool = False
    error_summary: str = ""


# ---------------------------------------------------------------------------
# Constants — spec requires the exact emoji + label strings.
# ---------------------------------------------------------------------------


class TestConstants:
    def test_status_thinking_emoji(self) -> None:
        assert STATUS_THINKING == "🧠 思考中..."

    def test_status_generating_emoji(self) -> None:
        assert STATUS_GENERATING == "✍️ 生成回复中..."

    def test_reasoning_prefix(self) -> None:
        assert STATUS_REASONING_PREFIX == "💭 推理: "

    def test_reasoning_preview_chars(self) -> None:
        assert REASONING_PREVIEW_CHARS == 80

    def test_text_limit(self) -> None:
        assert TEXT_LIMIT == 4000

    def test_truncation_marker(self) -> None:
        assert TRUNCATION_MARKER == "\n\n[...回复过长,已截断]"

    def test_send_attachment_tool_name(self) -> None:
        assert SEND_ATTACHMENT_TOOL == "send_attachment"


# ---------------------------------------------------------------------------
# truncate_reply — explicit boundary tests so the off-by-one cases are
# documented.
# ---------------------------------------------------------------------------


class TestTruncateReply:
    def test_short_body_passes_through(self) -> None:
        assert truncate_reply("hello") == "hello"

    def test_exact_limit_passes_through(self) -> None:
        body = "x" * TEXT_LIMIT
        assert truncate_reply(body) == body

    def test_over_limit_appends_marker(self) -> None:
        body = "x" * (TEXT_LIMIT + 100)
        out = truncate_reply(body)
        assert out.endswith(TRUNCATION_MARKER)
        assert len(out) <= TEXT_LIMIT

    def test_custom_limit(self) -> None:
        body = "x" * 2500
        out = truncate_reply(body, limit=2000)
        assert out.endswith(TRUNCATION_MARKER)
        assert len(out) <= 2000


# ---------------------------------------------------------------------------
# tool_arg_preview / format_tool_status / format_tool_result — pure
# renderers shared across all four mutable-spinner channels.
# ---------------------------------------------------------------------------


class TestToolArgPreview:
    def test_web_search_query(self) -> None:
        out = tool_arg_preview("web_search", b'{"query":"hi there"}')
        assert "hi there" in out

    def test_read_file_path(self) -> None:
        assert tool_arg_preview("read_file", b'{"path":"/tmp/x"}') == "/tmp/x"

    def test_run_shell_command(self) -> None:
        assert tool_arg_preview("run_shell", b'{"command":"ls -la"}') == "ls -la"

    def test_send_attachment_short_path(self) -> None:
        assert (
            tool_arg_preview("send_attachment", b'{"path":"/tmp/a.pdf"}')
            == "a.pdf"
        )

    def test_unknown_tool_empty(self) -> None:
        assert tool_arg_preview("nope", b'{"x":1}') == ""

    def test_malformed_json_empty(self) -> None:
        assert tool_arg_preview("read_file", b"not json") == ""


class TestFormatToolStatus:
    def test_with_preview(self) -> None:
        out = format_tool_status(
            _Ev(plugin="web_search", tool="web_search", args_json=b'{"query":"x"}')
        )
        assert out.startswith("🔧 web_search")
        assert "x" in out

    def test_no_preview(self) -> None:
        out = format_tool_status(_Ev(plugin="builtin", tool="finish"))
        # Falls back to the 调用工具 label when no preview comes through.
        assert "🔧 调用工具" in out


class TestFormatToolResult:
    def test_success_ms(self) -> None:
        out = format_tool_result(
            _Ev(tool="web_search", duration_ms=42, is_error=False)
        )
        assert out == "✅ web_search (42ms)"

    def test_success_seconds(self) -> None:
        out = format_tool_result(
            _Ev(tool="web_search", duration_ms=1500, is_error=False)
        )
        assert out == "✅ web_search (1.5s)"

    def test_error_with_summary(self) -> None:
        out = format_tool_result(
            _Ev(
                tool="run_shell",
                duration_ms=200,
                is_error=True,
                error_summary="boom",
            )
        )
        assert "❌" in out
        assert "200ms" in out
        assert "boom" in out


class TestParseSendAttachmentArgs:
    def test_full(self) -> None:
        args = json.dumps({"path": "/tmp/x", "caption": "hi", "filename": "x.pdf"})
        path, cap, name = parse_send_attachment_args(_Ev(args_json=args.encode()))
        assert path == "/tmp/x"
        assert cap == "hi"
        assert name == "x.pdf"

    def test_path_only(self) -> None:
        args = json.dumps({"path": "/tmp/x"})
        path, cap, name = parse_send_attachment_args(_Ev(args_json=args.encode()))
        assert path == "/tmp/x"
        assert cap is None
        assert name is None

    def test_malformed_returns_empty(self) -> None:
        path, cap, name = parse_send_attachment_args(_Ev(args_json=b"not json"))
        assert path == ""
        assert cap is None
        assert name is None

    def test_missing_path(self) -> None:
        path, cap, name = parse_send_attachment_args(_Ev(args_json=b"{}"))
        assert path == ""


# ---------------------------------------------------------------------------
# MutableSpinner — the per-turn state machine. Exercises the contract
# without going through a full handle_one_*.
# ---------------------------------------------------------------------------


class TestMutableSpinner:
    @pytest.mark.asyncio
    async def test_token_delta_switches_to_generating(self) -> None:
        edits: list[str] = []

        async def edit(text: str) -> None:
            edits.append(text)

        spinner = MutableSpinner(edit)
        await spinner.on_token_delta("hello", is_reasoning=False)
        assert edits == [STATUS_GENERATING]
        assert spinner.text_parts == ["hello"]
        assert spinner.last_status == STATUS_GENERATING

    @pytest.mark.asyncio
    async def test_repeated_token_delta_does_not_re_edit(self) -> None:
        """Once the spinner is on STATUS_GENERATING, more token deltas
        must NOT re-edit (the visible text didn't change)."""
        edits: list[str] = []

        async def edit(text: str) -> None:
            edits.append(text)

        spinner = MutableSpinner(edit)
        await spinner.on_token_delta("a", is_reasoning=False)
        await spinner.on_token_delta("b", is_reasoning=False)
        await spinner.on_token_delta("c", is_reasoning=False)
        assert edits == [STATUS_GENERATING]
        assert "".join(spinner.text_parts) == "abc"

    @pytest.mark.asyncio
    async def test_reasoning_delta_renders_thinking_line(self) -> None:
        edits: list[str] = []

        async def edit(text: str) -> None:
            edits.append(text)

        spinner = MutableSpinner(edit)
        await spinner.on_token_delta("thinking...", is_reasoning=True)
        assert edits == [f"{STATUS_REASONING_PREFIX}thinking..."]
        # Reasoning text MUST NOT accumulate into the final reply buffer.
        assert spinner.text_parts == []

    @pytest.mark.asyncio
    async def test_reasoning_delta_long_text_truncates(self) -> None:
        edits: list[str] = []

        async def edit(text: str) -> None:
            edits.append(text)

        spinner = MutableSpinner(edit)
        big = "x" * 200
        await spinner.on_token_delta(big, is_reasoning=True)
        # Just the prefix + ≤80 + "…"
        assert edits[0].startswith(STATUS_REASONING_PREFIX)
        assert "…" in edits[0]

    @pytest.mark.asyncio
    async def test_reasoning_delta_empty_skips(self) -> None:
        edits: list[str] = []

        async def edit(text: str) -> None:
            edits.append(text)

        spinner = MutableSpinner(edit)
        await spinner.on_token_delta("   \n   ", is_reasoning=True)
        # Whitespace-only reasoning must not even trigger an edit.
        assert edits == []

    @pytest.mark.asyncio
    async def test_tool_call_renders_status(self) -> None:
        edits: list[str] = []

        async def edit(text: str) -> None:
            edits.append(text)

        spinner = MutableSpinner(edit)
        result = await spinner.on_tool_call(
            _Ev(plugin="web_search", tool="web_search", args_json=b'{"query":"x"}')
        )
        assert result is None  # not the send_attachment intercept
        assert len(edits) == 1
        assert "web_search" in edits[0]

    @pytest.mark.asyncio
    async def test_tool_call_send_attachment_invokes_handler(self) -> None:
        edits: list[str] = []
        handler_calls: list[Any] = []

        async def edit(text: str) -> None:
            edits.append(text)

        async def handler(ev: Any) -> str:
            handler_calls.append(ev)
            return "📎 已发送文件: a.pdf"

        spinner = MutableSpinner(edit, send_attachment_handler=handler)
        result = await spinner.on_tool_call(
            _Ev(plugin="send_attachment", tool="send_attachment",
                args_json=b'{"path":"/tmp/a.pdf"}')
        )
        assert result == "intercept"
        assert len(handler_calls) == 1
        assert edits == ["📎 已发送文件: a.pdf"]

    @pytest.mark.asyncio
    async def test_tool_call_send_attachment_without_handler_renders_default(
        self,
    ) -> None:
        """When no handler is registered, send_attachment renders like
        any other tool — the channel doesn't support file uploads."""
        edits: list[str] = []

        async def edit(text: str) -> None:
            edits.append(text)

        spinner = MutableSpinner(edit)
        result = await spinner.on_tool_call(
            _Ev(plugin="send_attachment", tool="send_attachment",
                args_json=b'{"path":"/tmp/a.pdf"}')
        )
        assert result is None  # not intercepted
        # Edit ran as the standard 🔧 spinner line.
        assert any("send_attachment" in t for t in edits)

    @pytest.mark.asyncio
    async def test_tool_result_renders_completion(self) -> None:
        edits: list[str] = []

        async def edit(text: str) -> None:
            edits.append(text)

        spinner = MutableSpinner(edit)
        await spinner.on_tool_result(
            _Ev(tool="web_search", duration_ms=200, is_error=False)
        )
        assert len(edits) == 1
        assert "✅" in edits[0]
        assert "200ms" in edits[0]

    @pytest.mark.asyncio
    async def test_tool_result_send_attachment_is_suppressed(self) -> None:
        """send_attachment completion must NOT overwrite the 📎 status
        the tool_call intercept already rendered."""
        edits: list[str] = []

        async def edit(text: str) -> None:
            edits.append(text)

        spinner = MutableSpinner(edit)
        await spinner.on_tool_result(
            _Ev(tool="send_attachment", duration_ms=200, is_error=False)
        )
        # No edit fired.
        assert edits == []

    @pytest.mark.asyncio
    async def test_dedup_identical_status(self) -> None:
        """An edit with the same text as last_status must not refire —
        Telegram's editMessageText rejects unchanged content."""
        edits: list[str] = []

        async def edit(text: str) -> None:
            edits.append(text)

        spinner = MutableSpinner(edit)
        await spinner.on_tool_call(_Ev(tool="finish"))
        first_len = len(edits)
        # Same event again — no new edit.
        await spinner.on_tool_call(_Ev(tool="finish"))
        assert len(edits) == first_len
