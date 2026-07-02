"""ConsoleApp — the REPL itself, plus the ``--print`` one-shot path.

Interaction model (claude-code's flow with hermes ergonomics):

* between turns: a prompt_toolkit prompt with persistent file history
  and a bottom toolbar showing model · session;
* during a turn: events stream through the renderer; **Ctrl-C cancels
  the turn** (sets the brain's cancel event) instead of killing the
  process — a second Ctrl-C at the idle prompt exits;
* ``/`` lines dispatch to :mod:`corlinman_server.console.commands`;
* ``-p/--print`` runs exactly one turn and writes only the assistant
  text to stdout (pipe-friendly), tool progress going to stderr;
* ``--output-format json|stream-json`` (print mode) switches stdout to
  a machine channel — claude-code's headless contract: ``json`` prints
  one closing ``result`` envelope, ``stream-json`` prints one JSON
  object per line as events arrive, ending with the same envelope;
* ``--max-turns N`` caps the REPL at N completed turns (0 = unlimited).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any

from corlinman_server.console.brain import Brain, BrainSession
from corlinman_server.console.commands import (
    TurnRequest,
    _estimate_session_cost_usd,
    dispatch,
    registry,
)
from corlinman_server.console.compaction import Compactor, maybe_auto_compact
from corlinman_server.console.events import (
    ConsoleEvent,
    ReasoningDelta,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnDone,
    TurnError,
)
from corlinman_server.console.project_memory import load_project_memory
from corlinman_server.console.render import Renderer
from corlinman_server.console.router import ModelRouter

__all__ = ["OUTPUT_FORMATS", "ConsoleApp", "run_console"]

_BANNER = "corlinman console — /help for commands, Ctrl-C interrupts a running turn"


def _build_slash_completer() -> Any:
    """A prompt_toolkit completer that suggests ``/slash`` commands at the
    start of a line — claude-code's command palette. Inert once the line
    has a space or doesn't open with ``/``. Built lazily so prompt_toolkit
    stays a soft dependency (the module imports without it)."""
    from prompt_toolkit.completion import Completer, Completion  # noqa: PLC0415

    class _SlashCompleter(Completer):
        def get_completions(self, document: Any, complete_event: Any) -> Any:
            text = document.text_before_cursor
            if not text.startswith("/") or " " in text:
                return
            word = text[1:].lower()
            for cmd in registry():
                for name in (cmd.name, *cmd.aliases):
                    if name.startswith(word):
                        yield Completion(
                            name,
                            start_position=-len(word),
                            display=f"/{name}",
                            display_meta=cmd.description,
                        )
                        break

    return _SlashCompleter()

#: ``--print`` stdout contracts (claude-code's ``--output-format``):
#: ``text`` streams the answer, ``json`` prints one ``result`` envelope,
#: ``stream-json`` prints one JSON event per line closed by the envelope.
OUTPUT_FORMATS = ("text", "json", "stream-json")


def _emit_json_line(payload: dict[str, Any]) -> None:
    """Write one JSON object + newline to stdout (the machine channel).

    ``ensure_ascii=False`` keeps CJK answers readable; stdout is flushed
    per line so a piping consumer sees events as they happen.
    """
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _stream_payload(ev: ConsoleEvent) -> dict[str, Any] | None:
    """Map a non-terminal console event to its ``stream-json`` line.

    Returns ``None`` for terminal events (they fold into the closing
    ``result`` envelope) and for any future event type without a wire
    mapping — unknown events must never corrupt the stdout stream.
    """
    if isinstance(ev, TextDelta):
        return {"type": "text_delta", "text": ev.text}
    if isinstance(ev, ReasoningDelta):
        return {"type": "reasoning_delta", "text": ev.text}
    if isinstance(ev, ToolStarted):
        raw = ev.args_json.decode("utf-8", "replace")
        try:
            args: Any = json.loads(raw)
        except ValueError:
            args = raw
        return {
            "type": "tool_started",
            "tool": ev.tool,
            "plugin": ev.plugin,
            "call_id": ev.call_id,
            "args": args,
        }
    if isinstance(ev, ToolFinished):
        return {
            "type": "tool_finished",
            "tool": ev.tool,
            "call_id": ev.call_id,
            "duration_ms": ev.duration_ms,
            "is_error": ev.is_error,
        }
    return None


def _result_envelope(
    *,
    result: str,
    session_key: str,
    model: str,
    num_turns: int,
    done: TurnDone | None,
    error: TurnError | None,
) -> dict[str, Any]:
    """The closing ``result`` object both JSON print modes emit last.

    Mirrors claude-code's ``--output-format json`` envelope in spirit: a
    stable shape a script can ``jq`` without knowing how the answer was
    produced. ``usage`` is zeroed when the turn died before ``TurnDone``.
    """
    return {
        "type": "result",
        "subtype": "error" if error is not None else "success",
        "result": result,
        "session_id": session_key,
        "model": model,
        "usage": {
            "prompt_tokens": done.prompt_tokens if done else 0,
            "completion_tokens": done.completion_tokens if done else 0,
            "total_tokens": done.total_tokens if done else 0,
        },
        "num_turns": num_turns,
        "is_error": error is not None,
        "error": (
            {"reason": error.reason, "message": error.message}
            if error is not None
            else None
        ),
    }


class ConsoleApp:
    """Wires session + renderer + router and runs the input loop."""

    def __init__(
        self,
        *,
        session: BrainSession,
        renderer: Renderer,
        router: ModelRouter,
        data_dir: Path,
        embedded: bool,
        output_format: str = "text",
        max_turns: int = 0,
    ) -> None:
        self.session = session
        self.renderer = renderer
        self.router = router
        self.data_dir = data_dir
        self.embedded = embedded
        #: One of :data:`OUTPUT_FORMATS`; only consulted by ``run_once``.
        self.output_format = output_format
        #: ``--max-turns`` REPL budget; 0 = unlimited. ``run_once`` is a
        #: single turn by construction, so it ignores the cap.
        self.max_turns = max_turns
        #: Completed user turns (success, error, or cancel — the user's
        #: turn was consumed either way). Drives the ``--max-turns`` gate.
        self.turns_completed = 0
        self.running = True
        #: CORLINMAN.md files folded into the system prompt (see /memory).
        self.project_memory_files: list[Path] = []
        #: True when the active model came from --model / /model — see
        #: run_turn's routing rule.
        self.model_explicit = False
        #: Interactive ask-approval resolver (embedded interactive REPL only;
        #: None in --print / attach). /permissions reads its always_allow set.
        self.approval_resolver: Any | None = None

    def max_turns_reached(self) -> bool:
        """``--max-turns`` budget exhausted (0 = unlimited)."""
        return self.max_turns > 0 and self.turns_completed >= self.max_turns

    # ── lookups used by slash commands ────────────────────────────────

    def known_models(self) -> list[str]:
        """Alias names from the py-config drop (best-effort)."""
        path = os.environ.get("CORLINMAN_PY_CONFIG")
        if not path:
            return []
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        aliases = data.get("aliases") if isinstance(data, dict) else None
        return sorted(aliases) if isinstance(aliases, dict) else []

    async def list_sessions(self) -> list[str] | None:
        """Recent journal sessions, formatted; ``None`` in attach mode."""
        if not self.embedded:
            return None
        journal = await self._open_journal()
        if journal is None:
            return []
        try:
            rows = await journal.list_session_summaries(limit=20)
            out = []
            for row in rows:
                key = getattr(row, "session_key", "?")
                count = getattr(row, "message_count", "?")
                preview = (getattr(row, "last_user_text", None) or "").strip()
                out.append(f"{key}  ({count} msgs)  {preview}")
            return out
        finally:
            with contextlib.suppress(Exception):
                await journal.close()

    async def resume_session(self, session_key: str) -> int | None:
        """Switch to ``session_key``, replaying its journaled user /
        assistant messages into the window. ``None`` in attach mode."""
        if not self.embedded:
            return None
        self.session.reset(session_key=session_key)
        journal = await self._open_journal()
        if journal is None:
            return 0
        try:
            turn_ids = await journal.get_session_turn_ids(session_key, limit=50)
            replayed = 0
            for turn_id in reversed(turn_ids):  # oldest first
                messages = await journal._load_messages(turn_id)  # noqa: SLF001 — stable shim
                for msg in messages:
                    role = str(msg.get("role", ""))
                    content = str(msg.get("content", "") or "")
                    if role in ("user", "assistant") and content.strip():
                        self.session.window.append({"role": role, "content": content})
                        replayed += 1
            return replayed
        except Exception:  # noqa: BLE001 — resume is best-effort
            return 0
        finally:
            with contextlib.suppress(Exception):
                await journal.close()

    async def _open_journal(self) -> Any | None:
        try:
            from corlinman_server.agent_journal import AgentJournal  # noqa: PLC0415

            path = self.data_dir / "agent_journal.sqlite"
            backend = (os.environ.get("CORLINMAN_JOURNAL_BACKEND") or "").lower()
            if backend in ("", "sqlite") and not path.is_file():
                # SQLite backend with no journal yet — nothing to read. A
                # configured Postgres backend has no local file to probe,
                # so it always proceeds to open_from_env.
                return None
            return await AgentJournal.open_from_env(path)
        except Exception:  # noqa: BLE001 — journal access is best-effort
            return None

    # ── turn execution ────────────────────────────────────────────────

    async def run_turn(self, text: str) -> None:
        compacted = await maybe_auto_compact(self.session, self.router.utility_model())
        if compacted is not None and compacted.ok:
            self.renderer.console.print(compacted.notice, style="dim")
        elif compacted is not None:
            self.renderer.console.print(f"compact failed: {compacted.error}", style="yellow")
        decision = self.router.route_turn(
            text,
            # An explicit --model flag or /model choice must win over
            # auto-routing (claude-code rule): tell the router so it
            # never downgrades a hand-picked model to small_fast_model.
            explicit_model=self.session.model if self.model_explicit else None,
        )
        if decision.reason == "auto:simple":
            self.renderer.console.print(
                f"→ routed to {decision.model} (simple task)", style="dim"
            )
        self.renderer.start_turn()

        loop = asyncio.get_running_loop()
        installed = False
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            # Ctrl-C while streaming cancels the turn instead of raising
            # into the middle of the event loop (Windows: unsupported —
            # KeyboardInterrupt falls through to the except below).
            loop.add_signal_handler(signal.SIGINT, self.session.cancel_turn)
            installed = True
        try:
            async for ev in self.session.send(
                text,
                model=None if decision.reason == "default" else decision.model,
            ):
                self.renderer.on_event(
                    ev,
                    model=decision.model,
                    session_key=self.session.session_key,
                )
        except KeyboardInterrupt:
            self.session.cancel_turn()
            self.renderer.console.print("⏹ interrupted", style="yellow")
        finally:
            # Safety net: stop any rich Live the turn left running (an event
            # stream that raised before TurnDone/TurnError) so it can never
            # bleed into the next prompt and corrupt the terminal.
            self.renderer.finish_turn()
            if installed:
                with contextlib.suppress(Exception):
                    loop.remove_signal_handler(signal.SIGINT)
            self.turns_completed += 1

    # ── interactive loop ──────────────────────────────────────────────

    def _print_welcome(self) -> None:
        """A boxed welcome panel (model / session / data / brain + key
        commands) — claude-code's intro card, in place of the old one-line
        banner. Degrades to the plain banner if rich's panel is missing."""
        try:
            from rich.panel import Panel  # noqa: PLC0415
            from rich.table import Table  # noqa: PLC0415
            from rich.text import Text  # noqa: PLC0415
        except Exception:  # noqa: BLE001 — never let the banner crash the REPL
            self.renderer.console.print(_BANNER, style="bold")
            return

        meta = Table.grid(padding=(0, 2))
        meta.add_column(style="dim", justify="right")
        meta.add_column(style="bold", overflow="fold")
        meta.add_row("model", self.session.model)
        meta.add_row("session", self.session.session_key)
        meta.add_row("data", str(self.data_dir))
        meta.add_row("brain", self.session.brain.descriptor)

        hint = Text()
        for key, label, sep in (
            ("/help", " commands", "   "),
            ("/model", " switch", "   "),
            ("Ctrl-C", " interrupt", "   "),
            ("/quit", " exit", ""),
        ):
            hint.append(key, style="cyan")
            hint.append(label + sep, style="dim")

        body = Table.grid()
        body.add_row(meta)
        body.add_row("")
        body.add_row(hint)
        self.renderer.console.print(
            Panel.fit(
                body,
                title="corlinman console",
                title_align="left",
                border_style="cyan",
            )
        )

    def _bottom_toolbar(self) -> str:
        """prompt_toolkit bottom bar: model · session · live token count · cost
        (ABSORB_MATRIX Dim 12 — surfaces the session tokens/cost the loop already
        tracks; the raw model·session bar showed neither)."""
        s = self.session.stats
        parts = [f" {self.session.model}", self.session.session_key]
        if s.total_tokens:
            parts.append(f"{s.total_tokens:,} tok")
            cost = _estimate_session_cost_usd(
                self.session.model, s.prompt_tokens, s.completion_tokens
            )
            if cost is not None:
                parts.append(f"${cost:.4f}")
        return " · ".join(parts)

    async def run_repl(self) -> None:
        from prompt_toolkit import PromptSession  # noqa: PLC0415
        from prompt_toolkit.history import FileHistory  # noqa: PLC0415
        from prompt_toolkit.patch_stdout import patch_stdout  # noqa: PLC0415

        self._print_welcome()

        history_path = self.data_dir / "console_history"
        with contextlib.suppress(OSError):
            history_path.parent.mkdir(parents=True, exist_ok=True)
        prompt: PromptSession[str] = PromptSession(
            history=FileHistory(str(history_path)),
            completer=_build_slash_completer(),
            complete_while_typing=True,
        )

        while self.running:
            if self.max_turns_reached():
                # claude-code's --max-turns: a hard budget for scripted /
                # supervised runs — announce and leave, never mid-turn.
                self.renderer.console.print(
                    f"reached --max-turns={self.max_turns}, exiting", style="yellow"
                )
                break
            try:
                with patch_stdout():
                    line = await prompt.prompt_async(
                        "> ",
                        bottom_toolbar=self._bottom_toolbar,
                    )
            except KeyboardInterrupt:
                self.renderer.console.print("(/quit to exit)", style="dim")
                continue
            except EOFError:
                break

            line = line.strip()
            if not line:
                continue
            if line.startswith("/"):
                reply = await dispatch(self, line)
                if isinstance(reply, TurnRequest):
                    # Shared wizard-style command (e.g. /persona) — the
                    # prelude drives the agent, same as on the channels.
                    await self.run_turn(reply.content)
                elif reply:
                    self.renderer.console.print(reply, highlight=False)
                continue
            await self.run_turn(line)

        await self.session.brain.aclose()

    # ── one-shot (--print) ────────────────────────────────────────────

    async def run_once(self, text: str) -> int:
        """One turn, ``--print`` semantics. Returns a process exit code
        (0 success, 1 turn error).

        Output contract by ``output_format``:

        * ``text`` — assistant text streams to stdout; tool progress and
          errors go to the renderer's console (stderr in print mode);
        * ``json`` — stdout stays silent until the turn ends, then ONE
          ``result`` envelope is printed;
        * ``stream-json`` — one JSON object per line as events arrive,
          always closed by the same ``result`` envelope.
        """
        if self.output_format in ("json", "stream-json"):
            return await self._run_once_structured(text)
        decision = self.router.route_turn(
            text,
            # Same rule as run_turn: an explicit --model must never be
            # auto-downgraded — pipe mode included.
            explicit_model=self.session.model if self.model_explicit else None,
        )
        self.renderer.start_turn()
        failed = False
        try:
            async for ev in self.session.send(
                text,
                model=None if decision.reason == "default" else decision.model,
            ):
                if isinstance(ev, TextDelta):
                    sys.stdout.write(ev.text)
                    sys.stdout.flush()
                    continue
                if isinstance(ev, TurnError):
                    failed = True
                self.renderer.on_event(
                    ev,
                    model=decision.model,
                    session_key=self.session.session_key,
                )
        finally:
            if not sys.stdout.closed:
                sys.stdout.write("\n")
                sys.stdout.flush()
            await self.session.brain.aclose()
        return 1 if failed else 0

    async def _run_once_structured(self, text: str) -> int:
        """``json`` / ``stream-json`` print modes.

        stdout carries ONLY JSON; the renderer is bypassed entirely so
        no stray styling or progress line can ever land in a pipe (the
        renderer's console points at stderr in print mode, but headless
        consumers of these formats expect silence there too — matching
        claude-code's behaviour).
        """
        decision = self.router.route_turn(
            text,
            explicit_model=self.session.model if self.model_explicit else None,
        )
        streaming = self.output_format == "stream-json"
        reply_parts: list[str] = []
        done: TurnDone | None = None
        error: TurnError | None = None
        try:
            async for ev in self.session.send(
                text,
                model=None if decision.reason == "default" else decision.model,
            ):
                if isinstance(ev, TextDelta):
                    reply_parts.append(ev.text)
                if isinstance(ev, TurnDone):
                    done = ev
                elif isinstance(ev, TurnError):
                    error = ev
                elif streaming:
                    payload = _stream_payload(ev)
                    if payload is not None:
                        _emit_json_line(payload)
        finally:
            await self.session.brain.aclose()
        _emit_json_line(
            _result_envelope(
                result="".join(reply_parts),
                session_key=self.session.session_key,
                model=decision.model,
                num_turns=self.session.stats.turns,
                done=done,
                error=error,
            )
        )
        return 1 if error is not None else 0


async def run_console(
    *,
    data_dir: Path,
    config: dict[str, Any],
    model: str | None,
    attach: str | None,
    session_key: str | None,
    prompt: str | None,
    print_mode: bool,
    tool_progress: str,
    output_format: str = "text",
    max_turns: int = 0,
    attach_token: str | None = None,
) -> int:
    """Build the app (embedded or attach brain) and run it.

    The CLI command (:mod:`corlinman_server.cli.console`) owns argument
    parsing; this owns construction order: brain → session → router →
    renderer → app. ``output_format`` (one of :data:`OUTPUT_FORMATS`)
    only matters with ``print_mode``; ``max_turns`` only gates the REPL.
    """
    from rich.console import Console  # noqa: PLC0415

    brain: Brain
    if attach:
        from corlinman_server.console.attach import AttachBrain  # noqa: PLC0415

        # Production gateways gate /v1/chat/completions behind the auth
        # middleware; send the token both ways it accepts so the console
        # can attach without disabling auth.
        headers: dict[str, str] = {}
        if attach_token:
            headers["Authorization"] = f"Bearer {attach_token}"
            headers["X-API-Key"] = attach_token
        brain = AttachBrain(attach, headers=headers)
        embedded = False
    else:
        from corlinman_server.console.embedded import EmbeddedBrain  # noqa: PLC0415

        brain = await EmbeddedBrain.start(data_dir, config=config)
        embedded = True

    models_cfg = config.get("models") if isinstance(config, dict) else None
    default_model = model or (
        str(models_cfg.get("default"))
        if isinstance(models_cfg, dict) and models_cfg.get("default")
        else "claude-sonnet-4-6"
    )
    router = ModelRouter.from_config(config, default_model=default_model)

    session = BrainSession(brain=brain, model=default_model)
    session.compactor = Compactor.from_config(config)

    # Project memory (CORLINMAN.md) — loaded in every mode, including
    # --print (claude-code loads CLAUDE.md for one-shot runs too).
    # The servicer's _ensure_system_prompt PRESERVES a caller-supplied
    # system message verbatim (appending only the env block), so sending
    # memory alone would silently drop the default coding prompt — prefix
    # it explicitly to keep tool/coding behavior intact.
    memory_text, memory_files = load_project_memory(Path.cwd(), data_dir)
    if memory_text:
        base_prompt = ""
        try:
            from corlinman_server.agent_servicer import (  # noqa: PLC0415
                _CODING_SYSTEM_PROMPT,
            )

            base_prompt = _CODING_SYSTEM_PROMPT
        except Exception:  # noqa: BLE001 — attach-only/stripped installs
            pass
        session.system_prompt = (
            f"{base_prompt}\n\n{memory_text}" if base_prompt else memory_text
        )

    console = Console(file=sys.stderr) if print_mode else Console()
    # Rich UI (spinner + live markdown + tool blocks) only on an interactive
    # REPL terminal; --print / piped / JSON output stays on the raw path.
    renderer = Renderer(
        console,
        tool_progress=tool_progress,
        rich_ui=(not print_mode and bool(console.is_terminal)),
    )

    app = ConsoleApp(
        session=session,
        renderer=renderer,
        router=router,
        data_dir=data_dir,
        embedded=embedded,
        output_format=output_format,
        max_turns=max_turns,
    )
    app.project_memory_files = memory_files
    app.model_explicit = model is not None

    # Interactive tool approval (Dim 3): wire the ask-resolver so a permission
    # rule's ``ask`` verdict prompts y/always/No instead of fail-closing to
    # deny. Interactive REPL only — in --print there is no user to ask, so
    # asks stay fail-closed (the correct non-interactive posture); attach mode
    # has no in-process servicer to wire.
    if embedded and not print_mode:
        from corlinman_server.console.approval import (  # noqa: PLC0415
            ConsoleApprovalResolver,
            build_console_prompter,
        )

        wire = getattr(brain, "set_approval_resolver", None)
        if callable(wire):
            app.approval_resolver = ConsoleApprovalResolver(
                build_console_prompter(renderer)
            )
            wire(app.approval_resolver)

    if session_key:
        if embedded:
            # ``--session`` continues a conversation — replay its journaled
            # turns into the window (the chat contract is a stateless
            # message window, so without the replay the first prompt
            # would silently drop all prior context).
            replayed = await app.resume_session(session_key)
            if replayed:
                console.print(
                    f"resumed {session_key}: {replayed} message(s) replayed",
                    style="dim",
                    highlight=False,
                )
        else:
            session.session_key = session_key
            # Attach mode cannot replay history: the journal lives in the
            # remote gateway process and /v1/chat/completions is a
            # stateless window — say so instead of silently continuing
            # with empty context under an old key.
            console.print(
                f"note: --session {session_key} in attach mode only tags "
                "journaling on the gateway; prior turns are NOT replayed "
                "into this window",
                style="yellow",
                highlight=False,
            )

    if print_mode:
        if not prompt:
            console.print("--print requires a PROMPT argument", style="bold red")
            await brain.aclose()
            return 2
        return await app.run_once(prompt)

    if prompt:
        await app.run_turn(prompt)
    await app.run_repl()
    return 0
