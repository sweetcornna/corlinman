"""ConsoleApp — the REPL itself, plus the ``--print`` one-shot path.

Interaction model (claude-code's flow with hermes ergonomics):

* between turns: a prompt_toolkit prompt with persistent file history
  and a bottom toolbar showing model · session;
* during a turn: events stream through the renderer; **Ctrl-C cancels
  the turn** (sets the brain's cancel event) instead of killing the
  process — a second Ctrl-C at the idle prompt exits;
* ``/`` lines dispatch to :mod:`corlinman_server.console.commands`;
* ``-p/--print`` runs exactly one turn and writes only the assistant
  text to stdout (pipe-friendly), tool progress going to stderr.
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
from corlinman_server.console.commands import dispatch
from corlinman_server.console.events import TextDelta, TurnError
from corlinman_server.console.render import Renderer
from corlinman_server.console.router import ModelRouter

__all__ = ["ConsoleApp", "run_console"]

_BANNER = "corlinman console — /help for commands, Ctrl-C interrupts a running turn"


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
    ) -> None:
        self.session = session
        self.renderer = renderer
        self.router = router
        self.data_dir = data_dir
        self.embedded = embedded
        self.running = True
        #: True when the active model came from --model / /model — see
        #: run_turn's routing rule.
        self.model_explicit = False

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
            if installed:
                with contextlib.suppress(Exception):
                    loop.remove_signal_handler(signal.SIGINT)

    # ── interactive loop ──────────────────────────────────────────────

    async def run_repl(self) -> None:
        from prompt_toolkit import PromptSession  # noqa: PLC0415
        from prompt_toolkit.history import FileHistory  # noqa: PLC0415
        from prompt_toolkit.patch_stdout import patch_stdout  # noqa: PLC0415

        self.renderer.console.print(_BANNER, style="bold")
        self.renderer.console.print(
            f"brain: {self.session.brain.descriptor}   model: {self.session.model}",
            style="dim",
            highlight=False,
        )

        history_path = self.data_dir / "console_history"
        with contextlib.suppress(OSError):
            history_path.parent.mkdir(parents=True, exist_ok=True)
        prompt: PromptSession[str] = PromptSession(
            history=FileHistory(str(history_path)),
        )

        while self.running:
            try:
                with patch_stdout():
                    line = await prompt.prompt_async(
                        "> ",
                        bottom_toolbar=lambda: (
                            f" {self.session.model} · {self.session.session_key}"
                        ),
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
                if reply:
                    self.renderer.console.print(reply, highlight=False)
                continue
            await self.run_turn(line)

        await self.session.brain.aclose()

    # ── one-shot (--print) ────────────────────────────────────────────

    async def run_once(self, text: str) -> int:
        """One turn; assistant text → stdout, everything else → the
        renderer's console (which the CLI points at stderr). Returns a
        process exit code."""
        decision = self.router.route_turn(text)
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
    attach_token: str | None = None,
) -> int:
    """Build the app (embedded or attach brain) and run it.

    The CLI command (:mod:`corlinman_server.cli.console`) owns argument
    parsing; this owns construction order: brain → session → router →
    renderer → app.
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

    console = Console(file=sys.stderr) if print_mode else Console()
    renderer = Renderer(console, tool_progress=tool_progress)

    app = ConsoleApp(
        session=session,
        renderer=renderer,
        router=router,
        data_dir=data_dir,
        embedded=embedded,
    )
    app.model_explicit = model is not None

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
