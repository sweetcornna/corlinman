"""Terminal renderer — streams a turn's events into a rich console.

Two presentation paths share one event reducer:

* **rich UI** (``rich_ui=True``, auto-enabled on an interactive TTY) —
  claude-code-grade: a working **spinner** while the model thinks / a tool
  runs, assistant text rendered as live **Markdown** (headings, lists,
  code blocks with syntax highlight), and tool calls shown as
  ``⏺ tool(args)`` / ``⎿ result`` blocks. One ``rich.live.Live`` is active
  at a time; any failure trips ``rich_ui`` off and re-renders the event on
  the raw path, so a terminal quirk can never strand the REPL.
* **raw** (``rich_ui=False``, non-TTY / ``--print`` / piped) — the original
  hermes-style path: tokens streamed verbatim (zero latency, arbitrary
  length), tool calls as dim one-liners, a dim status footer. Unchanged so
  scripted/JSON consumers and the test-suite keep byte-stable output.

In both: ``todo_write`` renders as a live checklist; reasoning deltas are
hidden unless ``tool_progress="verbose"``; the turn closes with a
``model · session · tokens · elapsed`` footer.

``tool_progress`` modes mirror hermes: ``off`` (silent), ``new`` (skip
consecutive repeats of the same tool), ``all``, ``verbose``.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

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

#: How often (seconds) the live Markdown is re-parsed while streaming. The
#: Live widget repaints on its own clock; this only throttles the
#: comparatively expensive ``Markdown(buf)`` re-parse so a fast token
#: stream doesn't re-parse the growing buffer on every single delta. Each
#: re-parse is O(buffer length), so on a long reply the per-frame cost grows
#: — ``_MARKDOWN_LIVE_MAX_CHARS`` caps it.
_MARKDOWN_REPARSE_INTERVAL = 0.08

#: Above this assistant-block size we stop the per-frame live re-parse — the
#: live region freezes at the last render and the single final ``Markdown``
#: render at block end (``_flush_text_block``) shows the whole thing. Bounds
#: the worst-case O(n²) re-parse cost a pathologically long reply would
#: otherwise drive (n re-parses of an n-char buffer).
_MARKDOWN_LIVE_MAX_CHARS = 20_000

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


class _Working:
    """A live spinner + label + elapsed-seconds renderable.

    Re-renders every Live refresh so the spinner animates and the elapsed
    counter ticks. ``Spinner.render(t)`` returns a :class:`rich.text.Text`
    frame for wall-clock ``t``.
    """

    def __init__(self, label: str, start: float) -> None:
        from rich.spinner import Spinner  # noqa: PLC0415

        self._label = label
        self._start = start
        self._spinner = Spinner("dots", style="cyan")

    def set_label(self, label: str) -> None:
        self._label = label

    def __rich_console__(self, console: Any, options: Any) -> Any:
        from rich.text import Text  # noqa: PLC0415

        elapsed = time.monotonic() - self._start
        line = Text()
        line.append_text(self._spinner.render(time.monotonic()))
        line.append(f" {self._label} ", style="cyan")
        line.append(f"{elapsed:.0f}s · Ctrl-C 中断", style="dim")
        yield line


class Renderer:
    """Stateful per-app renderer; ``start_turn()`` resets turn state."""

    def __init__(
        self,
        console: Console,
        *,
        tool_progress: str = "new",
        show_reasoning: bool = False,
        rich_ui: bool | None = None,
    ) -> None:
        self.console = console
        self.tool_progress = tool_progress
        self.show_reasoning = show_reasoning
        # rich UI auto-enables on an interactive terminal; callers may force
        # it either way. Non-TTY (pipes, --print, CI) → raw path.
        if rich_ui is None:
            rich_ui = bool(getattr(console, "is_terminal", False))
        self.rich_ui = rich_ui
        self._turn_started_at = 0.0
        self._last_tool: str | None = None
        self._text_open = False  # mid-stream, cursor not at line start
        # Last rendered todo checklist. Deliberately *not* reset by
        # start_turn(): the agent's list survives across turns (the
        # TodoStore is session-scoped), so an unchanged list re-sent
        # next turn is still noise we skip — claude-code parity.
        self._last_todos: tuple[tuple[str, str], ...] | None = None
        # ── rich-path live state ──
        self._live: Any | None = None  # the single active rich.live.Live
        self._live_kind: str | None = None  # "spin" | "text"
        self._working: _Working | None = None  # spinner renderable (kind=spin)
        self._buf = ""  # current assistant Markdown block buffer
        self._last_md_at = 0.0  # last Markdown re-parse time (throttle)

    # ── turn lifecycle ────────────────────────────────────────────────

    def start_turn(self) -> None:
        self._turn_started_at = time.monotonic()
        self._last_tool = None
        self._text_open = False
        self._buf = ""
        if self.rich_ui:
            try:
                self._spin("思考中…")
            except Exception:  # noqa: BLE001 — degrade to raw for this turn
                self._fallback_to_raw()

    def finish_turn(self) -> None:
        """Tear any live widget down — the caller's per-turn ``finally``
        safety net. ``TurnDone`` / ``TurnError`` already stop the Live on a
        normal turn; this catches the path where the event stream raises
        before a terminal event, which would otherwise leave a spinner /
        markdown Live running into the next prompt and corrupt the
        terminal. Idempotent, never raises."""
        try:
            self._stop_live()
        except Exception:  # noqa: BLE001 — best-effort teardown
            self._live = None
            self._live_kind = None
            self._working = None

    def on_event(self, ev: ConsoleEvent, *, model: str, session_key: str) -> None:
        if self.rich_ui:
            try:
                self._on_event_rich(ev, model=model, session_key=session_key)
                return
            except Exception:  # noqa: BLE001 — never let rendering crash a turn
                self._fallback_to_raw()
                # fall through and render THIS event on the raw path
        self._on_event_raw(ev, model=model, session_key=session_key)

    # ── raw path (verbatim hermes behaviour; non-TTY / fallback) ──────

    def _on_event_raw(
        self, ev: ConsoleEvent, *, model: str, session_key: str
    ) -> None:
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
            # markup=False: the ``[reason]`` brackets must render literally,
            # not be parsed as a rich style tag (which silently ate the
            # reason for any non-style word like ``[rate_limit]``).
            self.console.print(
                f"✗ [{ev.reason}] {ev.message}",
                style="bold red",
                highlight=False,
                markup=False,
            )

    # ── rich path (TTY: spinner + live markdown + tool blocks) ────────

    def _on_event_rich(
        self, ev: ConsoleEvent, *, model: str, session_key: str
    ) -> None:
        if isinstance(ev, TextDelta):
            self._rich_text(ev.text)
        elif isinstance(ev, ReasoningDelta):
            if self.show_reasoning or self.tool_progress == "verbose":
                self._spin_label("推理中…")
        elif isinstance(ev, ToolStarted):
            self._rich_tool_started(ev)
        elif isinstance(ev, ToolFinished):
            self._rich_tool_finished(ev)
        elif isinstance(ev, TurnDone):
            self._flush_text_block()  # commit trailing markdown before footer
            self._stop_live()
            self._on_done(ev, model=model, session_key=session_key)
        elif isinstance(ev, TurnError):
            self._flush_text_block()
            self._stop_live()
            self._on_error(ev)

    # -- live management (exactly one Live active) --

    def _stop_live(self) -> None:
        # Clear refs FIRST so even a raising stop() leaves us live-free — a
        # leaked Live refresh thread would corrupt the next prompt
        # (prompt_toolkit owns the terminal between turns).
        live, self._live = self._live, None
        self._live_kind = None
        self._working = None
        if live is not None:
            try:
                live.stop()
            except Exception:  # noqa: BLE001 — state already cleared above
                pass

    def _spin(self, label: str) -> None:
        """Show (or relabel) the working spinner."""
        from rich.live import Live  # noqa: PLC0415

        if self._live_kind == "spin" and self._working is not None:
            self._working.set_label(label)
            return
        self._stop_live()
        working = _Working(label, time.monotonic())
        live = Live(
            working,
            console=self.console,
            refresh_per_second=12,
            transient=True,  # spinner line vanishes when stopped
        )
        # Start BEFORE publishing to self._live: if start() raises, we never
        # store a half-constructed Live that a later stop()/update() trips on.
        live.start()
        self._live = live
        self._live_kind = "spin"
        self._working = working

    def _spin_label(self, label: str) -> None:
        if self._live_kind == "spin" and self._working is not None:
            self._working.set_label(label)

    def _ensure_text_live(self) -> None:
        from rich.live import Live  # noqa: PLC0415
        from rich.markdown import Markdown  # noqa: PLC0415

        if self._live_kind == "text":
            return
        self._stop_live()
        self._buf = ""
        self._last_md_at = 0.0
        live = Live(
            Markdown(""),
            console=self.console,
            refresh_per_second=8,
            vertical_overflow="visible",
        )
        # Start before publishing (see _spin) so a start() failure can't leave
        # a half-constructed Live referenced.
        live.start()
        self._live = live
        self._live_kind = "text"

    def _rich_text(self, text: str) -> None:
        from rich.markdown import Markdown  # noqa: PLC0415

        if not text:
            return  # empty delta — nothing to render (avoids a spurious paint)
        self._ensure_text_live()
        self._buf += text
        # Past the cap, stop the per-frame re-parse; the final flush still
        # renders the whole block once. Bounds O(n²) on huge replies.
        if len(self._buf) > _MARKDOWN_LIVE_MAX_CHARS:
            return
        now = time.monotonic()
        # Throttle the (relatively costly) Markdown re-parse; the Live
        # widget still repaints on its own clock between updates.
        if now - self._last_md_at >= _MARKDOWN_REPARSE_INTERVAL:
            self._last_md_at = now
            if self._live is not None:
                self._live.update(Markdown(self._buf))

    def _flush_text_block(self) -> None:
        """Commit the in-progress assistant block: push the final Markdown
        into scrollback and tear the Live down."""
        from rich.markdown import Markdown  # noqa: PLC0415

        if self._live_kind == "text" and self._live is not None:
            if self._buf:
                self._live.update(Markdown(self._buf))
            self._stop_live()
            self._buf = ""

    def _rich_tool_started(self, ev: ToolStarted) -> None:
        # Commit whatever assistant text preceded the tool call.
        self._flush_text_block()
        if self.tool_progress == "off":
            self._spin("运行工具…")
            return
        if not ev.plugin and ev.tool in TODO_TOOL_NAMES:
            items = _parse_todo_items(ev.args_json)
            if items is not None:
                self._stop_live()
                self._rich_todo(items)
                self._spin("思考中…")
                return
        label = self._tool_label(ev)
        repeat = self.tool_progress == "new" and label == self._last_tool
        self._last_tool = label
        if not repeat:
            from rich.text import Text  # noqa: PLC0415

            self._stop_live()
            preview = args_preview(ev.args_json)
            line = Text("⏺ ", style="cyan")
            line.append(label, style="bold cyan")
            if preview:
                line.append(f"  {preview}", style="dim")
            self.console.print(line)
        self._spin(f"{label}…")

    def _rich_todo(self, items: list[tuple[str, str]]) -> None:
        self._last_tool = "todo_write"
        key = tuple(items)
        if key == self._last_todos:
            return
        self._last_todos = key
        for status, text in items:
            mark, style = _TODO_MARKS[status]
            self.console.print(f"{mark} {text}", style=style, highlight=False)

    def _rich_tool_finished(self, ev: ToolFinished) -> None:
        self._stop_live()  # stop the tool spinner
        if self.tool_progress != "off":
            from rich.text import Text  # noqa: PLC0415

            label = self._tool_label(ev)
            secs = ev.duration_ms / 1000.0
            line = Text("  ⎿ ", style="dim")
            if ev.is_error:
                line.append("✗ ", style="red")
                line.append(label, style="red")
                line.append(f" ({secs:.1f}s)", style="dim")
                if ev.error_summary:
                    line.append(f"  {ev.error_summary}", style="red dim")
            else:
                line.append("✓ ", style="green")
                line.append(label, style="green")
                line.append(f" ({secs:.1f}s)", style="dim")
            self.console.print(line)
        # The model keeps generating after a tool result.
        self._spin("思考中…")

    def _fallback_to_raw(self) -> None:
        """A rich-path failure: tear any Live down and never touch rich
        again this renderer's life. The raw path is always safe."""
        try:
            self._stop_live()
        except Exception:  # noqa: BLE001 — best-effort teardown
            self._live = None
            self._live_kind = None
        self.rich_ui = False
