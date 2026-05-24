"""Shared mutable-spinner status helpers — extracted from ``service.py``.

The Telegram channel pioneered a "mutable spinner line" UX where a
placeholder message is edited in place as the agent works: tool calls
render with arg previews, tool results render with ✅/❌ + duration, and
reasoning deltas show "💭 推理: …" excerpts before the final reply
replaces the placeholder. Discord, Slack, and Feishu all support
``editMessage`` and want the same behavior — so the per-turn state
machine lives here as a transport-agnostic class the four
``handle_one_*`` helpers compose.

## Design

* :class:`MutableSpinner` owns the per-turn state (``last_status``,
  ``text_parts``) and calls a transport-supplied ``edit_callback`` whenever
  the visible status text would change. No I/O lives in this class
  beyond the callback; the channel handler controls everything else
  (placeholder send, typing pulse, final emit).
* Each channel passes an optional ``send_attachment_handler`` —
  Telegram calls into ``_telegram_send_attachment``, Discord uploads via
  ``POST /channels/{id}/messages``, Slack via ``files.upload``, Feishu via
  ``/im/v1/files`` + ``msg_type="file"``. The spinner uses it to
  intercept the ``send_attachment`` tool: the handler's return string
  becomes the next status line (📎 …), and the spinner *suppresses* the
  ✅ completion edit because the dedicated status already conveys success.

## Constants

All exported because the test suite asserts on the emoji + duration
strings and the truncation marker. Keeping them as module-level names
means the per-channel handlers can also reference them without
re-defining the literals.
"""

from __future__ import annotations

import json as _json
from collections.abc import Awaitable, Callable
from typing import Any

__all__ = [
    "REASONING_PREVIEW_CHARS",
    "SEND_ATTACHMENT_TOOL",
    "STATUS_GENERATING",
    "STATUS_REASONING_PREFIX",
    "STATUS_THINKING",
    "TEXT_LIMIT",
    "TRUNCATION_MARKER",
    "MutableSpinner",
    "format_tool_result",
    "format_tool_status",
    "parse_send_attachment_args",
    "tool_arg_preview",
    "truncate_reply",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Initial placeholder text — shown before any token / tool events land.
STATUS_THINKING: str = "🧠 思考中..."

#: First time a (non-reasoning) ``token_delta`` arrives: signals the agent
#: has started producing the final answer.
STATUS_GENERATING: str = "✍️ 生成回复中..."

#: Prefix for the reasoning-delta spinner line (Anthropic ``thinking``
#: blocks, DeepSeek-R1 ``reasoning_content``). The first
#: :data:`REASONING_PREVIEW_CHARS` chars of the model's internal monologue
#: get sandwiched between this prefix and the rest of the spinner.
STATUS_REASONING_PREFIX: str = "💭 推理: "

#: Max chars of reasoning text to embed inside the spinner line — keeps
#: the edit small so we don't blow past per-channel message-length caps.
REASONING_PREVIEW_CHARS: int = 80

#: Soft cap on the final reply body. Telegram's hard limit is 4096 chars
#: for ``editMessageText``; Slack is 40k; Discord is 2000; Feishu is
#: ~30k for ``msg_type=text``. The cap here is the smallest "safe for
#: everyone" value with a few-char safety margin — channels with looser
#: limits can override before calling :func:`truncate_reply`.
TEXT_LIMIT: int = 4000

#: Suffix appended when :func:`truncate_reply` actually truncated.
TRUNCATION_MARKER: str = "\n\n[...回复过长,已截断]"

#: Tool name the channel handler intercepts for file uploads. The agent
#: side dispatches a no-op stub so the reasoning loop keeps going while
#: the channel side does the real transport-specific work.
SEND_ATTACHMENT_TOOL: str = "send_attachment"


# ---------------------------------------------------------------------------
# Pure helpers — no I/O, share behavior across all channels.
# ---------------------------------------------------------------------------


def truncate_reply(body: str, limit: int = TEXT_LIMIT) -> str:
    """Clamp ``body`` to ``limit`` chars, appending :data:`TRUNCATION_MARKER`
    when truncation actually happened.

    The marker eats ~32 chars from the budget so the final string is still
    within the limit. The caller is expected to log the original length so
    drops stay observable in production.
    """
    if len(body) <= limit:
        return body
    # Leave room for the marker (≤32 chars).
    return body[: limit - 32] + TRUNCATION_MARKER


def tool_arg_preview(tool: str, args_json: bytes | str) -> str:
    """Extract a one-line preview from a tool's args JSON.

    Returns an empty string for unknown tools or malformed args so the
    caller can render the bare tool name. Mirrors hermes-agent's
    ``_format_tool_progress`` per-tool argument summarisation — gives the
    user a concrete hook into what the agent is doing without dumping the
    whole JSON.
    """
    try:
        raw_str = (
            args_json.decode("utf-8") if isinstance(args_json, (bytes, bytearray))
            else args_json
        )
        args = _json.loads(raw_str or "{}")
    except (UnicodeDecodeError, ValueError):
        return ""
    if not isinstance(args, dict):
        return ""

    def _short(v: Any, n: int = 60) -> str:
        s = str(v).replace("\n", " ").strip()
        return s if len(s) <= n else s[: n - 1] + "…"

    if tool in ("web_search",):
        q = args.get("query") or args.get("q") or ""
        return _short(repr(q))
    if tool in ("web_fetch",):
        return _short(args.get("url") or "", 70)
    if tool in ("read_file", "write_file", "edit_file", "list_files"):
        return _short(args.get("path") or args.get("file") or "", 60)
    if tool in ("search_files",):
        return _short(repr(args.get("pattern") or args.get("regex") or ""))
    if tool in ("apply_patch",):
        return _short(args.get("file") or args.get("path") or "", 60)
    if tool in ("run_shell",):
        return _short(args.get("command") or args.get("cmd") or "", 60)
    if tool in ("calculator",):
        return _short(args.get("expression") or args.get("expr") or "", 50)
    if tool in (SEND_ATTACHMENT_TOOL,):
        p = args.get("path") or args.get("filename") or ""
        return _short(p.rsplit("/", 1)[-1] if "/" in p else p, 50)
    if tool in ("todo_write",):
        items = args.get("items") or args.get("todos") or []
        if isinstance(items, list):
            return f"{len(items)} item(s)"
    if tool in ("revert_changes",):
        return ""
    if tool in ("subagent_spawn", "subagent_spawn_many"):
        return _short(args.get("agent") or args.get("name") or "", 40)
    # Fallback: pick the first scalar value we can find.
    for k in ("name", "path", "query", "url", "text"):
        if k in args and isinstance(args[k], str):
            return _short(args[k], 60)
    return ""


def format_tool_status(ev: Any) -> str:
    """Render a ``ToolCallEvent`` as the next mutable-line status.

    Mirrors hermes-agent's ``_last_activity_desc`` style ("emoji + label +
    arg preview"). Truncates / sanitises so a runaway tool name can't blow
    past channel-specific message-length caps when fed into the edit.
    """
    tool = (getattr(ev, "tool", "") or "?").replace("\n", " ")
    plugin = (getattr(ev, "plugin", "") or "").replace("\n", " ")
    label = f"{plugin}.{tool}" if plugin and plugin != tool else tool
    if len(label) > 60:
        label = label[:57] + "..."
    preview = tool_arg_preview(tool, getattr(ev, "args_json", b""))
    if preview:
        return f"🔧 {label}  {preview}"
    return f"🔧 调用工具: {label}"


def format_tool_result(ev: Any) -> str:
    """Render a ``ToolResultEvent`` as a "tool finished" line.

    ✅ for success, ❌ for error. Duration is human-friendly (ms < 1s,
    seconds otherwise). Mirrors hermes-agent's
    ``tool_progress_callback("tool.completed", duration=..., is_error=...)``
    rendering.
    """
    tool = (getattr(ev, "tool", "") or "?").replace("\n", " ")
    if len(tool) > 60:
        tool = tool[:57] + "..."
    dur_ms = int(getattr(ev, "duration_ms", 0) or 0)
    dur = f"{dur_ms}ms" if dur_ms < 1000 else f"{dur_ms / 1000:.1f}s"
    if getattr(ev, "is_error", False):
        msg = (getattr(ev, "error_summary", "") or "").replace("\n", " ")
        msg = msg[:80] + "…" if len(msg) > 80 else msg
        return f"❌ {tool} 失败 ({dur}){': ' + msg if msg else ''}"
    return f"✅ {tool} ({dur})"


def parse_send_attachment_args(ev: Any) -> tuple[str, str | None, str | None]:
    """Parse ``send_attachment`` tool args into ``(path, caption, filename)``.

    Returns empty path on any decode/parse failure — caller should surface
    a friendly status instead of raising.
    """
    raw = getattr(ev, "args_json", b"") or b""
    try:
        raw_str = (
            raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        )
        obj = _json.loads(raw_str or "{}")
    except (UnicodeDecodeError, _json.JSONDecodeError):
        return ("", None, None)
    if not isinstance(obj, dict):
        return ("", None, None)
    path = str(obj.get("path") or "").strip()
    caption = obj.get("caption")
    if caption is not None and not isinstance(caption, str):
        caption = None
    filename = obj.get("filename")
    if filename is not None and not isinstance(filename, str):
        filename = None
    return (path, caption, filename)


# ---------------------------------------------------------------------------
# MutableSpinner — per-turn state machine.
# ---------------------------------------------------------------------------


#: Type alias for the edit callback the spinner calls whenever the visible
#: status text would change. Channels wrap their per-transport
#: ``editMessage`` call (Telegram ``editMessageText``, Discord
#: ``PATCH /channels/{id}/messages/{id}``, Slack ``chat.update``, Feishu
#: ``PUT /im/v1/messages/{id}``) in a function with this signature.
EditCallback = Callable[[str], Awaitable[None]]

#: Type alias for the optional ``send_attachment`` handler each channel
#: passes in. Returns a status string (📎 / ⚠️) that becomes the next
#: spinner line — the dedicated upload-result status doubles as the
#: completion indicator, which is why the spinner suppresses the ✅ edit
#: for the matching ``tool_result``.
SendAttachmentHandler = Callable[[Any], Awaitable[str]]


class MutableSpinner:
    """Per-turn state machine that drives the mutable-spinner status line.

    The four ``handle_one_*`` channel helpers feed every streamed
    ``ChatEventLike`` through this object's ``on_*`` methods. Token deltas
    are accumulated into a buffer the caller drains at the end of the
    turn; tool calls / results trigger ``edit_callback`` with the next
    rendered status line.

    Behavior is identical to the original Telegram inline implementation
    (the tests for that code path still pass against this class via the
    refactored ``handle_one_telegram``). Channels with no
    ``send_attachment_handler`` simply pass ``None`` and the spinner
    renders ``send_attachment`` like any other tool.

    Notes:
        * The spinner does NOT send the initial placeholder or the final
          edit — those belong to the per-channel handler because the
          send/edit shapes differ across transports.
        * ``last_status`` is exposed so the caller can suppress
          identical-content edits on the final emit (Telegram returns
          400 "message is not modified" otherwise).
        * The buffer ``text_parts`` is what the caller joins to produce
          the final reply; reasoning deltas are intentionally NOT
          accumulated (the user-facing text is the answer, not the
          model's internal monologue).
    """

    __slots__ = (
        "_edit_callback",
        "_last_status",
        "_send_attachment_handler",
        "_text_parts",
    )

    def __init__(
        self,
        edit_callback: EditCallback,
        *,
        send_attachment_handler: SendAttachmentHandler | None = None,
    ) -> None:
        self._edit_callback = edit_callback
        self._send_attachment_handler = send_attachment_handler
        self._last_status: str = STATUS_THINKING
        self._text_parts: list[str] = []

    @property
    def last_status(self) -> str:
        """The last status text we emitted to ``edit_callback``.

        Channels read this to skip a no-op final edit when the reply body
        happens to match what's already displayed.
        """
        return self._last_status

    @property
    def text_parts(self) -> list[str]:
        """The accumulated non-reasoning token deltas, in order.

        Mutating the returned list mutates internal state — callers
        usually ``"".join(spinner.text_parts)`` once at end-of-turn.
        """
        return self._text_parts

    async def _maybe_edit(self, text: str) -> None:
        """Call ``edit_callback`` iff the visible text would actually change.

        Telegram (and Feishu in some configurations) returns a 400 when
        an edit produces unchanged text; deduplicating here keeps the
        per-channel handler from having to remember the last status.
        """
        if text == self._last_status:
            return
        self._last_status = text
        await self._edit_callback(text)

    async def on_token_delta(self, text: str, is_reasoning: bool) -> None:
        """Handle one ``token_delta`` event.

        * Reasoning deltas (Anthropic ``thinking``, DeepSeek-R1
          ``reasoning_content``) render as 💭 lines but are *not*
          accumulated into the final reply.
        * Non-reasoning deltas are appended to ``text_parts`` and, on
          the first one, switch the status to ✍️ STATUS_GENERATING.
        """
        if is_reasoning:
            stripped = text.strip()
            if not stripped:
                return
            snippet = stripped.replace("\n", " ")
            if len(snippet) > REASONING_PREVIEW_CHARS:
                snippet = snippet[: REASONING_PREVIEW_CHARS - 1] + "…"
            await self._maybe_edit(f"{STATUS_REASONING_PREFIX}{snippet}")
            return
        self._text_parts.append(text)
        if self._last_status != STATUS_GENERATING:
            await self._maybe_edit(STATUS_GENERATING)

    async def on_tool_call(self, ev: Any) -> str | None:
        """Handle one ``tool_call`` event.

        For ``send_attachment``: if the channel registered a handler, call
        it (the handler does the actual upload) and render the returned
        status line. Returns the literal string ``"intercept"`` to signal
        the caller that the matching ``tool_result`` should be suppressed
        (the 📎 status already conveys completion).

        For everything else: render the standard 🔧 status line. Returns
        ``None``.
        """
        tool = getattr(ev, "tool", "") or ""
        if tool == SEND_ATTACHMENT_TOOL and self._send_attachment_handler is not None:
            status = await self._send_attachment_handler(ev)
            await self._maybe_edit(status)
            return "intercept"
        await self._maybe_edit(format_tool_status(ev))
        return None

    async def on_tool_result(self, ev: Any) -> None:
        """Handle one ``tool_result`` event.

        Renders ✅ / ❌ + human duration. ``send_attachment`` completions
        are suppressed because :meth:`on_tool_call` already rendered the
        dedicated 📎 line — re-overwriting it with ✅ would lose the
        useful "📎 已发送文件: X" status for ~zero gain.
        """
        if getattr(ev, "tool", "") == SEND_ATTACHMENT_TOOL:
            return
        await self._maybe_edit(format_tool_result(ev))
