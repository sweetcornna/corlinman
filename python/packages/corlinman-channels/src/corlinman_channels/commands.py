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
    "permitted AFTER the user explicitly picks the `edit` branch.\n"
    "- Walk through 7 stages in order, no skipping, no merging:\n"
    "    Stage 0 — Character Source (公众人物 vs 自创角色，决定是否走调研分支)\n"
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
    "STAGE 0 CONTRACT (新阶段，决定后续是否走自动调研):\n"
    "- 首问 `ask_user` 两选一: [\"公众人物（自动调研 + 蒸馏）\", "
    "\"自创角色（手动配置）\"].\n"
    "- 选「自创角色」→ 直接进 Stage 1，行为和现版一致（手动 5 轮访谈）。\n"
    "- 选「公众人物」→ 走 Stage 0a / 0b / 0c：\n"
    "    Stage 0a — **只问一个** `ask_user`：「角色全名是？」用户答完立刻"
    "进 Stage 0b，**不要追问『有没有本地资料』之类的二次问题** —— 公众分支"
    "默认就是 auto-research。需要喂本地资料的场景留到 Stage 0c 审阅的 `补充` "
    "分支再问。\n"
    "    Stage 0b — 调研：**MUST call `web_search` 至少 1 次** 再 `web_fetch` "
    "2-3 个权威结果。**绝对禁止凭训练语料填 bucket** —— 抓取失败 ⚠️ 显式标"
    "注，让用户选 `重做` 或 `改自创`。\n"
    "    Stage 0c — 按 `huashu-nuwa` skill 的提炼框架（精简版）蒸馏出 5 个 "
    "bucket: identity / mental_models (2-3 个) / expression_dna / "
    "anti_patterns / honest_boundaries. 然后用四选项 `ask_user` 审阅这份"
    "蒸馏草稿。审阅时 `补充` 分支可以追加问「想喂本地一手资料再调研一轮吗？」"
    "（仅当用户对蒸馏不满意时才走这条）。\n"
    "- 确认 Stage 0c 后，把 5 bucket 作为会话 buffer 带入 Stage 1-3：\n"
    "    Stage 1 自动从名字生成 slug + display_name 让用户审阅（不再问 "
    "'id 是什么'）；Stage 2 用 buckets 直接填 5 个轴向后审阅；Stage 3 用 "
    "expression_dna 生成 few-shot 样本后审阅。**仍走每阶段的四选项审阅"
    "闸门**，绝不跳过。\n"
    "- Stage 6 起草 system_prompt 时把 honest_boundaries 作为 "
    "\"Limitations\" 段注入，防 persona 在不知道领域瞎编。\n"
    "\n"
    "Tools you will use: ask_user, persona_list (edit branch only), "
    "persona_get, persona_create, persona_update, persona_list_assets, "
    "persona_attach_asset_from_url, web_search (Stage 0b), web_fetch "
    "(Stage 0b + Stage 4)."
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


def _render_sethome(ctx: CommandContext) -> CommandResult:
    """Persist the caller's current binding as their "home channel".

    Sync handler — talks straight to
    :mod:`corlinman_server.home_channel_store` over a short SQLite
    connection. The store lives in corlinman-server, which is a soft
    dep of corlinman-channels (channels can run standalone in tests),
    so the import is lazy and a missing module degrades to an
    advisory failure reply instead of crashing the handler.

    On the playground surface (``channel=="playground"``) we still
    accept the write — the operator can pre-register a home channel
    from the web admin if they want — but the reply text mentions the
    synthetic binding so it's obvious where the row landed.
    """
    binding = ctx.binding
    try:
        from corlinman_server.home_channel_store import (  # noqa: PLC0415
            resolve_user_id,
            set_home,
        )
    except ImportError as exc:
        return CommandResult(
            reply=(
                "❌ 主聊天窗口设置不可用：corlinman-server 未安装"
                f" ({exc.name or 'home_channel_store'})。"
            )
        )

    user_id = resolve_user_id(binding.channel, binding.sender)
    try:
        set_home(
            user_id,
            channel=binding.channel,
            account=binding.account,
            thread=binding.thread,
            sender=binding.sender,
        )
    except Exception as exc:  # noqa: BLE001 — surface storage errors politely
        return CommandResult(
            reply=f"❌ 写入主聊天窗口失败：{exc}",
        )

    return CommandResult(
        reply=(
            "✅ 主聊天窗口已设置："
            f"{binding.channel}/{binding.thread}. "
            "重启与重要系统提醒将发送到此处。"
        )
    )


async def _render_use_default_persona(ctx: CommandContext) -> CommandResult:
    """Ensure the built-in ``grantley`` persona is seeded + selected.

    First-run-wizard contract D2: confirms the default helper is
    active so the user can chat immediately without going through
    the Stage 0–6 custom-persona flow.

    The persona store lives in corlinman-server and the active-
    persona surface today is the "is row present + ``is_builtin=True``"
    flag set by :func:`seed_builtin_personas` (see Agent B's owned
    ``personas.py`` route for the use-default endpoint). Calling the
    seeder directly is idempotent and matches what the HTTP endpoint
    does internally — so we go through the in-process path rather
    than spinning a localhost HTTP call (no auth complications, no
    extra socket).

    Async because the persona store's ``open`` / ``seed`` are
    coroutines; the slash-command dispatcher
    (:func:`run_command_handler`) auto-awaits coroutine handlers, so
    no change is required at the call site. The synthetic playground
    fallback in :func:`_run_command_handler_sync` already returns a
    polite "(requires async surface)" reply for async handlers, so
    invoking this on the web playground degrades gracefully.

    Falls back to a polite "(not wired)" reply when corlinman-server
    isn't importable (standalone channel tests).
    """
    del ctx  # binding not needed — persona selection is global today
    try:
        from corlinman_server.persona import (  # noqa: PLC0415
            DEFAULT_GRANTLEY_DISPLAY_NAME,
            DEFAULT_GRANTLEY_ID,
            PersonaStore,
            seed_builtin_personas,
        )
    except ImportError as exc:
        return CommandResult(
            reply=(
                "❌ 默认人格切换不可用：corlinman-server 未安装"
                f" ({exc.name or 'persona'})。"
            )
        )

    # Resolve the persona DB path the gateway opened at boot —
    # ``<data_dir>/personas.sqlite``, where ``data_dir`` follows the
    # same env-var precedence as ``gateway.lifecycle.entrypoint``.
    raw = os.environ.get("CORLINMAN_DATA_DIR")
    if raw:
        data_dir = os.path.abspath(raw)
    else:
        try:
            from pathlib import Path as _Path  # noqa: PLC0415

            data_dir = str(_Path.home() / ".corlinman")
        except (RuntimeError, OSError):
            data_dir = ".corlinman"

    from pathlib import Path as _Path  # noqa: PLC0415

    db_path = _Path(data_dir) / "personas.sqlite"
    try:
        store = await PersonaStore.open(db_path)
        try:
            await seed_builtin_personas(store)
        finally:
            await store.close()
    except Exception as exc:  # noqa: BLE001 — surface storage errors politely
        return CommandResult(
            reply=f"❌ 默认人格切换失败：{exc}",
        )

    return CommandResult(
        reply=(
            f"✅ 已切换至默认助手「{DEFAULT_GRANTLEY_DISPLAY_NAME}」"
            f"（id={DEFAULT_GRANTLEY_ID}）。直接发消息即可对话。"
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
    # First-run-wizard surface (W3 / docs/PLAN_FIRST_RUN_WIZARD.md
    # Agent D's slice). ``/sethome`` records the current channel
    # binding as the user's "main" thread; restart broadcasts and
    # other important system pings are sent only there.
    CommandSpec(
        name="sethome",
        aliases=(
            "/sethome",
            "/主页",
        ),
        summary="将当前聊天窗口设为主聊天窗口（重启等系统提醒的接收点）",
        category="Configuration",
        handler=_render_sethome,
    ),
    # ``/use-default-persona`` is the wizard's "use the built-in
    # helper" branch — seeds + activates the bundled ``grantley``
    # persona so a fresh deployment can chat without going through
    # the Stage 0–6 persona-wizard.
    CommandSpec(
        name="use-default-persona",
        aliases=(
            "/use-default-persona",
            "/use_default_persona",
            "/默认人格",
        ),
        summary="使用内置默认助手（grantley）",
        category="Configuration",
        handler=_render_use_default_persona,
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
