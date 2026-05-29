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
import os as _os
from collections.abc import Awaitable, Callable
from pathlib import Path as _Path
from typing import Any

__all__ = [
    "ASK_USER_TOOL",
    "REASONING_PREVIEW_CHARS",
    "SEND_ATTACHMENT_TOOL",
    "STATUS_CANCELLING",
    "STATUS_GENERATING",
    "STATUS_REASONING_PREFIX",
    "STATUS_THINKING",
    "TEXT_LIMIT",
    "TODO_WRITE_TOOL",
    "TRUNCATION_MARKER",
    "MutableSpinner",
    "chunk_reply",
    "format_ask_user",
    "format_elapsed_ms",
    "format_todo_list",
    "format_tool_heartbeat",
    "format_tool_result",
    "format_tool_status",
    "format_turn_footer",
    "parse_ask_user_args",
    "parse_send_attachment_args",
    "resolve_attachment_path",
    "tool_arg_preview",
    "truncate_reply",
    "try_append_footer",
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

#: Tool name for the agent's session-scoped task list. Channels render
#: the JSON args as a checkbox list (``☑ / ▣ / ☐``) instead of the
#: generic ``🔧 todo_write {N} item(s)`` spinner line so the user sees
#: the actual plan the agent is working through.
TODO_WRITE_TOOL: str = "todo_write"

#: Tool name for the agent's "pause and ask the user" tool. The spinner
#: special-cases this just like ``todo_write`` / ``send_attachment``:
#: instead of the generic ``🔧 ask_user ...`` line we render the
#: question (and its canned options as bullets, when present). The
#: matching ``tool_result`` is suppressed — the question IS the status.
ASK_USER_TOOL: str = "ask_user"

#: Status line shown the moment a turn is cancelled (W3.1). Driven by
#: the :class:`corlinman_agent.events.Cancelling` envelope the reasoning
#: loop emits inside :meth:`ReasoningLoop.cancel`, so the user sees
#: feedback within ~50ms instead of waiting for the next round
#: boundary's ``ErrorEvent(reason="cancelled")``.
STATUS_CANCELLING: str = "⏹ 正在取消…"


# ---------------------------------------------------------------------------
# Pure helpers — no I/O, share behavior across all channels.
# ---------------------------------------------------------------------------


def truncate_reply(body: str, limit: int = TEXT_LIMIT) -> str:
    """Clamp ``body`` to ``limit`` chars, appending :data:`TRUNCATION_MARKER`
    when truncation actually happened.

    Used only for surfaces that physically cannot multi-send (the spinner
    line is edited in place, so it gets one shot per render). Final
    replies should call :func:`chunk_reply` + multi-send instead — see
    that helper for the rationale.
    """
    if len(body) <= limit:
        return body
    # Leave room for the marker (≤32 chars).
    return body[: limit - 32] + TRUNCATION_MARKER


def chunk_reply(
    body: str,
    limit: int = TEXT_LIMIT,
    *,
    prefix_overhead: int = 16,
) -> list[str]:
    """Split ``body`` into chunks ≤ ``limit`` chars, on natural boundaries.

    Returns a list of strings each prefixed with ``(n/N)\\n`` when there
    is more than one chunk so the reader knows the message is continued.
    A single-chunk body is returned unchanged.

    ``prefix_overhead`` reserves room for the ``(n/N)\\n`` header inside
    each chunk's char budget. Default 16 covers ``(99/99)\\n`` plus a
    safety margin.

    Boundary preference: paragraph break (``\\n\\n``) → line break
    (``\\n``) → sentence (``. ``/``。``) → hard char cut. The greedy
    walker always picks the **latest** boundary within the effective
    budget; if no boundary lands past half the budget the cut is forced
    at ``effective`` to avoid pathological tiny chunks.

    Unlike :func:`truncate_reply` this never appends a "truncated"
    marker — every character of ``body`` is preserved across the
    returned chunks. The caller is responsible for sending each chunk
    via its channel's transport (multiple ``sendMessage`` for Telegram /
    Discord / Slack / Feishu / QQ).
    """
    if len(body) <= limit:
        return [body]

    effective = max(limit - prefix_overhead, limit // 2)
    half = effective // 2
    chunks: list[str] = []
    remaining = body
    while remaining:
        if len(remaining) <= effective:
            chunks.append(remaining)
            break
        # Find the latest natural boundary within [half, effective].
        window = remaining[:effective]
        cut = window.rfind("\n\n")
        if cut < half:
            cut = window.rfind("\n")
        if cut < half:
            # Sentence boundaries — ASCII period+space, CJK full-stop.
            cut = max(window.rfind(". "), window.rfind("。"), window.rfind("！"), window.rfind("？"))
            if cut > 0:
                cut += 1  # include the punctuation itself
        if cut < half:
            cut = effective  # hard char cut
        # Slice; don't strip leading/trailing whitespace from chunk
        # contents — preserves code-block fidelity if the body contains
        # ``` ... ``` spanning chunks.
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip("\n")
    if len(chunks) == 1:
        return chunks
    n = len(chunks)
    return [f"({i + 1}/{n})\n{c}" for i, c in enumerate(chunks)]


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


#: Max characters per todo line (content / activeForm) before truncation.
_TODO_LINE_CHARS: int = 60

#: Hard cap on the rendered todo block — Telegram's editMessageText hard
#: limit is 4096; keep a comfortable safety margin so a runaway list
#: never blows past it (and so the surrounding spinner / reply body has
#: room too).
_TODO_BLOCK_CHARS: int = 1500


def format_todo_list(args_json: bytes | str, *, max_lines: int = 8) -> str:
    """Render ``todo_write`` JSON args as a checkbox list.

    Output shape::

        📋 任务清单 (2/5):
        ☑ Search market data
        ☑ Collate vendor list
        ▣ Drafting decision memo
        ☐ Build chart
        ☐ Send final files

    * ``☑`` (completed) / ``▣`` (in_progress) / ``☐`` (pending or unknown).
    * The ``in_progress`` row prefers ``activeForm`` (present-continuous —
      "Drafting decision memo") over ``content``; everything else uses
      ``content``.
    * Long lines are truncated to :data:`_TODO_LINE_CHARS` with a trailing
      ``…``.
    * When the list is longer than ``max_lines`` we show the first
      ``max_lines - 1`` entries (always keeping the active row visible)
      plus a trailing ``… +N more`` summary line.
    * Empty / malformed input returns ``""`` so the caller can fall back
      to the generic spinner line.

    The whole rendered block is capped at :data:`_TODO_BLOCK_CHARS` to
    keep Telegram's ``editMessageText`` happy and to leave room for the
    final assistant reply when prepended to a QQ summary block.
    """
    raw_str: str
    try:
        if isinstance(args_json, (bytes, bytearray)):
            raw_str = args_json.decode("utf-8")
        else:
            raw_str = args_json or ""
    except UnicodeDecodeError:
        return ""
    if not raw_str.strip():
        return ""
    try:
        obj = _json.loads(raw_str)
    except ValueError:
        return ""
    if not isinstance(obj, dict):
        return ""

    todos = obj.get("todos")
    if not isinstance(todos, list) or not todos:
        return ""

    # Normalise each row up-front so the slice logic below can stay
    # ignorant of validation.
    rows: list[tuple[str, str]] = []  # (mark, text)
    done = 0
    in_progress_idx: int | None = None
    for i, entry in enumerate(todos):
        if not isinstance(entry, dict):
            # Malformed entry: render a placeholder so the user notices
            # without crashing the whole list.
            rows.append(("☐", "(invalid)"))
            continue
        status = entry.get("status")
        content = entry.get("content")
        active = entry.get("activeForm") or entry.get("active_form")
        # Anything outside the canonical 3 statuses falls back to ☐.
        if status == "completed":
            mark = "☑"
            done += 1
        elif status == "in_progress":
            mark = "▣"
            if in_progress_idx is None:
                in_progress_idx = i
        else:
            mark = "☐"
        # Prefer activeForm for the in-flight row; everything else uses
        # the imperative content.
        if status == "in_progress" and isinstance(active, str) and active.strip():
            text = active.strip()
        elif isinstance(content, str) and content.strip():
            text = content.strip()
        elif isinstance(active, str) and active.strip():
            text = active.strip()
        else:
            text = "(unnamed)"
        text = text.replace("\n", " ")
        if len(text) > _TODO_LINE_CHARS:
            text = text[: _TODO_LINE_CHARS - 1] + "…"
        rows.append((mark, text))

    total = len(rows)
    header = f"📋 任务清单 ({done}/{total}):"

    # Slice for overflow. If total > max_lines we show max_lines-1 rows +
    # a "… +N more" summary. Keep the in-progress row visible: if it
    # would otherwise be hidden, swap it into the last visible slot.
    if total > max_lines:
        keep = max_lines - 1
        visible_indices = list(range(keep))
        if (
            in_progress_idx is not None
            and in_progress_idx >= keep
        ):
            # Swap the active row into the last visible slot so the user
            # always sees what's running RIGHT NOW.
            visible_indices[-1] = in_progress_idx
        body_lines = [f"{rows[i][0]} {rows[i][1]}" for i in visible_indices]
        body_lines.append(f"… +{total - keep} more")
    else:
        body_lines = [f"{m} {t}" for m, t in rows]

    out = "\n".join([header, *body_lines])
    if len(out) > _TODO_BLOCK_CHARS:
        # Hard cap with a clear marker so the truncation is observable.
        out = out[: _TODO_BLOCK_CHARS - 1] + "…"
    return out


def format_tool_status(ev: Any) -> str:
    """Render a ``ToolCallEvent`` as the next mutable-line status.

    Mirrors hermes-agent's ``_last_activity_desc`` style ("emoji + label +
    arg preview"). Truncates / sanitises so a runaway tool name can't blow
    past channel-specific message-length caps when fed into the edit.

    The ``todo_write`` tool is special-cased: instead of the generic
    ``🔧 todo_write 5 item(s)`` line we render the full checkbox list
    (mutable in-place across the turn) via :func:`format_todo_list`.

    The ``ask_user`` tool is similarly special-cased — we render
    ``❓ 等待用户回答: <question>`` plus a bulleted list of options when
    present via :func:`format_ask_user`. The matching ``tool_result`` is
    suppressed by :func:`format_tool_result` (the question line IS the
    user-facing signal; the model's assistant text will repeat the
    question as the turn's final reply).
    """
    tool = (getattr(ev, "tool", "") or "?").replace("\n", " ")
    if tool == TODO_WRITE_TOOL:
        rendered = format_todo_list(getattr(ev, "args_json", b""))
        if rendered:
            return rendered
        # Fall through to the generic rendering if parsing failed —
        # better to show *something* than to silently hide the call.
    if tool == ASK_USER_TOOL:
        rendered = format_ask_user(getattr(ev, "args_json", b""))
        if rendered:
            return rendered
        # Same fall-through policy — never silently hide a tool call.
    plugin = (getattr(ev, "plugin", "") or "").replace("\n", " ")
    label = f"{plugin}.{tool}" if plugin and plugin != tool else tool
    if len(label) > 60:
        label = label[:57] + "..."
    preview = tool_arg_preview(tool, getattr(ev, "args_json", b""))
    if preview:
        return f"🔧 {label}  {preview}"
    return f"🔧 调用工具: {label}"


def format_elapsed_ms(elapsed_ms: int) -> str:
    """Human-friendly elapsed-time formatter for status lines.

    * < 1000 ms → ``"500ms"``
    * < 60 s → ``"3s"`` / ``"12s"``
    * >= 60 s → ``"1m23s"`` / ``"10m05s"``

    Negative / zero inputs render as ``"0s"`` so a clock skew never
    surfaces a confusing ``-2s`` to the user.
    """
    ms = max(0, int(elapsed_ms))
    if ms < 1000:
        return f"{ms}ms"
    seconds = ms // 1000
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    return f"{minutes}m{secs:02d}s"


def format_tool_heartbeat(name: str, elapsed_ms: int) -> str:
    """Render a :class:`ToolStateHeartbeat` as the next mutable-spinner line.

    Shape: ``🔧 {name} … {elapsed}`` — same emoji as
    :func:`format_tool_status` so the spinner stays visually stable as a
    long-running tool ticks through its heartbeat updates. ``name`` is
    sanitised the same way ``format_tool_status`` sanitises the label
    so a runaway tool name can't blow past channel-specific message-
    length caps.

    Spec: §1.4 of ``docs/PLAN_TASK_OBSERVABILITY.md`` — keeps users
    informed for 60s+ tools that would otherwise look stuck.
    """
    label = (name or "?").replace("\n", " ")
    if len(label) > 60:
        label = label[:57] + "..."
    return f"🔧 {label} … {format_elapsed_ms(elapsed_ms)}"


def format_turn_footer(
    elapsed_ms: int,
    tool_calls: int,
    estimated_cost_usd: float | None,
    cost_status: str | None,
) -> str:
    """Render the post-turn observability footer (W4.1).

    Shape (one line, parenthesised so it can be appended to any reply
    without disrupting the model's paragraph structure)::

        (elapsed: 12s · 3 tool calls · ~$0.0120)

    Field rules:

    * ``elapsed_ms`` always rendered via :func:`format_elapsed_ms`.
    * ``tool_calls`` omitted when ``0``; singular ``"1 tool call"`` /
      plural ``"N tool calls"``.
    * Cost omitted when ``None`` or ``<= 0`` (a zero-cost turn carries no
      signal and would be confusing — see W4.1 spec).
    * Cost prefixed ``~`` when ``cost_status`` is ``"estimated"`` /
      ``"unknown"`` / ``None``; bare otherwise (``"calculated"`` /
      ``"actual"`` etc.). The ``~`` flags "best-effort number, not an
      invoice", mirroring the gateway's ``_CostMeter`` semantics.

    Spec: §1.4 of ``docs/PLAN_TASK_OBSERVABILITY.md`` — the channel
    adapter (Telegram / QQ / Discord / Slack / Feishu) appends this
    line to the final reply on ``TurnComplete``. Channels whose
    message-length cap would push the reply over the limit drop the
    footer gracefully via :func:`try_append_footer`.
    """
    parts = [f"elapsed: {format_elapsed_ms(elapsed_ms)}"]
    if tool_calls > 0:
        parts.append(
            f"{tool_calls} tool call{'s' if tool_calls != 1 else ''}"
        )
    if estimated_cost_usd is not None and estimated_cost_usd > 0:
        prefix = "~" if cost_status in ("estimated", "unknown", None) else ""
        parts.append(f"{prefix}${estimated_cost_usd:.4f}")
    return f"({' · '.join(parts)})"


def try_append_footer(message: str, footer: str, limit: int = TEXT_LIMIT) -> str:
    """Append ``footer`` to ``message`` on a fresh line iff it fits.

    Used by every channel adapter to attach the post-turn footer (W4.1).
    The ``\\n\\n{footer}`` separator keeps the footer visually distinct
    from the model's prose; the footer is dropped silently when the
    composed body would exceed ``limit`` so a near-cap reply doesn't
    lose user-facing content for a decorative observability line.

    ``footer`` empty → returns ``message`` unchanged (no trailing
    whitespace, no separator).
    """
    if not footer:
        return message
    composed = f"{message}\n\n{footer}" if message else footer
    if len(composed) > limit:
        return message
    return composed


def format_tool_result(ev: Any) -> str:
    """Render a ``ToolResultEvent`` as a "tool finished" line.

    ✅ for success, ❌ for error. Duration is human-friendly (ms < 1s,
    seconds otherwise). Mirrors hermes-agent's
    ``tool_progress_callback("tool.completed", duration=..., is_error=...)``
    rendering.

    Returns an empty string for ``todo_write`` (same suppression as
    ``send_attachment``): the call-side already rendered the full list
    and a trailing ``✅ todo_write (3ms)`` line would just clutter the
    spinner. Same suppression applies to ``ask_user``: the question line
    rendered on the call side is the user-facing signal, and a follow-up
    ``✅ ask_user (1ms)`` line would just overwrite it.
    """
    tool = (getattr(ev, "tool", "") or "?").replace("\n", " ")
    if tool == TODO_WRITE_TOOL:
        return ""
    if tool == ASK_USER_TOOL:
        return ""
    if len(tool) > 60:
        tool = tool[:57] + "..."
    dur_ms = int(getattr(ev, "duration_ms", 0) or 0)
    dur = f"{dur_ms}ms" if dur_ms < 1000 else f"{dur_ms / 1000:.1f}s"
    if getattr(ev, "is_error", False):
        msg = (getattr(ev, "error_summary", "") or "").replace("\n", " ")
        msg = msg[:80] + "…" if len(msg) > 80 else msg
        return f"❌ {tool} 失败 ({dur}){': ' + msg if msg else ''}"
    return f"✅ {tool} ({dur})"


#: Cap on the question text rendered into the spinner / op-summary
#: line. Mirrors the agent-side cap in
#: :mod:`corlinman_agent.interactive.ask_user` so the same value
#: round-trips between the two layers without surprise truncation.
_ASK_USER_QUESTION_CHARS: int = 400

#: Per-option label cap when rendering as a bulleted list. Telegram's
#: inline-keyboard ``callback_data`` is 64 bytes; the visible button
#: text can be longer, so we keep the renderer cap a bit looser for the
#: textual fallback path.
_ASK_USER_OPTION_CHARS: int = 80


def parse_ask_user_args(args_json: bytes | str) -> tuple[str, list[str], bool]:
    """Parse ``ask_user`` tool args into ``(question, options, multiple)``.

    Pure: never raises and never touches I/O. Returns ``("", [], False)``
    on any decode / shape error so the caller can branch on the empty
    question string and skip rendering.

    Used by:

    * :func:`format_ask_user` for the spinner / op-summary text block.
    * The Telegram handler (in :mod:`service`) when building the inline
      keyboard for the final reply.
    """
    raw = args_json or b""
    try:
        raw_str = (
            raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        )
        obj = _json.loads(raw_str or "{}")
    except (UnicodeDecodeError, _json.JSONDecodeError):
        return ("", [], False)
    if not isinstance(obj, dict):
        return ("", [], False)
    q_raw = obj.get("question")
    question = q_raw.strip() if isinstance(q_raw, str) else ""
    opts_raw = obj.get("options") or []
    options: list[str] = []
    if isinstance(opts_raw, list):
        for o in opts_raw[:8]:
            label = str(o).replace("\n", " ").strip()
            if not label:
                continue
            if len(label) > _ASK_USER_OPTION_CHARS:
                label = label[: _ASK_USER_OPTION_CHARS - 1] + "…"
            options.append(label)
    multiple = bool(obj.get("multiple", False))
    return (question, options, multiple)


def format_ask_user(args_json: bytes | str) -> str:
    """Render ``ask_user`` args as the spinner / op-summary block.

    Output shape::

        ❓ 等待用户回答: <question>
          · option A
          · option B
          · option C

    Returns ``""`` on parse failure / empty question so the caller can
    fall back to the generic ``🔧 ask_user`` spinner line. Question is
    truncated at :data:`_ASK_USER_QUESTION_CHARS` to keep the spinner
    line under Telegram's ``editMessageText`` cap on long questions.
    """
    question, options, _multiple = parse_ask_user_args(args_json)
    if not question:
        return ""
    q = question.replace("\n", " ")
    if len(q) > _ASK_USER_QUESTION_CHARS:
        q = q[: _ASK_USER_QUESTION_CHARS - 1] + "…"
    lines = [f"❓ 等待用户回答: {q}"]
    for opt in options:
        lines.append(f"  · {opt}")
    return "\n".join(lines)


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


def _agent_workspace_root() -> _Path:
    """Mirror of :func:`corlinman_agent.coding._common.resolve_workspace`.

    Inlined here instead of imported so this package doesn't depend on
    ``corlinman_agent``'s private module. Keep the env-var fallback chain
    in lockstep with the agent side or files written by ``write_file``
    won't be found by :func:`resolve_attachment_path`.
    """
    env_ws = _os.environ.get("CORLINMAN_AGENT_WORKSPACE")
    if env_ws:
        root = _Path(env_ws)
    else:
        data_dir = _os.environ.get("CORLINMAN_DATA_DIR")
        base = _Path(data_dir) if data_dir else _Path.home() / ".corlinman"
        root = base / "workspace"
    return root.resolve()


def resolve_attachment_path(path_str: str) -> _Path | None:
    """Resolve a ``send_attachment`` ``path`` argument to a real file.

    The reasoning loop typically writes files via ``write_file``, which
    confines paths to the agent workspace (``~/.corlinman/workspace`` by
    default). Models frequently reuse the same *relative* path when
    calling ``send_attachment`` — so resolving the path against the
    process cwd (``Path(path_str)``) almost always misses, even when the
    file exists in the workspace.

    Resolution order (first hit wins):

    1. **Absolute path that exists on disk** — used as-is. The agent
       already has ``run_shell`` and could read arbitrary files
       anyway, so this isn't a new exfiltration vector — and matching
       the pre-fix permissive behaviour keeps callers that pass
       absolute paths from regressing.
    2. **Relative path joined with the workspace root** — fixes the
       common ``write_file("x.html") + send_attachment("x.html")`` flow
       where the gateway's cwd ≠ workspace.
    3. **Workspace + basename** — last-ditch for an absolute path
       whose dirname is wrong but whose basename matches a workspace
       file (e.g. model hallucinates ``/tmp/whatever/x.html``).

    Returns the resolved :class:`Path` when a regular file is found,
    else ``None`` — callers render a friendly status. Empty / blank
    input also returns ``None`` so the upstream "missing path" branch
    wins over a confusing "not found" message.
    """
    if not path_str or not path_str.strip():
        return None
    workspace = _agent_workspace_root()
    raw = _Path(path_str)

    candidates: list[_Path] = []
    if raw.is_absolute():
        candidates.append(raw)
        candidates.append(workspace / raw.name)
    else:
        candidates.append((workspace / raw).resolve())
        if raw.name and raw.name != path_str:
            candidates.append(workspace / raw.name)

    for cand in candidates:
        try:
            if cand.is_file():
                return cand
        except OSError:
            continue
    return None


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
        "_last_ask_user_args",
        "_last_op_status",
        "_last_status",
        "_last_todo_args",
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
        #: The most recent ``todo_write`` args bytes seen this turn — the
        #: full list snapshot, ready to be re-rendered by callers that
        #: want to prepend it to a final summary (QQ / QQ-official /
        #: WeChat-official). Reset to ``None`` between turns.
        self._last_todo_args: bytes | None = None
        #: The most recent NON-todo "operation flow" status line — a
        #: tool-call (``🔧 web_search 'x'``), tool-result (``✅ … (200ms)``),
        #: or reasoning-delta line (``💭 推理: …``). Used by
        #: :meth:`_render_combined` to append the live op flow UNDER the
        #: todo list so users see both "what's planned" and "what's
        #: firing right now". Cleared to ``None`` when a real (non-
        #: reasoning) ``token_delta`` arrives — the operation is over,
        #: the response is coming.
        self._last_op_status: str | None = None
        #: Raw JSON args of the most recent ``ask_user`` call this turn.
        #: ``None`` when the agent never called ``ask_user``. The Telegram
        #: handler reads this at end-of-turn to decide whether to attach
        #: an inline keyboard to the final reply; QQ-family channels
        #: ignore it (the bulleted options already render via the
        #: spinner / summary path).
        self._last_ask_user_args: bytes | None = None

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

    @property
    def last_todo_args(self) -> bytes | None:
        """Raw JSON args of the most recent ``todo_write`` call this turn.

        Channels that prepend a post-hoc summary block (QQ / QQ-official /
        WeChat-official, which can't edit messages live) read this at
        end-of-turn and feed it back through :func:`format_todo_list` so
        the user sees the FINAL list snapshot above the activity log.
        ``None`` when the agent never called ``todo_write``.
        """
        return self._last_todo_args

    @property
    def last_ask_user_args(self) -> bytes | None:
        """Raw JSON args of the most recent ``ask_user`` call this turn.

        The Telegram handler reads this at end-of-turn to decide whether
        to send the final reply with an inline-keyboard of option
        buttons; other channels can ignore it (their bulleted-list
        rendering already lives in the spinner / op-summary block).
        ``None`` when the agent never called ``ask_user``.
        """
        return self._last_ask_user_args

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

    def _build_combined(self) -> str | None:
        """Compose the visible spinner text from todo + op-status state.

        Layout when both are set::

            📋 任务清单 (1/3):
            ☑ Search market data
            ▣ Drafting decision memo
            ☐ Send final files

            🔧 web_search 'gpt-5.5 news'

        A blank line separates the two blocks so users can parse "what's
        planned" from "what's firing right now" at a glance. When only
        the todo list is set, the bare list renders alone. When only the
        op-status is set, the bare op line renders alone. When neither
        is set, returns ``None`` — caller falls back to whatever bare
        status it intended to emit (e.g. :data:`STATUS_GENERATING`,
        :data:`STATUS_THINKING`).

        The combined block honours the same 1500-char todo cap (already
        applied by :func:`format_todo_list`) and the overall 4000-char
        :data:`TEXT_LIMIT` so the result never overshoots Telegram's
        ``editMessageText`` hard limit.
        """
        todo = self._last_todo_args
        op = self._last_op_status
        todo_block = ""
        if todo:
            todo_block = format_todo_list(todo)
        if todo_block and op:
            combined = f"{todo_block}\n\n{op}"
        elif todo_block:
            combined = todo_block
        elif op:
            combined = op
        else:
            return None
        return truncate_reply(combined)

    async def _render_combined(self) -> None:
        """Emit the combined view via :meth:`_maybe_edit`.

        No-op when neither state is populated (the caller is expected to
        fall through to an explicit ``_maybe_edit`` of a bare status
        like :data:`STATUS_GENERATING`).
        """
        text = self._build_combined()
        if text is None:
            return
        await self._maybe_edit(text)

    async def on_token_delta(self, text: str, is_reasoning: bool) -> None:
        """Handle one ``token_delta`` event.

        * Reasoning deltas (Anthropic ``thinking``, DeepSeek-R1
          ``reasoning_content``) render as 💭 lines but are *not*
          accumulated into the final reply. When a todo list is in
          flight the reasoning line appears UNDER it via
          :meth:`_render_combined`, so the user sees both "what's
          planned" and "what's currently being thought through".
        * Non-reasoning deltas are appended to ``text_parts`` and, on
          the first one, switch the status to ✍️ STATUS_GENERATING.
          The op-flow status is cleared (the operation is over, the
          response is coming) and the GENERATING transition is emitted
          BARE — not appended under the todo list, since at this point
          the user wants the answer, not a stale "what's planned" view.
        """
        if is_reasoning:
            stripped = text.strip()
            if not stripped:
                return
            snippet = stripped.replace("\n", " ")
            if len(snippet) > REASONING_PREVIEW_CHARS:
                snippet = snippet[: REASONING_PREVIEW_CHARS - 1] + "…"
            self._last_op_status = f"{STATUS_REASONING_PREFIX}{snippet}"
            await self._render_combined()
            return
        self._text_parts.append(text)
        # First real token: clear the op flow and switch to GENERATING.
        # The transition is emitted bare (no todo overlay) because the
        # answer is what the user wants to see now; the planning context
        # would just push the visible reply down a screen.
        self._last_op_status = None
        if self._last_status != STATUS_GENERATING:
            await self._maybe_edit(STATUS_GENERATING)

    async def on_tool_call(self, ev: Any) -> str | None:
        """Handle one ``tool_call`` event.

        For ``send_attachment``: if the channel registered a handler, call
        it (the handler does the actual upload) and render the returned
        status line. Returns the literal string ``"intercept"`` to signal
        the caller that the matching ``tool_result`` should be suppressed
        (the 📎 status already conveys completion).

        For ``todo_write``: stash the args bytes (so end-of-turn summary
        builders can re-render the final list) and edit the spinner with
        the combined view — the rendered checkbox list plus, if a
        previous non-todo tool is still in flight, the live op line
        underneath. The matching ``tool_result`` is also suppressed —
        the list IS the status; a trailing ``✅ todo_write`` would just
        clutter the line.

        For everything else: stash the rendered ``format_tool_status``
        line as the current op flow and emit the combined view via
        :meth:`_render_combined`. Returns ``None``.
        """
        tool = getattr(ev, "tool", "") or ""
        if tool == SEND_ATTACHMENT_TOOL and self._send_attachment_handler is not None:
            status = await self._send_attachment_handler(ev)
            # The dedicated 📎 status doubles as the op-flow line —
            # subsequent reasoning / todo-write events that re-render
            # the combined view should keep it visible until the next
            # tool fires.
            self._last_op_status = status
            await self._maybe_edit(status)
            return "intercept"
        if tool == TODO_WRITE_TOOL:
            # Normalise args to bytes so the end-of-turn summary builder
            # can hand them straight back to ``format_todo_list``. The
            # tool dispatcher always sends bytes, but be defensive in
            # case a unit test feeds a str.
            raw = getattr(ev, "args_json", b"") or b""
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            self._last_todo_args = raw
            await self._render_combined()
            return None
        if tool == ASK_USER_TOOL:
            # Stash the args bytes so the Telegram handler can read the
            # options at end-of-turn and attach an inline keyboard. Then
            # render the question as the live op-flow status line so the
            # spinner shows "❓ 等待用户回答: ..." until the assistant
            # text replaces the placeholder.
            raw = getattr(ev, "args_json", b"") or b""
            if isinstance(raw, str):
                raw = raw.encode("utf-8")
            self._last_ask_user_args = raw
            self._last_op_status = format_tool_status(ev)
            await self._render_combined()
            return None
        # Non-todo, non-attachment tool: this IS the live op flow now.
        self._last_op_status = format_tool_status(ev)
        await self._render_combined()
        return None

    async def on_tool_heartbeat(self, tool_name: str, elapsed_ms: int) -> None:
        """Handle one :class:`ToolStateHeartbeat` event (W3.1).

        Refreshes the live op-flow line with ``🔧 {tool} … {elapsed}``
        so the user sees a long-running tool ticking forward instead of
        a stale ``🔧 run_shell  pytest -x`` for 60+ seconds. Composes
        the same combined-view layout as :meth:`on_tool_call` so a
        heartbeat under an in-progress todo list refreshes only the
        op-flow portion.

        ``tool_name`` should match the spinner's most recently-shown
        tool; if no tool is currently displayed (e.g. the spinner already
        flipped to STATUS_GENERATING) the heartbeat is silently
        suppressed — the user is reading the model's answer, not
        the tool flow.
        """
        # If GENERATING is showing, the response is in flight and we
        # explicitly do NOT want to clobber it with a tool heartbeat —
        # see ``on_token_delta`` for the matching design comment.
        if self._last_status == STATUS_GENERATING:
            return
        # If no op flow has been shown this turn yet, suppress —
        # heartbeats arriving before the matching ToolStateRunning are
        # an out-of-order anomaly the consumer shouldn't surface.
        if self._last_op_status is None:
            return
        self._last_op_status = format_tool_heartbeat(tool_name, elapsed_ms)
        await self._render_combined()

    async def on_cancelling(self) -> None:
        """Handle one :class:`Cancelling` event (W3.1).

        Flips the spinner to :data:`STATUS_CANCELLING`
        (``⏹ 正在取消…``) the moment cancellation is requested,
        without waiting for the round-boundary ``TurnErrored`` to
        propagate. Bare emit — no combined-view layout — so the user
        immediately sees the system has acknowledged their cancel and
        isn't ignoring them.
        """
        await self._maybe_edit(STATUS_CANCELLING)

    async def on_tool_result(self, ev: Any) -> None:
        """Handle one ``tool_result`` event.

        Renders ✅ / ❌ + human duration as the new op-flow line, then
        re-emits the combined view so the result appears under the todo
        list (if any). ``send_attachment`` completions are suppressed
        because :meth:`on_tool_call` already rendered the dedicated 📎
        line — re-overwriting it with ✅ would lose the useful
        "📎 已发送文件: X" status for ~zero gain.

        ``todo_write`` completions are likewise suppressed — the list
        rendered on the call side IS the user-visible signal that the
        plan changed; a follow-up ``✅ todo_write (3ms)`` would only
        overwrite it with strictly less information. The spinner does
        NOT need to revert to the previous status — the list IS the
        status. The next ``tool_call`` for a different tool will
        naturally overwrite it.
        """
        tool = getattr(ev, "tool", "") or ""
        if tool == SEND_ATTACHMENT_TOOL:
            return
        if tool == TODO_WRITE_TOOL:
            return
        if tool == ASK_USER_TOOL:
            # Same suppression as todo_write — the question rendered on
            # the call side is the user-facing signal; overwriting it
            # with ``✅ ask_user (1ms)`` would lose useful context. The
            # next call (or the assistant's final reply) will naturally
            # supersede.
            return
        self._last_op_status = format_tool_result(ev)
        await self._render_combined()
