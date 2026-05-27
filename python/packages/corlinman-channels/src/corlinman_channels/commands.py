"""Slash-command registry for inbound channels + web playground.

Two delivery models coexist per command:

* **Prelude** (legacy) — the router rewrites the user's literal text to
  a ``[SYSTEM-INSERTED] ...`` block and forwards it to the agent. The
  LLM produces the actual reply. Used for wizard-style commands
  (``/persona``) that need follow-up questions.
* **Handler** — the channel adapter invokes a sync callable that
  returns a :class:`CommandResult`; the adapter sends the result text
  back directly and the agent turn is skipped entirely. Used for
  read-only commands (``/help``, ``/whoami``, ``/status``) where an
  LLM relay is wasteful.

A :class:`CommandSpec` may carry one or both. When both are present
the router prefers the handler (no LLM cost), while the web playground
falls back to the prelude (it does not have a direct-send surface).

See ``docs/PLAN_PERSONA_STUDIO.md`` for the prelude lineage and
``docs/PLAN_COMMAND_SYSTEM.md`` (TBD) for the handler extension.

Two surfaces consume this module:

* :mod:`corlinman_channels.router` (``ChannelRouter.dispatch``) — for
  channel adapters (QQ / Telegram / Discord / Slack / Feishu / WeChat).
* :mod:`corlinman_server.gateway.services.chat_bootstrap` (the web
  message-assembly helper) — for the admin playground.

Sharing the registry keeps both surfaces in lockstep.

Matching contract
-----------------

The matcher fires on whole-stripped-message exact match of any
registered alias, OR a message whose first whitespace-delimited token
equals an alias (the "command + args" form). Partial substring matches
inside longer prose intentionally do NOT trigger. See
:func:`match_command` for the precise rule.

Runtime extension
-----------------

External code may register additional commands at runtime via
:func:`register_command`. The dispatcher reads
:data:`COMMAND_REGISTRY` + the mutable :data:`runtime_registry`.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from corlinman_channels.common import ChannelBinding

__all__ = [
    "COMMAND_REGISTRY",
    "CommandContext",
    "CommandHandler",
    "CommandResult",
    "CommandSpec",
    "all_specs",
    "apply_command_prelude",
    "is_command_admin",
    "match_command",
    "match_command_with_args",
    "register_command",
    "run_command_handler",
    "runtime_registry",
    "telegram_bot_commands",
    "validate_registry",
]


# ---------------------------------------------------------------------------
# Handler types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandContext:
    """Per-invocation context handed to a command handler.

    ``binding`` may be a synthetic playground binding when the
    invocation comes from the web playground (``channel="playground"``)
    — handlers that surface binding fields should tolerate that.
    """

    spec: CommandSpec
    raw_text: str  # full original line, stripped
    args_text: str  # everything after the matched alias (may be "")
    binding: ChannelBinding
    is_admin: bool


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Envelope returned by a command handler."""

    reply: str | None = None  # text to send back; None = no reply
    ephemeral: bool = False  # platform-supported "only sender sees it"


# Handlers may be sync or async. The dispatcher auto-awaits coroutines.
CommandHandler = Callable[
    ["CommandContext"], "CommandResult | Awaitable[CommandResult]"
]


# ---------------------------------------------------------------------------
# CommandSpec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandSpec:
    """Static metadata for a single slash command.

    A spec must carry at least one delivery path — either
    ``wizard_prelude`` (text injected to the agent turn) or ``handler``
    (direct sync/async callable). Specs that carry both are routed by
    surface: channel adapters prefer the handler, the web playground
    prefers the prelude.

    Fields
    ------
    name
        Canonical short identifier (no leading slash; ``"persona"``).
    aliases
        Every literal string a user can type to invoke the command.
        First entry is conventionally the primary Latin form
        (``"/persona"``); subsequent entries add localised aliases
        (``"/角色"``) and bare-word ergonomic forms (``"配置人格"``).
        Matching is case-sensitive.
    summary
        One-line description surfaced by ``/help``.
    category
        Coarse grouping for the auto-generated ``/help`` listing —
        ``"Session"`` / ``"Configuration"`` / ``"Info"`` etc.
        Defaults to ``"General"``.
    args_hint
        Short usage hint for ``/help`` (e.g. ``"<id>"``, ``"[label]"``).
        Empty when the command takes no args.
    admin_only
        When ``True``, the dispatcher requires the caller to satisfy
        :func:`is_command_admin` before running the handler / prelude.
        Non-admin attempts get a fixed "admin-only" reply.
    wizard_prelude
        SYSTEM-INSERTED text fed to the agent in place of the literal
        command. ``None`` when this command has no prelude path.
    handler
        Callable invoked synchronously (or awaited if a coroutine
        function) when the channel adapter dispatches the command.
        Returns a :class:`CommandResult`. ``None`` when this command
        has no handler path.
    """

    name: str
    aliases: tuple[str, ...]
    summary: str
    category: str = "General"
    args_hint: str = ""
    admin_only: bool = False
    wizard_prelude: str | None = None
    handler: CommandHandler | None = None


# ---------------------------------------------------------------------------
# Built-in prelude texts
# ---------------------------------------------------------------------------


_PERSONA_WIZARD_PRELUDE: str = (
    "[SYSTEM-INSERTED] The user invoked the /persona command. This is a "
    "staged materials-gathering wizard, NOT a registry browser. Follow "
    "the bundled `configure-persona` skill for the full script; the "
    "hard contract below is binding even if that skill is unavailable.\n"
    "\n"
    "HARD RULES (binding):\n"
    "- Your FIRST action MUST be `ask_user`. Do NOT call `persona_list` "
    "as the opening move — that turns the wizard into a list and is "
    "exactly what this command is fixing. `persona_list` is only "
    "permitted AFTER the user explicitly picks the `edit` branch in "
    "Stage 1.\n"
    "- Walk through 6 stages in order, no skipping, no merging:\n"
    "    Stage 1 — Identity (id + display_name; or `edit` branch)\n"
    "    Stage 2 — Text materials (身份/语气/口头禅/禁忌话题/示例对话)\n"
    "    Stage 3 — Sample corpus (N 条「角色会这样说」的对话样本)\n"
    "    Stage 4 — External links / files (URLs → web_fetch 摘要)\n"
    "    Stage 5 — Images (emoji + reference: 拖拽 / URL / 跳过)\n"
    "    Stage 6 — Compose + persist (draft system_prompt → confirm → "
    "`persona_create`)\n"
    "- End EVERY stage with one `ask_user` that pastes back the "
    "collected items as a numbered list and offers the FIXED four "
    "options: [\"确认\", \"补充\", \"修改\", \"重做\"]. Only on `确认` may "
    "you advance to the next stage. `补充` → continue collecting in "
    "this stage; `修改` → ask which item to change and re-ask only "
    "that one; `重做` → drop this stage's buffer and restart it.\n"
    "- Do NOT call `persona_create` until the user confirms the Stage "
    "6 整体草稿 (system_prompt + short_summary). No early persist.\n"
    "- Do NOT merge multiple stages into a single `ask_user`. One "
    "stage's review must complete before the next stage begins.\n"
    "\n"
    "Tools you will use: ask_user, persona_list (edit branch only), "
    "persona_get, persona_create, persona_update, persona_list_assets, "
    "persona_attach_asset_from_url, web_fetch (Stage 4)."
)


_PERSONA_LIST_PRELUDE: str = (
    "[SYSTEM-INSERTED] The user invoked the persona list shortcut. "
    "Call persona_list and render the result as a numbered list "
    "with each entry's id, display_name, and short_summary. Do not "
    "start a configuration wizard; this is a read-only listing."
)


# /help retains its prelude as the web-playground fallback. On
# channels, the handler below renders the same listing directly so we
# skip the LLM round-trip.
_HELP_PRELUDE: str = (
    "[SYSTEM-INSERTED] The user invoked /help. Respond with a short "
    "intro line followed by a bullet list of every registered slash "
    "command and its summary. Group by category if possible. Keep the "
    "response under 14 lines."
)


# ---------------------------------------------------------------------------
# Built-in handlers
# ---------------------------------------------------------------------------


def _render_help(ctx: CommandContext) -> CommandResult:
    """Auto-generate the slash-command listing from the live registry.

    Replaces the historically hardcoded ``_HELP_PRELUDE`` listing so
    new commands appear in ``/help`` automatically the moment they're
    registered. Hides ``admin_only`` commands from non-admins.
    """
    by_category: dict[str, list[CommandSpec]] = {}
    for spec in all_specs():
        if spec.admin_only and not ctx.is_admin:
            continue
        by_category.setdefault(spec.category, []).append(spec)
    if not by_category:
        return CommandResult(reply="(no commands registered)")

    lines: list[str] = ["可用命令 / Available commands:"]
    # Stable category ordering: General first, then alphabetical.
    cats = sorted(by_category.keys(), key=lambda c: (c != "General", c))
    for cat in cats:
        lines.append("")
        lines.append(f"[{cat}]")
        for spec in by_category[cat]:
            primary = spec.aliases[0] if spec.aliases else f"/{spec.name}"
            hint = f" {spec.args_hint}" if spec.args_hint else ""
            extra_aliases = list(spec.aliases[1:])
            alias_blurb = (
                f"  (aliases: {', '.join(extra_aliases)})"
                if extra_aliases
                else ""
            )
            lines.append(f"  {primary}{hint} — {spec.summary}{alias_blurb}")
    return CommandResult(reply="\n".join(lines))


def _render_whoami(ctx: CommandContext) -> CommandResult:
    """Dump the caller's channel binding so users can self-diagnose."""
    b = ctx.binding
    admin_tag = "yes" if ctx.is_admin else "no"
    lines = [
        "channel binding:",
        f"  channel: {b.channel}",
        f"  account: {b.account}",
        f"  thread:  {b.thread}",
        f"  sender:  {b.sender}",
        f"  session: {b.session_key()}",
        f"  admin:   {admin_tag}",
    ]
    return CommandResult(reply="\n".join(lines))


def _render_status(ctx: CommandContext) -> CommandResult:
    """Short bot-status line — kept minimal in v1.

    Future revisions may surface persona, bound model, uptime, agent
    busy/idle, etc. The handler stays simple today so the command can
    ship without new infra; the channel adapter can pass extra fields
    via ``ctx`` if needed.
    """
    return CommandResult(
        reply=(
            "corlinman online. Use /help to see commands. "
            f"Channel: {ctx.binding.channel}."
        )
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


COMMAND_REGISTRY: tuple[CommandSpec, ...] = (
    CommandSpec(
        name="persona",
        aliases=(
            "/persona",
            "/角色",
            "/人格",
            "配置人格",
            "配置角色",
        ),
        summary="启动 persona 配置向导",
        category="Configuration",
        wizard_prelude=_PERSONA_WIZARD_PRELUDE,
    ),
    CommandSpec(
        name="persona-list",
        aliases=(
            # ``/persona-list`` is the canonical Latin form; the
            # underscore variant exists so the BotFather menu (which
            # only accepts ``[a-z0-9_]{1,32}`` names) can register it
            # and round-trip user clicks back to a matching alias.
            "/persona-list",
            "/persona_list",
            "/角色列表",
            "/人格列表",
        ),
        summary="列出已注册的 persona",
        category="Configuration",
        wizard_prelude=_PERSONA_LIST_PRELUDE,
    ),
    CommandSpec(
        name="help",
        aliases=(
            "/help",
            "/帮助",
        ),
        summary="显示可用命令列表",
        category="Info",
        wizard_prelude=_HELP_PRELUDE,
        handler=_render_help,
    ),
    CommandSpec(
        name="whoami",
        aliases=(
            "/whoami",
            "/我是谁",
        ),
        summary="显示当前会话的 channel binding",
        category="Info",
        handler=_render_whoami,
    ),
    CommandSpec(
        name="status",
        aliases=(
            "/status",
            "/状态",
        ),
        summary="显示机器人当前状态",
        category="Info",
        handler=_render_status,
    ),
)


#: Mutable extension surface. External modules may append :class:`CommandSpec`
#: instances via :func:`register_command`. The dispatcher reads
#: :data:`COMMAND_REGISTRY` concatenated with this list.
runtime_registry: list[CommandSpec] = []


def all_specs() -> tuple[CommandSpec, ...]:
    """Snapshot of every registered spec — built-in + runtime.

    Returned as a tuple so callers can iterate without worrying about a
    concurrent :func:`register_command` mutating the list mid-walk.
    """
    return COMMAND_REGISTRY + tuple(runtime_registry)


def register_command(spec: CommandSpec) -> None:
    """Append ``spec`` to the runtime registry.

    Raises :class:`ValueError` when the spec collides with an existing
    name or alias, or when it has neither a prelude nor a handler.
    """
    if spec.wizard_prelude is None and spec.handler is None:
        raise ValueError(
            f"command {spec.name!r}: must set at least one of "
            "wizard_prelude / handler"
        )
    existing_names: set[str] = {s.name for s in all_specs()}
    if spec.name in existing_names:
        raise ValueError(f"command name {spec.name!r} already registered")
    existing_aliases: set[str] = {a for s in all_specs() for a in s.aliases}
    for alias in spec.aliases:
        if alias in existing_aliases:
            raise ValueError(f"command alias {alias!r} already registered")
    runtime_registry.append(spec)


def validate_registry(specs: tuple[CommandSpec, ...] | None = None) -> None:
    """Validate spec invariants. Raises :class:`ValueError` on failure.

    Invariants:

    * Every spec has at least one of ``wizard_prelude`` / ``handler``.
    * Names are globally unique.
    * Aliases are globally unique.
    * Each alias starts with ``/`` OR is a bare ergonomic form. (We do
      not enforce a specific prefix — Chinese ergonomic forms like
      ``"配置人格"`` are intentional.)
    """
    snapshot = specs if specs is not None else all_specs()
    seen_names: set[str] = set()
    seen_aliases: set[str] = set()
    for spec in snapshot:
        if spec.wizard_prelude is None and spec.handler is None:
            raise ValueError(
                f"command {spec.name!r}: must set at least one of "
                "wizard_prelude / handler"
            )
        if spec.name in seen_names:
            raise ValueError(f"duplicate command name {spec.name!r}")
        seen_names.add(spec.name)
        for alias in spec.aliases:
            if alias in seen_aliases:
                raise ValueError(f"duplicate command alias {alias!r}")
            seen_aliases.add(alias)


# Sanity-check the built-in set at import time so a typo in
# COMMAND_REGISTRY surfaces before any dispatch runs.
validate_registry(COMMAND_REGISTRY)


# ---------------------------------------------------------------------------
# Matching + substitution
# ---------------------------------------------------------------------------


def _scan_match(stripped: str) -> tuple[CommandSpec, str] | None:
    """Walk every registered spec and return the first match.

    Returns ``(spec, args_text)`` where ``args_text`` is the substring
    after the matched alias (with leading whitespace stripped); the
    full-alias-equality form yields ``args_text=""``.
    """
    for spec in all_specs():
        for alias in spec.aliases:
            if stripped == alias:
                return spec, ""
            prefix = alias + " "
            if stripped.startswith(prefix):
                return spec, stripped[len(prefix) :].lstrip()
    return None


def match_command(text: str) -> CommandSpec | None:
    """Return the matching :class:`CommandSpec` or ``None``.

    Matching rule (load-bearing — channel router + chat bootstrap both
    depend on this exact semantics, so any change must update the spec
    in :mod:`corlinman_channels.commands`):

    1. ``text`` is stripped of leading + trailing whitespace before any
       comparison. A pure-whitespace message returns ``None``.
    2. For each spec in :data:`COMMAND_REGISTRY` (then
       :data:`runtime_registry`), for each alias in ``spec.aliases``:
         a. If the stripped text equals the alias exactly → match.
         b. If the stripped text starts with ``alias + " "`` → match.
       The first match wins.
    3. Substring matches do not trigger.
    """
    stripped = text.strip()
    if not stripped:
        return None
    hit = _scan_match(stripped)
    return hit[0] if hit is not None else None


def match_command_with_args(text: str) -> tuple[CommandSpec, str] | None:
    """Same as :func:`match_command` but also returns the args substring.

    Returns ``(spec, args_text)`` on match where ``args_text`` is the
    text following the alias (leading whitespace stripped); ``""`` when
    the user typed only the alias. Returns ``None`` on no match.
    """
    stripped = text.strip()
    if not stripped:
        return None
    return _scan_match(stripped)


def apply_command_prelude(text: str, spec: CommandSpec) -> str:
    """Return the wizard prelude that should replace ``text``.

    Today this is a thin wrapper that returns ``spec.wizard_prelude``
    verbatim (the ``text`` argument is accepted but unused). When the
    spec has no prelude (handler-only command), returns ``text``
    unchanged — the playground / chat_bootstrap layer then leaves the
    literal text alone, and the channel layer is expected to invoke
    the handler via :func:`run_command_handler` instead.
    """
    del text  # reserved for future arg-token interpolation
    if spec.wizard_prelude is None:
        # Handler-only spec; nothing to inject. Caller (e.g.
        # chat_bootstrap) treats this as "no rewrite".
        return spec.wizard_prelude  # type: ignore[return-value]
    return spec.wizard_prelude


# ---------------------------------------------------------------------------
# Handler invocation
# ---------------------------------------------------------------------------


async def run_command_handler(
    spec: CommandSpec,
    ctx: CommandContext,
) -> CommandResult:
    """Invoke ``spec.handler`` and return its :class:`CommandResult`.

    Auto-awaits coroutine handlers; runs sync handlers inline. Admin
    gating happens here so callers don't need to duplicate the check
    — when ``spec.admin_only`` is set and ``ctx.is_admin`` is ``False``
    the handler is never called and a fixed denial reply is returned.

    Raises :class:`ValueError` if the spec has no handler.
    """
    if spec.handler is None:
        raise ValueError(
            f"command {spec.name!r} has no handler — "
            "this is a programming error in the caller"
        )
    if spec.admin_only and not ctx.is_admin:
        return CommandResult(
            reply=f"❌ {spec.aliases[0] if spec.aliases else spec.name} "
            "is an admin-only command.",
            ephemeral=True,
        )
    res: Any = spec.handler(ctx)
    if inspect.isawaitable(res):
        res = await res
    if not isinstance(res, CommandResult):
        raise TypeError(
            f"command {spec.name!r} handler returned "
            f"{type(res).__name__}, expected CommandResult"
        )
    return res


def _run_command_handler_sync(
    spec: CommandSpec,
    ctx: CommandContext,
) -> CommandResult:
    """Sync convenience wrapper around :func:`run_command_handler`.

    Used by surfaces that have no async context (the web playground's
    chat_bootstrap rewrite path). Refuses to run async handlers in this
    mode — they would deadlock the event loop — and falls back to a
    short "(not supported on this surface)" reply.
    """
    if spec.handler is None:
        raise ValueError(
            f"command {spec.name!r} has no handler — "
            "this is a programming error in the caller"
        )
    if spec.admin_only and not ctx.is_admin:
        return CommandResult(
            reply=f"❌ {spec.aliases[0] if spec.aliases else spec.name} "
            "is an admin-only command.",
            ephemeral=True,
        )
    if asyncio.iscoroutinefunction(spec.handler):
        return CommandResult(
            reply=(
                f"({spec.aliases[0] if spec.aliases else spec.name} requires "
                "an async surface — try it from the channel adapter)"
            )
        )
    res = spec.handler(ctx)
    if inspect.isawaitable(res):
        # Caller declared sync handler but returned a coroutine — same
        # fallback as iscoroutinefunction; we never block-await here.
        return CommandResult(
            reply=(
                f"({spec.aliases[0] if spec.aliases else spec.name} requires "
                "an async surface — try it from the channel adapter)"
            )
        )
    if not isinstance(res, CommandResult):
        raise TypeError(
            f"command {spec.name!r} handler returned "
            f"{type(res).__name__}, expected CommandResult"
        )
    return res


# ---------------------------------------------------------------------------
# Telegram BotFather command-menu export
# ---------------------------------------------------------------------------


import re as _re  # noqa: E402 — keep regex local to this section

_TG_NAME_RE = _re.compile(r"^[a-z0-9_]{1,32}$")


def telegram_bot_commands() -> list[dict[str, str]]:
    """Return the registry as a Telegram ``setMyCommands`` payload.

    Filters specs to Telegram's command-name rules
    (``[a-z0-9_]{1,32}``) — so ``persona-list`` (hyphen) is matched
    via its ``/persona_list`` alias instead of its canonical name.
    ``admin_only`` commands are excluded; admins still know to type
    them, but exposing them in every user's menu would be noisy.

    Returns a list of ``{"command": ..., "description": ...}`` dicts
    sorted by category then primary alias, ready to POST verbatim to
    the Telegram Bot API.
    """
    out: list[dict[str, str]] = []
    for spec in all_specs():
        if spec.admin_only:
            continue
        chosen: str | None = None
        # Prefer the canonical name when it matches the regex; else
        # scan aliases for a ``/``-prefixed Telegram-safe form.
        if _TG_NAME_RE.match(spec.name):
            chosen = spec.name
        else:
            for alias in spec.aliases:
                if not alias.startswith("/"):
                    continue
                stripped = alias[1:]
                if _TG_NAME_RE.match(stripped):
                    chosen = stripped
                    break
        if chosen is None:
            continue
        # Telegram truncates descriptions at ~256 chars; ours are
        # short already, but be defensive.
        out.append({"command": chosen, "description": spec.summary[:256]})
    # Sort by (category, command) so the BotFather menu has a stable
    # order across deploys. ``General`` sorts first by convention.
    by_name = {entry["command"]: entry for entry in out}
    name_to_spec: dict[str, CommandSpec] = {}
    for spec in all_specs():
        for alias in spec.aliases:
            if alias.startswith("/") and alias[1:] in by_name:
                name_to_spec[alias[1:]] = spec
                break
        if spec.name in by_name:
            name_to_spec[spec.name] = spec

    def _sort_key(entry: dict[str, str]) -> tuple[int, str, str]:
        spec = name_to_spec.get(entry["command"])
        cat = spec.category if spec else "General"
        return (0 if cat == "General" else 1, cat, entry["command"])

    out.sort(key=_sort_key)
    return out


# ---------------------------------------------------------------------------
# Admin gate
# ---------------------------------------------------------------------------


_COMMAND_ADMINS_ENV: str = "CORLINMAN_COMMAND_ADMINS"


def is_command_admin(binding: ChannelBinding) -> bool:
    """Return ``True`` when ``binding.sender`` is allow-listed.

    Reads the ``CORLINMAN_COMMAND_ADMINS`` env var, a comma-separated
    list of ``"<channel>:<sender>"`` strings (e.g.
    ``"qq:12345,telegram:67890"``). Returns ``True`` for any caller
    when the var is unset / empty — that preserves today's
    "no gating" stance for deployments that have not configured an
    admin list.
    """
    raw = os.environ.get(_COMMAND_ADMINS_ENV, "").strip()
    if not raw:
        return True
    needle = f"{binding.channel}:{binding.sender}"
    entries = {item.strip() for item in raw.split(",") if item.strip()}
    return needle in entries
