"""Slash commands — registry-as-data, claude-code style.

Each command is a :class:`SlashCommand` row; ``/help`` is generated from
the table so it can never drift. Handlers are ``async (app, args) -> str
| None`` — the returned string is printed by the REPL; handlers needing
richer output print via ``app.renderer.console`` themselves.

``app`` is the :class:`corlinman_server.console.app.ConsoleApp` — typed
``Any`` here to avoid an import cycle (the REPL imports this module).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from corlinman_server.console.render import TOOL_PROGRESS_MODES

__all__ = ["SlashCommand", "dispatch", "registry"]

Handler = Callable[[Any, str], Awaitable[str | None]]


@dataclass(frozen=True, slots=True)
class SlashCommand:
    name: str
    description: str
    handler: Handler
    aliases: tuple[str, ...] = ()
    usage: str = ""


async def _cmd_help(app: Any, args: str) -> str:
    _ = (app, args)
    lines = ["commands:"]
    for cmd in registry():
        names = "/" + cmd.name + (
            " (" + ", ".join("/" + a for a in cmd.aliases) + ")" if cmd.aliases else ""
        )
        usage = f" {cmd.usage}" if cmd.usage else ""
        lines.append(f"  {names}{usage} — {cmd.description}")
    return "\n".join(lines)


async def _cmd_new(app: Any, args: str) -> str:
    _ = args
    app.session.reset()
    return f"new session: {app.session.session_key}"


async def _cmd_clear(app: Any, args: str) -> str:
    _ = args
    app.renderer.console.clear()
    app.session.reset()
    return f"cleared — new session: {app.session.session_key}"


async def _cmd_model(app: Any, args: str) -> str:
    name = args.strip()
    if not name:
        current = app.session.model
        small = app.router.small_fast_model or "(unset)"
        auto = "on" if app.router.auto_route else "off"
        known = app.known_models()
        listing = ("\nknown models/aliases: " + ", ".join(known)) if known else ""
        return (
            f"model: {current}\nsmall_fast_model: {small}  auto_route: {auto}"
            f"{listing}\nusage: /model <name> to switch"
        )
    app.session.model = name
    app.router.default_model = name
    # A hand-picked model disables auto-downgrade routing (claude-code
    # rule: routing never overrides the human).
    app.model_explicit = True
    return f"model set: {name}"


async def _cmd_models(app: Any, args: str) -> str:
    _ = args
    known = app.known_models()
    if not known:
        return (
            "no model aliases found — configure providers via the admin UI "
            "or [models.aliases] in config.toml"
        )
    return "models/aliases:\n  " + "\n  ".join(known)


async def _cmd_session(app: Any, args: str) -> str:
    _ = args
    return f"session: {app.session.session_key}  (window: {len(app.session.window)} msgs)"


async def _cmd_sessions(app: Any, args: str) -> str:
    _ = args
    rows = await app.list_sessions()
    if rows is None:
        return "session listing is unavailable in attach mode"
    if not rows:
        return "no recorded sessions"
    return "recent sessions:\n  " + "\n  ".join(rows)


async def _cmd_resume(app: Any, args: str) -> str:
    key = args.strip()
    if not key:
        return "usage: /resume <session-key>  (see /sessions)"
    replayed = await app.resume_session(key)
    if replayed is None:
        return "resume is unavailable in attach mode"
    suffix = f"{replayed} message(s) replayed" if replayed else "no prior turns replayed"
    return f"session: {key} — {suffix}"


async def _cmd_usage(app: Any, args: str) -> str:
    _ = args
    s = app.session.stats
    return (
        f"turns: {s.turns}  prompt: {s.prompt_tokens}  "
        f"completion: {s.completion_tokens}  total: {s.total_tokens} tokens"
    )


async def _cmd_status(app: Any, args: str) -> str:
    _ = args
    r = app.router
    return "\n".join(
        [
            f"brain: {app.session.brain.descriptor}",
            f"model: {app.session.model}  (small: {r.small_fast_model or '—'}, "
            f"auto_route: {'on' if r.auto_route else 'off'})",
            f"session: {app.session.session_key}  window: {len(app.session.window)} msgs",
            f"tool progress: {app.renderer.tool_progress}",
        ]
    )


async def _cmd_progress(app: Any, args: str) -> str:
    mode = args.strip().lower()
    if mode not in TOOL_PROGRESS_MODES:
        return f"usage: /progress <{'|'.join(TOOL_PROGRESS_MODES)}>"
    app.renderer.tool_progress = mode
    return f"tool progress: {mode}"


async def _cmd_verbose(app: Any, args: str) -> str:
    _ = args
    new_mode = "verbose" if app.renderer.tool_progress != "verbose" else "new"
    app.renderer.tool_progress = new_mode
    return f"tool progress: {new_mode}"


async def _cmd_quit(app: Any, args: str) -> str | None:
    _ = args
    app.running = False
    return None


_REGISTRY: tuple[SlashCommand, ...] = (
    SlashCommand("help", "show this list", _cmd_help, aliases=("h", "?")),
    SlashCommand("new", "start a fresh session (drops the window)", _cmd_new),
    SlashCommand("clear", "clear screen + start a fresh session", _cmd_clear),
    SlashCommand("model", "show or switch the active model", _cmd_model, usage="[name]"),
    SlashCommand("models", "list configured model aliases", _cmd_models),
    SlashCommand("session", "show the current session key", _cmd_session),
    SlashCommand("sessions", "list recent sessions (embedded mode)", _cmd_sessions),
    SlashCommand(
        "resume", "switch to a session and replay its turns", _cmd_resume, usage="<key>"
    ),
    SlashCommand("usage", "token usage for this console run", _cmd_usage),
    SlashCommand("status", "brain / model / session overview", _cmd_status),
    SlashCommand(
        "progress", "set tool progress display mode", _cmd_progress, usage="<mode>"
    ),
    SlashCommand("verbose", "toggle verbose tool progress", _cmd_verbose),
    SlashCommand("quit", "exit the console", _cmd_quit, aliases=("exit", "q")),
)


def registry() -> tuple[SlashCommand, ...]:
    return _REGISTRY


def _lookup(name: str) -> SlashCommand | None:
    for cmd in _REGISTRY:
        if name == cmd.name or name in cmd.aliases:
            return cmd
    return None


async def dispatch(app: Any, line: str) -> str | None:
    """Run the slash command in ``line`` (which starts with ``/``).

    Returns the text to print, or ``None`` for silent success. An
    unknown command returns a hint instead of raising — typos must not
    stack-trace the REPL.
    """
    body = line[1:].strip()
    if not body:
        return await _cmd_help(app, "")
    name, _, args = body.partition(" ")
    cmd = _lookup(name.lower())
    if cmd is None:
        return f"unknown command: /{name} — try /help"
    return await cmd.handler(app, args.strip())
