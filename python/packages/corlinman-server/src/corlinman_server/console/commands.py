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

from corlinman_server.console.compaction import Compactor
from corlinman_server.console.render import TOOL_PROGRESS_MODES
from corlinman_server.console.rewind import cmd_rewind as _cmd_rewind

__all__ = ["SlashCommand", "TurnRequest", "dispatch", "registry"]


@dataclass(frozen=True, slots=True)
class TurnRequest:
    """Sentinel return from :func:`dispatch` — a command that resolved to a
    *prelude* (wizard-style shared command like ``/persona``, or ``/init``'s
    codebase-analysis prompt) which the REPL must send through the brain as a
    chat turn rather than print."""

    content: str


Handler = Callable[[Any, str], Awaitable[str | TurnRequest | None]]


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
        names = (
            "/"
            + cmd.name
            + (" (" + ", ".join("/" + a for a in cmd.aliases) + ")" if cmd.aliases else "")
        )
        usage = f" {cmd.usage}" if cmd.usage else ""
        lines.append(f"  {names}{usage} — {cmd.description}")
    return "\n".join(lines)


def _reset_approval_cache(app: Any) -> None:
    """Drop cached "always" approval grants on a session/mode boundary.

    The interactive resolver's cache is presented as "always THIS
    session" — /new, /clear, and permission-mode switches all move that
    boundary, so the grants must not carry across (Codex #104).
    """
    resolver = getattr(app, "approval_resolver", None)
    reset = getattr(resolver, "reset", None)
    if callable(reset):
        reset()


async def _cmd_new(app: Any, args: str) -> str:
    _ = args
    app.session.reset()
    _reset_approval_cache(app)
    return f"new session: {app.session.session_key}"


async def _cmd_clear(app: Any, args: str) -> str:
    _ = args
    app.renderer.console.clear()
    app.session.reset()
    _reset_approval_cache(app)
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
    # Fuzzy resolution (Dim 11): an exact key wins; a unique substring match
    # resolves; multiple matches disambiguate instead of guessing. Zero
    # matches fall through with the raw key — /resume can also start a fresh
    # named session (today's semantics, preserved).
    matcher = getattr(app, "match_session_keys", None)
    if callable(matcher):
        matches = await matcher(key)
        if len(matches) == 1:
            key = matches[0]
        elif len(matches) > 1:
            listing = "\n".join(f"  {k}" for k in matches[:10])
            return f"ambiguous — {len(matches)} sessions match '{key}':\n{listing}"
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


def _estimate_session_cost_usd(
    model: str, prompt_tokens: int, completion_tokens: int
) -> float | None:
    """Estimated USD cost for the session's tokens, or ``None`` if the pricing
    estimator is unavailable / the model is unknown. Reuses the agent loop's
    per-model coefficients (ABSORB_MATRIX Dim 12: cost is computed deep but was
    never surfaced in the console)."""
    try:
        from corlinman_agent.reasoning_loop import (  # noqa: PLC0415
            _estimate_turn_cost_usd,
        )
    except ImportError:
        return None
    # TurnStats uses OpenAI-style names; the estimator wants input/output. The
    # console does not track cache-token classes, so those are omitted (0).
    cost = _estimate_turn_cost_usd(
        model, {"input_tokens": prompt_tokens, "output_tokens": completion_tokens}
    )
    return cost if cost > 0 else None


async def _cmd_cost(app: Any, args: str) -> str:
    _ = args
    s = app.session.stats
    cost = _estimate_session_cost_usd(app.session.model, s.prompt_tokens, s.completion_tokens)
    cost_line = (
        f"${cost:.4f} (estimated)"
        if cost is not None
        else "unavailable (model not in the pricing table)"
    )
    return (
        "session cost\n"
        f"  model:  {app.session.model}\n"
        f"  turns:  {s.turns}\n"
        f"  tokens: {s.prompt_tokens} in + {s.completion_tokens} out "
        f"= {s.total_tokens}\n"
        f"  cost:   {cost_line}"
    )


async def _cmd_memory(app: Any, args: str) -> str:
    _ = args
    files = getattr(app, "project_memory_files", None) or []
    if not files:
        return (
            "no project memory loaded — create a CORLINMAN.md in your "
            "project root (or CORLINMAN.local.md for untracked notes)"
        )
    lines = [f"project memory ({len(files)} file(s), folded into the system prompt):"]
    for path in files:
        try:
            size = f"{path.stat().st_size} bytes"
        except OSError:
            size = "unreadable"
        lines.append(f"  {path}  ({size})")
    return "\n".join(lines)


async def _cmd_compact(app: Any, args: str) -> str:
    """Manual /compact — runs unconditionally (no threshold check), so
    it works even when auto-compact is off or its breaker has tripped."""
    _ = args
    compactor = app.session.compactor or Compactor()
    result = await compactor.compact(app.session, model=app.router.utility_model())
    if not result.ok:
        return (
            f"compact failed: {result.error} "
            f"(window unchanged, ~{result.before_tokens} est. tokens)"
        )
    return result.notice + f"  (window: {len(app.session.window)} msgs)"


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


#: Recognized permission modes (mirrors ``PermissionMode``); validated here so
#: a typo never coerces to ``default`` server-side — silently dropping from
#: ``plan`` to ``default`` would re-enable mutations without the user noticing.
_PERMISSION_MODES = ("default", "acceptEdits", "plan", "bypass")


async def _cmd_permissions(app: Any, args: str) -> str:
    brain = app.session.brain
    get = getattr(brain, "get_permission_mode", None)
    set_ = getattr(brain, "set_permission_mode", None)
    if not callable(get) or not callable(set_):
        return "permission control unavailable (attach mode)"
    requested = args.strip()
    if not requested:
        current = get()
        if current is None:
            return "permission control unavailable (direct fallback — no tool gate)"
        lines = [
            f"permission mode: {current}",
            f"modes: {' | '.join(_PERMISSION_MODES)}",
            "usage: /permissions <mode>   (/plan toggles plan mode)",
        ]
        resolver = getattr(app, "approval_resolver", None)
        always = sorted(getattr(resolver, "always_allow", ()) or ())
        if always:
            lines.append(f"always-allowed this session: {', '.join(always)}")
        return "\n".join(lines)
    matched = next((m for m in _PERMISSION_MODES if m.lower() == requested.lower()), None)
    if matched is None:
        return f"unknown mode: {requested} — mode unchanged\nmodes: {' | '.join(_PERMISSION_MODES)}"
    resolved = set_(matched)
    if resolved is None:
        return "permission control unavailable (direct fallback — no tool gate)"
    # A mode switch invalidates interactive "always" grants — most sharply,
    # a cached run_shell grant must not keep mutating in plan mode (the
    # gate resolves explicit ask rules BEFORE the mode override, so the
    # resolver cache would otherwise bypass /plan entirely).
    _reset_approval_cache(app)
    suffix = "  ⚠ all tool gating disabled" if resolved == "bypass" else ""
    return f"permission mode: {resolved}{suffix}"


async def _cmd_plan(app: Any, args: str) -> str:
    """Enter/exit plan mode (mutating tools denied while planning)."""
    if args.strip().lower() in ("off", "exit", "done"):
        return await _cmd_permissions(app, "default")
    return await _cmd_permissions(app, "plan")


_INIT_PROMPT = (
    "Bootstrap a CORLINMAN.md project-memory file for this codebase.\n\n"
    "First, use your file tools (list, search, read) to inspect the project: "
    "the directory layout, build/test/lint commands (check Makefile, "
    "package.json, pyproject.toml, CONTRIBUTING.md), the high-level "
    "architecture, and any project-specific conventions or gotchas.\n\n"
    "Then write a concise, high-signal CORLINMAN.md at the repository root (the "
    "directory containing .git, else the current directory). It is folded into "
    "the system prompt of every future session, so keep it tight — cover: the "
    "exact build/lint/test commands, the main components and how they fit "
    "together, and the non-obvious rules a new contributor must know. If a "
    "CORLINMAN.md already exists, read it first and improve it rather than "
    "discarding good content. Finish with a one-line summary of what you "
    "captured."
)


async def _cmd_init(app: Any, args: str) -> TurnRequest:
    """Bootstrap CORLINMAN.md from a one-shot codebase-analysis turn (the
    claude-code ``/init`` analog). Returns a :class:`TurnRequest` so the brain
    runs it with its file tools; the CORLINMAN.md discovery/@include pipeline
    then folds the result into every subsequent session's system prompt."""
    _ = (app, args)
    return TurnRequest(_INIT_PROMPT)


_REGISTRY: tuple[SlashCommand, ...] = (
    SlashCommand("help", "show this list", _cmd_help, aliases=("h", "?")),
    SlashCommand("new", "start a fresh session (drops the window)", _cmd_new),
    SlashCommand("clear", "clear screen + start a fresh session", _cmd_clear),
    SlashCommand("model", "show or switch the active model", _cmd_model, usage="[name]"),
    SlashCommand("models", "list configured model aliases", _cmd_models),
    SlashCommand("session", "show the current session key", _cmd_session),
    SlashCommand("sessions", "list recent sessions (embedded mode)", _cmd_sessions),
    SlashCommand("resume", "switch to a session and replay its turns", _cmd_resume, usage="<key>"),
    SlashCommand("usage", "token usage for this console run", _cmd_usage),
    SlashCommand("cost", "estimated USD cost for this session", _cmd_cost),
    SlashCommand(
        "permissions",
        "show or set the permission mode (default/acceptEdits/plan/bypass)",
        _cmd_permissions,
        usage="[mode]",
    ),
    SlashCommand(
        "plan",
        "enter plan mode — mutating tools denied; /plan off to exit",
        _cmd_plan,
        usage="[off]",
    ),
    SlashCommand("compact", "summarize older turns to shrink the context window", _cmd_compact),
    SlashCommand(
        "rewind",
        "list workspace checkpoints or restore one",
        _cmd_rewind,
        usage="[n|sha]",
    ),
    SlashCommand("memory", "list loaded CORLINMAN.md project-memory files", _cmd_memory),
    SlashCommand("init", "analyze the codebase and write a CORLINMAN.md", _cmd_init),
    SlashCommand("status", "brain / model / session overview", _cmd_status),
    SlashCommand("progress", "set tool progress display mode", _cmd_progress, usage="<mode>"),
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


async def _dispatch_shared(app: Any, line: str) -> str | TurnRequest | None:
    """Fall back to the cross-surface command registry
    (:mod:`corlinman_channels.commands` — the same registry the chat
    channels and the web playground dispatch through), so every shared
    command works in the console too. Returns ``None`` when the registry
    is unavailable or has no match.

    The console presents itself as a synthetic binding
    (``channel="console"``) exactly like the playground's
    ``channel="playground"`` — handlers use it to detect a non-channel
    surface.
    """
    try:
        from corlinman_channels.commands import (  # noqa: PLC0415 — soft dep
            CommandContext,
            apply_command_prelude,
            is_command_admin,
            match_command_with_args,
            run_command_handler,
        )
        from corlinman_channels.common import ChannelBinding  # noqa: PLC0415
    except ImportError:
        return None

    match = match_command_with_args(line)
    if match is None:
        return None
    spec, args_text = match
    if spec.wizard_prelude is not None:
        prelude = apply_command_prelude(line, spec, args_text=args_text)
        return TurnRequest(content=prelude or line)
    if spec.handler is None:
        return None
    binding = ChannelBinding(
        channel="console",
        account="local",
        thread=app.session.session_key,
        sender="local",
    )
    ctx = CommandContext(
        spec=spec,
        raw_text=line,
        args_text=args_text,
        binding=binding,
        is_admin=is_command_admin(binding),
    )
    result = await run_command_handler(spec, ctx)
    # corlinman_channels ships no py.typed marker, so ``reply`` is seen
    # as Any — narrow it explicitly.
    reply = result.reply
    return reply if isinstance(reply, str) else None


async def dispatch(app: Any, line: str) -> str | TurnRequest | None:
    """Run the slash command in ``line`` (which starts with ``/``).

    Resolution order: console-local registry first (richer, window-aware
    implementations of /model, /new, /usage), then the cross-surface
    channel registry (so /persona, /whoami, /status and every future
    shared command work here too). Returns the text to print, ``None``
    for silent success, or a :class:`TurnRequest` the REPL must send as
    a chat turn. An unknown command returns a hint instead of raising —
    typos must not stack-trace the REPL.
    """
    body = line[1:].strip()
    if not body:
        return await _cmd_help(app, "")
    name, _, args = body.partition(" ")
    cmd = _lookup(name.lower())
    if cmd is not None:
        return await cmd.handler(app, args.strip())
    shared = await _dispatch_shared(app, line)
    if shared is not None:
        return shared
    return f"unknown command: /{name} — try /help"
