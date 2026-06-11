"""Terminal renderer — streams a turn's events into a rich console.

hermes-agent display semantics, simplified:

* assistant text streams token-by-token (no buffering — perceived
  latency is the whole point of streaming);
* tool calls render as dim one-liners with an args preview, finished
  calls add duration and ✓/✗ (``ToolFinished`` only arrives on the
  embedded path — attach mode shows starts only, by wire contract);
* ``todo_write`` calls render as a checklist block (claude-code's
  ``TodoWrite`` live list) instead of a raw tool line — one marker per
  item, identical consecutive lists are not repainted;
* reasoning deltas are hidden unless ``tool_progress="verbose"``;
* a dim status line (model · session · tokens · elapsed) closes the
  turn — claude-code's cost/status footer.

``tool_progress`` modes mirror hermes: ``off`` (silent), ``new`` (skip
consecutive repeats of the same tool), ``all``, ``verbose``.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from corlinman_server.console.events import (
    ConsoleEvent,
    ReasoningDelta,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnDone,
    TurnError,
    args_preview,
)

if TYPE_CHECKING:
    from rich.console import Console

__all__ = ["TODO_TOOL_NAMES", "TOOL_PROGRESS_MODES", "Renderer"]

TOOL_PROGRESS_MODES = ("off", "new", "all", "verbose")

#: Builtin task-list tool names rendered as a live checklist instead of
#: the generic "◐ tool" line. Mirrors
#: ``corlinman_agent.coding.todo.TODO_WRITE_TOOL``; kept as a literal so
#: attach-only consoles never import the agent package. Other surfaces
#: (web chat, channels) should import this constant rather than
#: hard-coding the name.
TODO_TOOL_NAMES: frozenset[str] = frozenset({"todo_write"})

_TODO_STATUSES = ("pending", "in_progress", "completed")

#: status → (marker, rich style) for one checklist line.
_TODO_MARKS: dict[str, tuple[str, str]] = {
    "pending": ("☐", "dim"),
    "in_progress": ("◐", "bold cyan"),
    "completed": ("☒", "green"),
}


def _parse_todo_items(args_json: bytes | str) -> list[tuple[str, str]] | None:
    """``todo_write`` args → ``[(status, display_text)]``, or ``None``
    when the args are malformed/empty (caller falls back to the generic
    tool line). ``in_progress`` items display their present-continuous
    ``activeForm`` when the model supplied one."""
    raw = args_json.decode("utf-8", "replace") if isinstance(args_json, bytes) else args_json
    try:
        obj = json.loads(raw) if raw.strip() else None
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    todos = obj.get("todos")
    if not isinstance(todos, list) or not todos:
        return None
    items: list[tuple[str, str]] = []
    for entry in todos:
        if not isinstance(entry, dict):
            return None
        content = entry.get("content")
        status = entry.get("status")
        if not isinstance(content, str) or not content.strip():
            return None
        if status not in _TODO_STATUSES:
            return None
        text = content.strip()
        if status == "in_progress":
            active = entry.get("activeForm") or entry.get("active_form")
            if isinstance(active, str) and active.strip():
                text = active.strip()
        items.append((status, text))
    return items


class Renderer:
    """Stateful per-app renderer; ``start_turn()`` resets turn state."""

    def __init__(
        self,
        console: Console,
        *,
        tool_progress: str = "new",
        show_reasoning: bool = False,
    ) -> None:
        self.console = console
        self.tool_progress = tool_progress
        self.show_reasoning = show_reasoning
        self._turn_started_at = 0.0
        self._last_tool: str | None = None
        self._text_open = False  # mid-stream, cursor not at line start
        # Last rendered todo checklist. Deliberately *not* reset by
        # start_turn(): the agent's list survives across turns (the
        # TodoStore is session-scoped), so an unchanged list re-sent
        # next turn is still noise we skip — claude-code parity.
        self._last_todos: tuple[tuple[str, str], ...] | None = None

    # ── turn lifecycle ────────────────────────────────────────────────

    def start_turn(self) -> None:
        self._turn_started_at = time.monotonic()
        self._last_tool = None
        self._text_open = False

    def on_event(self, ev: ConsoleEvent, *, model: str, session_key: str) -> None:
        if isinstance(ev, TextDelta):
            self._stream_text(ev.text)
        elif isinstance(ev, ReasoningDelta):
            if self.show_reasoning or self.tool_progress == "verbose":
                self._break_line()
                self.console.print(ev.text, style="dim italic", end="")
                self._text_open = True
        elif isinstance(ev, ToolStarted):
            self._on_tool_started(ev)
        elif isinstance(ev, ToolFinished):
            self._on_tool_finished(ev)
        elif isinstance(ev, TurnDone):
            self._on_done(ev, model=model, session_key=session_key)
        elif isinstance(ev, TurnError):
            self._on_error(ev)

    # ── pieces ────────────────────────────────────────────────────────

    def _stream_text(self, text: str) -> None:
        # Raw write keeps token latency at zero — rich markup parsing on
        # every delta is wasted work and can mangle partial markup.
        self.console.file.write(text)
        self.console.file.flush()
        self._text_open = not text.endswith("\n")

    def _break_line(self) -> None:
        if self._text_open:
            self.console.file.write("\n")
            self.console.file.flush()
            self._text_open = False

    def _tool_label(self, ev: ToolStarted | ToolFinished) -> str:
        return f"{ev.plugin}:{ev.tool}" if ev.plugin else ev.tool

    def _on_tool_started(self, ev: ToolStarted) -> None:
        if self.tool_progress == "off":
            return
        if not ev.plugin and ev.tool in TODO_TOOL_NAMES:
            if self._on_todo_started(ev):
                return
            # Malformed args — fall through to the generic tool line.
        label = self._tool_label(ev)
        if self.tool_progress == "new" and label == self._last_tool:
            return
        self._last_tool = label
        preview = args_preview(ev.args_json)
        self._break_line()
        line = f"◐ {label}" + (f"  {preview}" if preview else "")
        self.console.print(line, style="dim")
        if self.tool_progress == "verbose" and ev.args_json:
            self.console.print(ev.args_json.decode("utf-8", "replace"), style="dim")

    def _on_todo_started(self, ev: ToolStarted) -> bool:
        """Render a ``todo_write`` call as a checklist block.

        Returns ``True`` when the event was consumed (rendered, or
        skipped because the list is identical to the last one shown);
        ``False`` when the args were malformed and the caller should
        fall back to the generic tool line.
        """
        items = _parse_todo_items(ev.args_json)
        if items is None:
            return False
        # Keep the "new"-mode repeat bookkeeping coherent for whatever
        # tool comes after the checklist.
        self._last_tool = self._tool_label(ev)
        key = tuple(items)
        if key == self._last_todos:
            return True  # unchanged list — don't repaint
        self._last_todos = key
        self._break_line()
        for status, text in items:
            mark, style = _TODO_MARKS[status]
            self.console.print(f"{mark} {text}", style=style, highlight=False)
        return True

    def _on_tool_finished(self, ev: ToolFinished) -> None:
        if self.tool_progress == "off":
            return
        label = self._tool_label(ev)
        secs = ev.duration_ms / 1000.0
        self._break_line()
        if ev.is_error:
            summary = f"  {ev.error_summary}" if ev.error_summary else ""
            self.console.print(f"✗ {label} ({secs:.1f}s){summary}", style="red dim")
        else:
            self.console.print(f"✓ {label} ({secs:.1f}s)", style="green dim")

    def _on_done(self, ev: TurnDone, *, model: str, session_key: str) -> None:
        self._break_line()
        elapsed = time.monotonic() - self._turn_started_at
        parts = [model, session_key, f"{elapsed:.1f}s"]
        if ev.total_tokens:
            parts.append(f"{ev.total_tokens} tok")
        self.console.print("  ·  ".join(parts), style="dim", highlight=False)

    def _on_error(self, ev: TurnError) -> None:
        self._break_line()
        if ev.is_cancelled:
            self.console.print("⏹ interrupted", style="yellow")
        else:
            self.console.print(f"✗ [{ev.reason}] {ev.message}", style="bold red", highlight=False)
