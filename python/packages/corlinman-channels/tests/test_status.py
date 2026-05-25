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
    ASK_USER_TOOL,
    REASONING_PREVIEW_CHARS,
    SEND_ATTACHMENT_TOOL,
    STATUS_GENERATING,
    STATUS_REASONING_PREFIX,
    STATUS_THINKING,
    TEXT_LIMIT,
    TODO_WRITE_TOOL,
    TRUNCATION_MARKER,
    MutableSpinner,
    format_ask_user,
    format_todo_list,
    format_tool_result,
    format_tool_status,
    parse_ask_user_args,
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


# ---------------------------------------------------------------------------
# format_todo_list — Claude-Code-style checkbox renderer for the
# ``todo_write`` tool call. Lives in the mutable-spinner module so the
# Telegram spinner + the QQ/WeChat summary block share the same shape.
# ---------------------------------------------------------------------------


def _todos_json(items: list[dict[str, str]]) -> bytes:
    """Encode a ``todo_write`` args payload as the dispatcher would."""
    return json.dumps({"todos": items}).encode("utf-8")


class TestFormatTodoList:
    def test_constant_exported(self) -> None:
        """The shared tool-name constant must match the agent-side one
        so the channel handlers wire correctly without a stale literal."""
        assert TODO_WRITE_TOOL == "todo_write"

    def test_renders_checkboxes(self) -> None:
        """Mixed 2-completed / 1-in_progress / 2-pending list must
        render all three checkbox glyphs and the (done/total) header."""
        args = _todos_json([
            {"content": "Search market data",
             "activeForm": "Searching market data",
             "status": "completed"},
            {"content": "Collate vendor list",
             "activeForm": "Collating vendor list",
             "status": "completed"},
            {"content": "Draft decision memo",
             "activeForm": "Drafting decision memo",
             "status": "in_progress"},
            {"content": "Build chart",
             "activeForm": "Building chart",
             "status": "pending"},
            {"content": "Send final files",
             "activeForm": "Sending final files",
             "status": "pending"},
        ])
        out = format_todo_list(args)
        assert "📋 任务清单 (2/5):" in out
        assert "☑" in out
        assert "▣" in out
        assert "☐" in out

    def test_uses_active_form_for_in_progress(self) -> None:
        """The in_progress row prefers ``activeForm`` (present-continuous)
        over ``content`` so the spinner reads naturally — "Drafting…"
        instead of "Draft…"."""
        args = _todos_json([
            {"content": "Draft decision memo",
             "activeForm": "Drafting decision memo",
             "status": "in_progress"},
        ])
        out = format_todo_list(args)
        assert "▣ Drafting decision memo" in out
        # The imperative form must NOT appear on the in_progress row.
        assert "Draft decision memo" not in out

    def test_uses_content_for_non_active_rows(self) -> None:
        """completed + pending rows render ``content`` (imperative)
        even when ``activeForm`` is supplied."""
        args = _todos_json([
            {"content": "Search market data",
             "activeForm": "Searching market data",
             "status": "completed"},
            {"content": "Build chart",
             "activeForm": "Building chart",
             "status": "pending"},
        ])
        out = format_todo_list(args)
        assert "☑ Search market data" in out
        assert "☐ Build chart" in out
        # Active form must not bleed into non-in_progress rows.
        assert "Searching market data" not in out
        assert "Building chart" not in out

    def test_truncates_long_content(self) -> None:
        """Content > 60 chars must be truncated with a trailing ``…``."""
        long_content = "x" * 100
        args = _todos_json([
            {"content": long_content,
             "activeForm": "Doing thing",
             "status": "pending"},
        ])
        out = format_todo_list(args)
        # 60-char cap (59 visible + …) — anything longer would blow past
        # the per-line budget.
        assert "…" in out
        # Original full-length string must NOT appear.
        assert long_content not in out

    def test_collapses_overflow(self) -> None:
        """12-item list with ``max_lines=5`` must show 4 rows + the
        "… +N more" summary line so the block stays compact."""
        items = [
            {"content": f"Task {i}",
             "activeForm": f"Doing task {i}",
             "status": "pending"}
            for i in range(12)
        ]
        out = format_todo_list(_todos_json(items), max_lines=5)
        lines = out.splitlines()
        # 1 header + 4 visible rows + 1 "… +N more" = 6 lines total.
        assert len(lines) == 6
        assert "… +" in lines[-1]
        assert "more" in lines[-1]

    def test_overflow_keeps_in_progress_visible(self) -> None:
        """When the active row would be hidden by max_lines truncation,
        it must be swapped into the last visible slot so the user always
        sees what the agent is doing RIGHT NOW."""
        items = [
            {"content": f"Task {i}",
             "activeForm": f"Doing task {i}",
             "status": "completed"}
            for i in range(10)
        ]
        # Make the last item the in-progress one (well past max_lines).
        items[-1] = {
            "content": "The active task",
            "activeForm": "Running the active task",
            "status": "in_progress",
        }
        out = format_todo_list(_todos_json(items), max_lines=5)
        assert "▣" in out
        assert "Running the active task" in out

    def test_empty_array_returns_empty(self) -> None:
        assert format_todo_list(b'{"todos":[]}') == ""

    def test_malformed_json_returns_empty(self) -> None:
        assert format_todo_list(b"not json at all") == ""

    def test_missing_todos_key_returns_empty(self) -> None:
        assert format_todo_list(b'{"other":[]}') == ""

    def test_null_todos_returns_empty(self) -> None:
        assert format_todo_list(b'{"todos":null}') == ""

    def test_non_dict_root_returns_empty(self) -> None:
        assert format_todo_list(b"[]") == ""

    def test_empty_args_returns_empty(self) -> None:
        assert format_todo_list(b"") == ""
        assert format_todo_list("") == ""

    def test_accepts_str_args(self) -> None:
        """JSON args may arrive as str (in tests / when an upstream
        already decoded). Both bytes and str must work."""
        items = [
            {"content": "a", "activeForm": "doing a", "status": "pending"},
        ]
        raw = json.dumps({"todos": items})
        out = format_todo_list(raw)
        assert "☐ a" in out

    def test_unknown_status_falls_back_to_pending_glyph(self) -> None:
        """A row with an unrecognised status must render as ☐ rather
        than crash — keeps the channel robust against agent-side bugs."""
        args = json.dumps({"todos": [
            {"content": "Mystery task",
             "activeForm": "Doing mystery task",
             "status": "whatever"},
        ]}).encode("utf-8")
        out = format_todo_list(args)
        assert "☐ Mystery task" in out

    def test_block_capped_at_safety_limit(self) -> None:
        """A pathological list must stay under the 1500-char hard cap
        so a runaway agent can never blow past Telegram's editMessageText
        limit. The cap is enforced AFTER max_lines collapse, so this
        test exercises the post-collapse truncation path with a very
        wide ``max_lines`` value."""
        items = [
            {"content": "x" * 50,
             "activeForm": "y" * 50,
             "status": "pending"}
            for _ in range(200)
        ]
        out = format_todo_list(_todos_json(items), max_lines=300)
        # The cap is 1500; allow exact equality, never overrun.
        assert len(out) <= 1500


class TestFormatToolStatusTodo:
    """``format_tool_status`` must special-case ``todo_write`` and emit
    the rendered checkbox list instead of the generic ``🔧`` line."""

    def test_renders_todo_list_for_todo_write(self) -> None:
        args = _todos_json([
            {"content": "Step one", "activeForm": "Doing step one",
             "status": "in_progress"},
            {"content": "Step two", "activeForm": "Doing step two",
             "status": "pending"},
        ])
        out = format_tool_status(_Ev(tool="todo_write", args_json=args))
        # The list header must replace the generic spinner line.
        assert out.startswith("📋 任务清单")
        # No legacy "🔧 todo_write" fallback should appear.
        assert "🔧 todo_write" not in out
        assert "item(s)" not in out

    def test_falls_back_to_generic_on_malformed_args(self) -> None:
        """If the todo_write args are unparseable we must NOT swallow
        the call — fall back to the generic 🔧 line so the user still
        sees that something happened."""
        out = format_tool_status(_Ev(tool="todo_write", args_json=b"not json"))
        # Either the generic fallback or the empty-list fallback are
        # acceptable; the point is the call cannot vanish silently.
        assert out  # non-empty
        # If it fell back to the generic line it must carry the tool name.
        if "📋" not in out:
            assert "todo_write" in out


class TestFormatToolResultTodo:
    """``format_tool_result`` must suppress the trailing ✅ line for
    ``todo_write`` so it doesn't clutter the spinner immediately after
    the checkbox list rendered on the call side."""

    def test_todo_write_result_returns_empty(self) -> None:
        assert format_tool_result(_Ev(tool="todo_write", duration_ms=3)) == ""

    def test_other_tools_still_render(self) -> None:
        # Sanity: the suppression is targeted, not a blanket disable.
        out = format_tool_result(_Ev(tool="read_file", duration_ms=10))
        assert out == "✅ read_file (10ms)"


class TestMutableSpinnerTodo:
    """Spinner-level wiring: ``todo_write`` calls must render the list,
    stash the args for end-of-turn summary builders, and suppress the
    paired ``tool_result``."""

    @pytest.mark.asyncio
    async def test_call_renders_todo_list(self) -> None:
        edits: list[str] = []

        async def edit(text: str) -> None:
            edits.append(text)

        spinner = MutableSpinner(edit)
        args = _todos_json([
            {"content": "a", "activeForm": "doing a", "status": "pending"},
            {"content": "b", "activeForm": "doing b", "status": "in_progress"},
        ])
        await spinner.on_tool_call(_Ev(tool="todo_write", args_json=args))
        # Exactly one edit, carrying the list header.
        assert len(edits) == 1
        assert edits[0].startswith("📋 任务清单")
        # The spinner stashed the args for the QQ summary path.
        assert spinner.last_todo_args == args

    @pytest.mark.asyncio
    async def test_result_is_suppressed(self) -> None:
        edits: list[str] = []

        async def edit(text: str) -> None:
            edits.append(text)

        spinner = MutableSpinner(edit)
        await spinner.on_tool_result(
            _Ev(tool="todo_write", duration_ms=3, is_error=False)
        )
        # No edit fired — the list already shown is the signal.
        assert edits == []

    @pytest.mark.asyncio
    async def test_last_todo_args_overwritten_on_each_call(self) -> None:
        """Two ``todo_write`` calls in one turn must leave only the
        LATEST snapshot on the spinner — the summary block re-renders
        that state, not an intermediate one."""
        edits: list[str] = []

        async def edit(text: str) -> None:
            edits.append(text)

        spinner = MutableSpinner(edit)
        first = _todos_json([
            {"content": "a", "activeForm": "doing a", "status": "pending"},
        ])
        second = _todos_json([
            {"content": "a", "activeForm": "doing a", "status": "completed"},
            {"content": "b", "activeForm": "doing b", "status": "in_progress"},
        ])
        await spinner.on_tool_call(_Ev(tool="todo_write", args_json=first))
        await spinner.on_tool_call(_Ev(tool="todo_write", args_json=second))
        assert spinner.last_todo_args == second

    @pytest.mark.asyncio
    async def test_str_args_coerced_to_bytes(self) -> None:
        """Defensive: a unit-test event might feed args_json as str. The
        spinner must normalise to bytes so the summary builder can hand
        them straight back to format_todo_list (which accepts both,
        but the bytes round-trip is the documented contract)."""
        edits: list[str] = []

        async def edit(text: str) -> None:
            edits.append(text)

        spinner = MutableSpinner(edit)
        args_str = '{"todos":[{"content":"x","activeForm":"doing x","status":"pending"}]}'
        await spinner.on_tool_call(_Ev(tool="todo_write", args_json=args_str))  # type: ignore[arg-type]
        assert isinstance(spinner.last_todo_args, bytes)

    @pytest.mark.asyncio
    async def test_spinner_renders_todo_with_current_op_below(self) -> None:
        """A ``todo_write`` call followed by a non-todo ``tool_call`` must
        produce a combined edit carrying BOTH the rendered list AND the
        live op line ``🔧 web_search 'x'``, separated by a blank line.
        This is the v1.1.1 fix: users could see the list flip but lost
        sight of which tool was firing right now."""
        edits: list[str] = []

        async def edit(text: str) -> None:
            edits.append(text)

        spinner = MutableSpinner(edit)
        args = _todos_json([
            {"content": "Search market data",
             "activeForm": "Searching market data",
             "status": "in_progress"},
            {"content": "Draft memo",
             "activeForm": "Drafting memo",
             "status": "pending"},
        ])
        await spinner.on_tool_call(_Ev(tool="todo_write", args_json=args))
        await spinner.on_tool_call(_Ev(
            plugin="web_search", tool="web_search",
            args_json=b'{"query":"gpt-5.5 news"}',
        ))
        # At least one edit must carry BOTH blocks.
        combined = [
            e for e in edits
            if "📋 任务清单" in e and "🔧 web_search" in e
        ]
        assert combined, f"no combined edit found: {edits}"
        sample = combined[-1]
        # Blank-line separator between the todo block and the op line.
        assert "\n\n🔧 web_search" in sample, sample
        # Todo content still visible (in-progress row uses activeForm).
        assert "▣ Searching market data" in sample
        # Op arg preview surfaces.
        assert "gpt-5.5 news" in sample

    @pytest.mark.asyncio
    async def test_spinner_op_line_clears_on_token_delta_first(self) -> None:
        """When tokens begin, the op-flow line under the todo list must
        drop. The visible status switches to bare ``STATUS_GENERATING``
        — no stale ``🔧 web_search`` line lingers under the list once
        the answer is coming through. (Whether the GENERATING transition
        keeps the todo header above it is an implementation choice; the
        critical invariant is that the OLD op line is gone.)"""
        edits: list[str] = []

        async def edit(text: str) -> None:
            edits.append(text)

        spinner = MutableSpinner(edit)
        args = _todos_json([
            {"content": "Search market data",
             "activeForm": "Searching market data",
             "status": "in_progress"},
        ])
        await spinner.on_tool_call(_Ev(tool="todo_write", args_json=args))
        await spinner.on_tool_call(_Ev(
            plugin="web_search", tool="web_search",
            args_json=b'{"query":"gpt-5.5 news"}',
        ))
        await spinner.on_tool_result(_Ev(
            tool="web_search", duration_ms=302, is_error=False,
        ))
        # Drain a real (non-reasoning) token delta.
        await spinner.on_token_delta("hello", is_reasoning=False)
        # Final edit during streaming: must be the bare GENERATING
        # status. No leftover web_search line, no completion line.
        assert edits[-1] == STATUS_GENERATING, edits[-1]
        # And the internal op-status really IS cleared (defensive).
        assert spinner._last_op_status is None  # type: ignore[attr-defined]
        # text_parts accumulated normally.
        assert "".join(spinner.text_parts) == "hello"

    @pytest.mark.asyncio
    async def test_spinner_reasoning_appears_under_todo(self) -> None:
        """Reasoning deltas (Anthropic ``thinking``, DeepSeek-R1
        ``reasoning_content``) must render UNDER the todo list, not
        replace it. Users keep sight of the plan while the model
        thinks out loud."""
        edits: list[str] = []

        async def edit(text: str) -> None:
            edits.append(text)

        spinner = MutableSpinner(edit)
        args = _todos_json([
            {"content": "Search market data",
             "activeForm": "Searching market data",
             "status": "in_progress"},
        ])
        await spinner.on_tool_call(_Ev(tool="todo_write", args_json=args))
        await spinner.on_token_delta(
            "I should search broader sources first",
            is_reasoning=True,
        )
        # The latest edit must combine the todo block with the
        # reasoning preview line.
        combined = edits[-1]
        assert "📋 任务清单" in combined
        assert STATUS_REASONING_PREFIX in combined
        assert "search broader" in combined
        # Blank-line separator.
        assert f"\n\n{STATUS_REASONING_PREFIX}" in combined, combined
        # Reasoning text does NOT accumulate into the reply buffer.
        assert spinner.text_parts == []


# ---------------------------------------------------------------------------
# ask_user — pause-and-ask tool rendering.
# ---------------------------------------------------------------------------


def _ask_user_json(question: str, options: list[str] | None = None,
                   multiple: bool = False) -> bytes:
    payload: dict[str, Any] = {"question": question}
    if options is not None:
        payload["options"] = options
    if multiple:
        payload["multiple"] = True
    return json.dumps(payload).encode("utf-8")


class TestAskUserToolName:
    def test_constant(self) -> None:
        assert ASK_USER_TOOL == "ask_user"


class TestFormatToolStatusAskUser:
    """``format_tool_status`` must special-case ``ask_user`` and emit
    the ❓ question block instead of the generic 🔧 line."""

    def test_format_tool_status_ask_user_renders_question(self) -> None:
        args = _ask_user_json("Should I overwrite README.md?")
        out = format_tool_status(_Ev(tool="ask_user", args_json=args))
        assert out.startswith("❓ 等待用户回答")
        assert "README.md" in out
        # No generic fallback strings.
        assert "🔧 ask_user" not in out

    def test_format_tool_status_ask_user_with_options_renders_bullets(
        self,
    ) -> None:
        args = _ask_user_json("Pick", options=["yes", "no", "maybe"])
        out = format_tool_status(_Ev(tool="ask_user", args_json=args))
        # Question line first.
        assert out.splitlines()[0].startswith("❓")
        # Bulleted options follow, one per line with the "  · " marker.
        for opt in ("yes", "no", "maybe"):
            assert f"  · {opt}" in out

    def test_format_tool_status_ask_user_falls_back_on_malformed(self) -> None:
        out = format_tool_status(_Ev(tool="ask_user", args_json=b"not json"))
        # Either the generic 🔧 line OR an empty options block — never
        # silently nothing.
        assert out  # non-empty
        # If we fell through to the generic renderer, the tool name is
        # carried through.
        if "❓" not in out:
            assert "ask_user" in out


class TestFormatToolResultAskUser:
    """``format_tool_result`` must suppress the trailing ✅ line for
    ``ask_user`` (the question line on the call side IS the signal)."""

    def test_format_tool_result_ask_user_is_suppressed(self) -> None:
        assert format_tool_result(_Ev(tool="ask_user", duration_ms=3)) == ""

    def test_other_tools_still_render(self) -> None:
        # Sanity: the suppression is targeted, not blanket.
        out = format_tool_result(_Ev(tool="read_file", duration_ms=10))
        assert out == "✅ read_file (10ms)"


class TestParseAskUserArgs:
    def test_full(self) -> None:
        args = _ask_user_json("q?", options=["a", "b"], multiple=True)
        q, opts, multi = parse_ask_user_args(args)
        assert q == "q?"
        assert opts == ["a", "b"]
        assert multi is True

    def test_no_options(self) -> None:
        q, opts, multi = parse_ask_user_args(_ask_user_json("q?"))
        assert q == "q?"
        assert opts == []
        assert multi is False

    def test_malformed_returns_empty(self) -> None:
        q, opts, multi = parse_ask_user_args(b"not json")
        assert q == ""
        assert opts == []
        assert multi is False

    def test_long_label_truncates(self) -> None:
        long = "x" * 500
        _q, opts, _multi = parse_ask_user_args(
            _ask_user_json("q?", options=[long])
        )
        assert len(opts) == 1
        assert opts[0].endswith("…")
        assert len(opts[0]) < 200


class TestFormatAskUserStandalone:
    def test_empty_args_empty_output(self) -> None:
        assert format_ask_user(b"") == ""

    def test_empty_question_empty_output(self) -> None:
        # ``ask_user`` MUST have a question — empty body returns "".
        assert format_ask_user(b'{"question": ""}') == ""

    def test_with_options_block(self) -> None:
        out = format_ask_user(_ask_user_json("Pick", options=["a", "b"]))
        lines = out.splitlines()
        assert lines[0].startswith("❓")
        assert lines[1] == "  · a"
        assert lines[2] == "  · b"


class TestMutableSpinnerAskUser:
    """The spinner must stash the args (for the Telegram keyboard) and
    suppress the matching ``tool_result``."""

    @pytest.mark.asyncio
    async def test_call_renders_question_and_stashes_args(self) -> None:
        edits: list[str] = []

        async def edit(text: str) -> None:
            edits.append(text)

        spinner = MutableSpinner(edit)
        args = _ask_user_json("Overwrite?", options=["yes", "no"])
        await spinner.on_tool_call(_Ev(tool="ask_user", args_json=args))
        # The spinner emitted the ❓ block.
        assert edits, "expected an edit with the question"
        assert edits[-1].startswith("❓")
        # The args are available for end-of-turn handlers.
        assert spinner.last_ask_user_args == args

    @pytest.mark.asyncio
    async def test_result_is_suppressed(self) -> None:
        edits: list[str] = []

        async def edit(text: str) -> None:
            edits.append(text)

        spinner = MutableSpinner(edit)
        await spinner.on_tool_result(
            _Ev(tool="ask_user", duration_ms=2, is_error=False)
        )
        # No edit fired — the question already shown IS the signal.
        assert edits == []

    @pytest.mark.asyncio
    async def test_last_ask_user_args_default_none(self) -> None:
        async def edit(text: str) -> None:
            return None

        spinner = MutableSpinner(edit)
        assert spinner.last_ask_user_args is None


# ---------------------------------------------------------------------------
# W3.1 — heartbeat / cancelling formatters + spinner reactions.
# ---------------------------------------------------------------------------


from corlinman_channels._status import (  # noqa: E402 — group with W3.1 tests
    STATUS_CANCELLING,
    format_elapsed_ms,
    format_tool_heartbeat,
)


class TestFormatElapsedMs:
    """Spec: §1.4 of ``docs/PLAN_TASK_OBSERVABILITY.md`` — must render
    human-friendly elapsed-time text for the heartbeat spinner."""

    def test_sub_second(self) -> None:
        assert format_elapsed_ms(500) == "500ms"

    def test_exact_second(self) -> None:
        assert format_elapsed_ms(1000) == "1s"

    def test_seconds(self) -> None:
        assert format_elapsed_ms(12_345) == "12s"

    def test_minutes(self) -> None:
        # 83s → "1m23s"
        assert format_elapsed_ms(83_000) == "1m23s"

    def test_long_minutes(self) -> None:
        assert format_elapsed_ms(605_000) == "10m05s"

    def test_negative_clamps_to_zero(self) -> None:
        # Clock skew defensiveness — never render -2s.
        assert format_elapsed_ms(-5) == "0ms"


class TestFormatToolHeartbeat:
    """The heartbeat line keeps the same 🔧 prefix as the call line so
    the user sees a stable spinner shape as a long-running tool ticks."""

    def test_basic_shape(self) -> None:
        assert format_tool_heartbeat("run_shell", 12_500) == "🔧 run_shell … 12s"

    def test_sub_second(self) -> None:
        out = format_tool_heartbeat("read_file", 800)
        assert out == "🔧 read_file … 800ms"

    def test_minute_plus(self) -> None:
        assert (
            format_tool_heartbeat("subagent_spawn", 90_000)
            == "🔧 subagent_spawn … 1m30s"
        )

    def test_empty_name_renders_question_mark(self) -> None:
        assert format_tool_heartbeat("", 5_000) == "🔧 ? … 5s"

    def test_long_name_truncates(self) -> None:
        out = format_tool_heartbeat("x" * 200, 1_000)
        assert out.startswith("🔧 ")
        assert "..." in out
        # Sanity: doesn't blow Telegram's 4096-char hard limit.
        assert len(out) < 100


class TestStatusCancellingConstant:
    def test_emoji_and_text(self) -> None:
        assert STATUS_CANCELLING == "⏹ 正在取消…"


class TestSpinnerHeartbeat:
    """``MutableSpinner.on_tool_heartbeat`` refreshes the op-flow line
    iff a tool is currently displayed; otherwise it stays silent."""

    @pytest.mark.asyncio
    async def test_heartbeat_refreshes_after_tool_call(self) -> None:
        edits: list[str] = []

        async def edit(text: str) -> None:
            edits.append(text)

        spinner = MutableSpinner(edit)
        await spinner.on_tool_call(
            _Ev(tool="run_shell", args_json=b'{"command":"sleep 60"}')
        )
        # Initial call line.
        assert edits[-1].startswith("🔧 run_shell")
        await spinner.on_tool_heartbeat("run_shell", 10_000)
        # Last edit must be the heartbeat line.
        assert edits[-1] == "🔧 run_shell … 10s"

    @pytest.mark.asyncio
    async def test_heartbeat_before_any_tool_is_suppressed(self) -> None:
        edits: list[str] = []

        async def edit(text: str) -> None:
            edits.append(text)

        spinner = MutableSpinner(edit)
        await spinner.on_tool_heartbeat("any_tool", 5_000)
        # No edit fired — the heartbeat arrived without context.
        assert edits == []

    @pytest.mark.asyncio
    async def test_heartbeat_while_generating_is_suppressed(self) -> None:
        edits: list[str] = []

        async def edit(text: str) -> None:
            edits.append(text)

        spinner = MutableSpinner(edit)
        await spinner.on_tool_call(
            _Ev(tool="run_shell", args_json=b'{"command":"x"}')
        )
        # Switch into GENERATING.
        await spinner.on_token_delta("Hello", is_reasoning=False)
        prior_len = len(edits)
        # Heartbeat must not clobber the answer-in-flight indicator.
        await spinner.on_tool_heartbeat("run_shell", 30_000)
        assert len(edits) == prior_len


class TestSpinnerCancelling:
    """``MutableSpinner.on_cancelling`` flips to ``STATUS_CANCELLING``
    regardless of prior state."""

    @pytest.mark.asyncio
    async def test_flips_to_cancelling(self) -> None:
        edits: list[str] = []

        async def edit(text: str) -> None:
            edits.append(text)

        spinner = MutableSpinner(edit)
        await spinner.on_tool_call(
            _Ev(tool="run_shell", args_json=b'{"command":"x"}')
        )
        await spinner.on_cancelling()
        assert edits[-1] == STATUS_CANCELLING

    @pytest.mark.asyncio
    async def test_cancelling_idempotent_against_dedup(self) -> None:
        edits: list[str] = []

        async def edit(text: str) -> None:
            edits.append(text)

        spinner = MutableSpinner(edit)
        await spinner.on_cancelling()
        await spinner.on_cancelling()
        # Second call dedups via ``_maybe_edit``.
        assert edits.count(STATUS_CANCELLING) == 1
