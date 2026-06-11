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
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from corlinman_channels.common import ChannelBinding

__all__ = [
    "COMMAND_REGISTRY",
    "CommandContext",
    "CommandHandler",
    "CommandResult",
    "CommandSpec",
    "SlashAccessPolicy",
    "SlashAccessTier",
    "all_specs",
    "apply_command_prelude",
    "is_command_admin",
    "load_commands_dir",
    "match_command",
    "match_command_with_args",
    "register_command",
    "register_commands_from_dir",
    "register_skill_command",
    "run_command_handler",
    "runtime_registry",
    "slash_access_policy_from_env",
    "substitute_arguments",
    "telegram_bot_commands",
    "unknown_command_notice",
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


def _render_new(ctx: CommandContext) -> CommandResult:
    """``/new`` — start a fresh conversation context on this binding.

    Bumps the binding's session epoch via
    :mod:`corlinman_channels.binding_prefs` (backed by the server-side
    SQLite store); the request builders fold the epoch into the derived
    session key, so the agent sees a brand-new conversation while every
    earlier epoch stays journaled and addressable.

    On the web playground / CLI console the binding is synthetic: the
    HTTP chat route derives its session key from the request body /
    ``X-Session-Key`` header (not from this binding), and the console
    intercepts ``/new`` locally before it ever reaches this handler.
    Bumping the synthetic binding's epoch would change nothing for the
    caller's actual conversation — so reply honestly instead of
    pretending a new session started.
    """
    from corlinman_channels import binding_prefs  # noqa: PLC0415 — soft-dep shim

    if ctx.binding.channel in ("playground", "console"):
        surface_hint = (
            "网页端请使用界面上的「新对话」按钮开启新会话。"
            if ctx.binding.channel == "playground"
            else "控制台请直接使用本地 /new（它会优先生效）。"
        )
        return CommandResult(
            reply=(
                "ℹ️ /new 作用于聊天频道（QQ/Telegram/Discord 等）的会话；"
                f"当前界面自行管理会话。{surface_hint}"
            )
        )

    prefs = binding_prefs.bump_session_epoch(ctx.binding)
    if prefs is None:
        return CommandResult(
            reply="❌ 新会话功能不可用：corlinman-server 偏好存储未安装。"
        )
    return CommandResult(
        reply=(
            "🆕 已开启新会话（第 "
            f"{prefs.session_epoch} 轮）。之前的对话已归档，"
            "随时可在会话列表中找回。"
        )
    )


def _render_model(ctx: CommandContext) -> CommandResult:
    """``/model [name|default]`` — show / set / clear the per-conversation
    model override.

    The override is applied by the channel request builders on every
    later turn of this binding. The name is not validated here — model
    resolution happens (and surfaces a clean upstream error) on the next
    turn, exactly like an admin-set alias typo would.
    """
    from corlinman_channels import binding_prefs  # noqa: PLC0415 — soft-dep shim

    if ctx.binding.channel in ("playground", "console"):
        # Synthetic surfaces don't route chats through the channel
        # request builders, so a persisted override would silently do
        # nothing — be honest instead (web picks its model in the UI;
        # the console's local /model takes precedence over this handler).
        return CommandResult(
            reply=(
                "/model 在此界面不生效：网页端请在界面里选择模型；"
                "CLI 控制台请直接使用本地 /model。频道会话（QQ/Telegram/"
                "Discord/Slack/飞书）中使用本命令可切换模型。"
            )
        )

    args = ctx.args_text.strip()
    if not args:
        prefs = binding_prefs.get_prefs(ctx.binding)
        current = getattr(prefs, "model_override", None) if prefs else None
        shown = current or "（默认模型）"
        return CommandResult(
            reply=(
                f"当前会话模型：{shown}\n"
                "用法：/model <模型名或别名> 切换；/model default 恢复默认。"
            )
        )
    clear = args.lower() in ("default", "reset", "clear", "默认")
    prefs = binding_prefs.set_model_override(ctx.binding, None if clear else args)
    if prefs is None:
        return CommandResult(
            reply="❌ 模型切换不可用：corlinman-server 偏好存储未安装。"
        )
    if clear:
        return CommandResult(reply="✅ 已恢复默认模型。")
    return CommandResult(
        reply=(
            f"✅ 本会话模型已切换为：{args}\n"
            "（名称在下一条消息时解析；写错会得到一条明确的错误提示。）"
        )
    )


#: ``/usage`` pagination — page size for ``list_session_turns`` plus a
#: hard page cap so a pathological session (or a backend bug that keeps
#: returning full pages) bounds the handler's worst case at
#: ``_USAGE_PAGE_SIZE * _USAGE_MAX_PAGES`` rows.
_USAGE_PAGE_SIZE: int = 200
_USAGE_MAX_PAGES: int = 25


async def _collect_session_turns(
    journal: Any,
    session_key: str,
    *,
    page_size: int = _USAGE_PAGE_SIZE,
    max_pages: int = _USAGE_MAX_PAGES,
) -> tuple[list[dict[str, Any]], bool]:
    """Drain ``list_session_turns`` for ``session_key`` via the cursor.

    ``list_session_turns`` returns at most ``limit`` rows ordered
    ``started_at_ms DESC``; conversations longer than one page need the
    ``before_turn_id`` cursor (the last — oldest — row of the previous
    page) to reach the earlier turns. Returns ``(rows, capped)`` where
    ``capped`` is True when ``max_pages`` full pages were read and older
    turns may be missing — the caller mentions the cap in the reply only
    in that case.
    """
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(max_pages):
        page = await journal.list_session_turns(
            session_key, limit=page_size, before_turn_id=cursor
        )
        rows.extend(page)
        if len(page) < page_size:
            return rows, False
        last_id = page[-1].get("turn_id")
        if last_id is None:
            # No cursor to continue from — stop rather than loop on the
            # same page forever.
            return rows, False
        cursor = str(last_id)
    return rows, True


async def _render_usage(ctx: CommandContext) -> CommandResult:
    """``/usage`` — turn count + estimated cost for this conversation.

    Async handler — reads the agent journal for the binding's
    *effective* session key (current epoch), paging through every turn
    via the ``before_turn_id`` cursor (capped — see
    :data:`_USAGE_MAX_PAGES`). Degrades to an advisory message when the
    journal/server package is unavailable.
    """
    try:
        from corlinman_server.agent_journal import AgentJournal  # noqa: PLC0415

        from corlinman_channels import binding_prefs  # noqa: PLC0415
    except ImportError:
        return CommandResult(reply="❌ 用量统计不可用：corlinman-server 未安装。")

    base_key = ctx.binding.session_key()
    session_key = binding_prefs.effective_session_key(ctx.binding, base_key)
    journal_path = Path(_channels_data_dir()) / "agent_journal.sqlite"
    # Only gate on the sqlite file's existence when the configured
    # backend actually IS sqlite (unset env = sqlite default). Postgres /
    # Redis deployments journal elsewhere and have no local file to
    # probe, so they proceed straight to ``open_from_env`` — mirrors
    # ``corlinman_server.console.app._open_journal``.
    backend = (os.environ.get("CORLINMAN_JOURNAL_BACKEND") or "").lower()
    if backend in ("", "sqlite") and not journal_path.is_file():
        return CommandResult(reply="本会话还没有任何记录。")
    try:
        journal = await AgentJournal.open_from_env(journal_path)
        try:
            turns, capped = await _collect_session_turns(journal, session_key)
        finally:
            await journal.close()
    except Exception as exc:  # noqa: BLE001 — stats are best-effort
        return CommandResult(reply=f"❌ 读取用量失败：{exc}")
    if not turns:
        return CommandResult(reply="本会话还没有任何记录。")
    total_cost = 0.0
    tool_calls = 0
    for row in turns:
        try:
            total_cost += float(row.get("estimated_cost_usd") or 0.0)
        except (TypeError, ValueError):
            pass
        try:
            tool_calls += int(row.get("tool_call_count") or 0)
        except (TypeError, ValueError):
            pass
    cost_part = f" · 估算成本 ~${total_cost:.4f}" if total_cost > 0 else ""
    cap_part = (
        f"（仅统计最近 {_USAGE_PAGE_SIZE * _USAGE_MAX_PAGES} 轮，更早的轮次未计入）"
        if capped
        else ""
    )
    return CommandResult(
        reply=(
            f"本会话用量：{len(turns)} 轮 · {tool_calls} 次工具调用"
            f"{cost_part}{cap_part}"
        )
    )


def _channels_data_dir() -> str:
    """Resolve the gateway data dir from the environment (handler-local).

    Mirrors the env-var precedence used by
    ``gateway.lifecycle.entrypoint`` and the persona-store handler:
    ``$CORLINMAN_DATA_DIR`` → ``~/.corlinman`` → ``.corlinman``. Kept as
    a tiny helper so the status-card handlers don't each re-implement it.
    """
    raw = os.environ.get("CORLINMAN_DATA_DIR")
    if raw:
        return os.path.abspath(raw)
    try:
        from pathlib import Path as _Path  # noqa: PLC0415

        return str(_Path.home() / ".corlinman")
    except (RuntimeError, OSError):
        return ".corlinman"


def _render_status(ctx: CommandContext) -> CommandResult:
    """Short bot-status line, plus the caller's shareable status link.

    Always emits a one-line status header. When the shareable
    agent-status-card feature is configured (see
    :func:`corlinman_channels.service.configure_status_links`), the
    caller's own signed link (``{public_url}/status/{token}``) is
    appended on a fresh line so they can tap through to a live
    trajectory view. When the feature is off the helper returns ``""``
    and we emit just the header — never a broken/empty link.

    Subcommand ``/status revoke`` (admin-only) invalidates every
    outstanding link for the caller's session (#34): it bumps the
    session's revocation epoch via
    :func:`corlinman_server.gateway.status_revocation.revoke_session`,
    so links already shared stop resolving (their baked-in epoch falls
    behind) while a fresh ``/status`` immediately mints a working one.

    The link helper lives in :mod:`corlinman_channels.service`, which
    imports this module — so the import is lazy (inside the function)
    to dodge the commands<->service circular import, and wrapped in
    ``try/except`` so a missing ``corlinman_server`` or a disabled
    feature degrades gracefully rather than crashing.
    """
    subcommand = ctx.args_text.strip().lower()

    # /status revoke — invalidate the caller's outstanding links (admin).
    if subcommand in ("revoke", "撤销", "吊销"):
        if not ctx.is_admin:
            return CommandResult(
                reply="❌ 仅管理员可以吊销状态链接。",
                ephemeral=True,
            )
        try:
            from pathlib import Path as _Path  # noqa: PLC0415

            from corlinman_server.gateway.status_revocation import (  # noqa: PLC0415
                revoke_session,
            )

            from corlinman_channels import binding_prefs  # noqa: PLC0415

            # Epoch-adjusted (matches the mint below): revoke the links
            # for the CURRENT conversation, not the pre-/new session.
            new_epoch = revoke_session(
                _Path(_channels_data_dir()),
                binding_prefs.effective_session_key(
                    ctx.binding, ctx.binding.session_key()
                ),
            )
        except Exception:  # noqa: BLE001 — never crash the handler
            return CommandResult(
                reply="❌ 吊销不可用：corlinman-server 未安装或数据目录不可写。",
                ephemeral=True,
            )
        if new_epoch <= 0:
            return CommandResult(
                reply="❌ 吊销未生效（无法持久化撤销状态）。",
                ephemeral=True,
            )
        return CommandResult(
            reply=(
                f"✅ 已吊销本会话此前的所有状态链接（撤销版本 #{new_epoch}）。"
                "之前分享出去的链接将立即失效；再次发送 /status 可获取新链接。"
            )
        )

    status_line = (
        "corlinman online. Use /help to see commands. "
        f"Channel: {ctx.binding.channel}."
    )

    link_line = ""
    try:
        from corlinman_channels import binding_prefs  # noqa: PLC0415
        from corlinman_channels.service import (  # noqa: PLC0415
            _status_link_line,
        )

        # Epoch-adjusted: after /new the live turns run under
        # ``<base>:eN`` — mint the link for that session, not the dead
        # epoch-0 one.
        link_line = _status_link_line(
            binding_prefs.effective_session_key(
                ctx.binding, ctx.binding.session_key()
            )
        )
    except Exception:  # noqa: BLE001 — a status link must never break the reply
        link_line = ""

    reply = f"{status_line}\n{link_line}" if link_line else status_line
    return CommandResult(reply=reply)


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
        summary="显示机器人状态 + 实时状态链接（/status revoke 吊销旧链接，仅管理员）",
        category="Info",
        args_hint="[revoke]",
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
    # Session commands — shared across every surface (channels, web
    # playground, CLI console). Backed by the per-binding prefs store
    # (corlinman_server.binding_prefs_store) via the soft-dep shim in
    # binding_prefs.py; see docs/PLAN_CLAUDECODE_PARITY.md.
    CommandSpec(
        name="new",
        aliases=("/new", "/新会话", "/重新开始"),
        summary="开启新会话（旧对话归档可找回）",
        category="Session",
        handler=_render_new,
    ),
    CommandSpec(
        name="model",
        aliases=("/model", "/模型"),
        summary="查看或切换本会话使用的模型",
        category="Session",
        args_hint="[名称|default]",
        handler=_render_model,
    ),
    CommandSpec(
        name="usage",
        aliases=("/usage", "/用量"),
        summary="查看本会话的轮数与估算成本",
        category="Session",
        handler=_render_usage,
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


def apply_command_prelude(
    text: str,
    spec: CommandSpec,
    *,
    args_text: str | None = None,
) -> str:
    """Return the wizard prelude that should replace ``text``.

    When the spec has no prelude (handler-only command), returns ``text``
    unchanged — the playground / chat_bootstrap layer then leaves the
    literal text alone, and the channel layer is expected to invoke
    the handler via :func:`run_command_handler` instead.

    CMP-07: when ``args_text`` is supplied (the text the user typed after
    the matched alias), ``$ARGUMENTS`` / ``$1``..``$N`` tokens in the
    prelude are substituted via :func:`substitute_arguments`. This is what
    makes a ``commands/foo.md`` body with a ``$ARGUMENTS`` placeholder
    actually receive the user's args. ``None`` (the default) preserves the
    historical verbatim behaviour for callers that don't have the args
    handy (e.g. the web playground's static rewrite).
    """
    del text  # the literal text is not injected; the prelude replaces it
    if spec.wizard_prelude is None:
        # Handler-only spec; nothing to inject. Caller (e.g.
        # chat_bootstrap) treats this as "no rewrite".
        return spec.wizard_prelude  # type: ignore[return-value]
    if args_text is not None:
        return substitute_arguments(spec.wizard_prelude, args_text)
    return spec.wizard_prelude


# ---------------------------------------------------------------------------
# Handler invocation
# ---------------------------------------------------------------------------


def _policy_refusal(
    spec: CommandSpec,
    policy: SlashAccessPolicy,
    binding: ChannelBinding,
    *,
    is_dm: bool,
    is_admin: bool,
) -> CommandResult | None:
    """Return a refusal :class:`CommandResult` when ``policy`` denies ``spec``.

    Returns ``None`` when the policy permits the call (the caller proceeds).
    Centralises CMP-06 enforcement so both the sync and async dispatch
    wrappers share one denial message + tier-aware wording.
    """
    if policy.allows(spec, binding, is_dm=is_dm, is_admin=is_admin):
        return None
    alias = spec.aliases[0] if spec.aliases else spec.name
    tier = policy.tier_for(spec)
    if tier == SlashAccessTier.DM_ONLY:
        msg = f"❌ {alias} 仅支持私聊使用。"
    else:
        msg = f"❌ {alias} is restricted to administrators."
    return CommandResult(reply=msg, ephemeral=True)


async def run_command_handler(
    spec: CommandSpec,
    ctx: CommandContext,
    *,
    policy: SlashAccessPolicy | None = None,
    is_dm: bool = False,
) -> CommandResult:
    """Invoke ``spec.handler`` and return its :class:`CommandResult`.

    Auto-awaits coroutine handlers; runs sync handlers inline. Admin
    gating happens here so callers don't need to duplicate the check
    — when ``spec.admin_only`` is set and ``ctx.is_admin`` is ``False``
    the handler is never called and a fixed denial reply is returned.

    When ``policy`` is supplied (CMP-06), :meth:`SlashAccessPolicy.allows`
    is consulted before the handler runs. ``is_dm`` flags whether the
    inbound came from a 1:1 chat so a ``DM_ONLY`` command is refused in a
    group. A denied call never invokes the handler — it returns an
    ephemeral refusal :class:`CommandResult` instead. ``None`` (default)
    preserves the historical allow-by-default behaviour (only the
    ``admin_only`` flag gates).

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
    if policy is not None:
        refusal = _policy_refusal(
            spec, policy, ctx.binding, is_dm=is_dm, is_admin=ctx.is_admin
        )
        if refusal is not None:
            return refusal
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
    *,
    policy: SlashAccessPolicy | None = None,
    is_dm: bool = False,
) -> CommandResult:
    """Sync convenience wrapper around :func:`run_command_handler`.

    Used by surfaces that have no async context (the web playground's
    chat_bootstrap rewrite path). Async handlers are handled by context:

    * **No event loop running on this thread** (FastAPI sync routes /
      threadpool workers, plain scripts, sync tests): it is safe to
      drive the coroutine to completion with :func:`asyncio.run` — this
      is what lets ``/usage`` (async — journal reads) work from a sync
      surface instead of refusing.
    * **A loop IS running on this thread**: blocking on the coroutine
      here would deadlock that loop (it cannot make progress while we
      wait on work it must itself execute), so we keep the polite
      "(requires an async surface)" refusal. Callers that hold a live
      loop should use :func:`run_command_handler` instead.

    Honours the same ``policy`` / ``is_dm`` CMP-06 gate as
    :func:`run_command_handler`.
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
    if policy is not None:
        refusal = _policy_refusal(
            spec, policy, ctx.binding, is_dm=is_dm, is_admin=ctx.is_admin
        )
        if refusal is not None:
            return refusal

    if asyncio.iscoroutinefunction(spec.handler):
        handler_fn: CommandHandler = spec.handler
        loop_running = True
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            loop_running = False  # no running loop — safe to block here
        if not loop_running:

            async def _drive() -> CommandResult:
                res_async: Any = handler_fn(ctx)
                if inspect.isawaitable(res_async):
                    res_async = await res_async
                if not isinstance(res_async, CommandResult):
                    raise TypeError(
                        f"command {spec.name!r} handler returned "
                        f"{type(res_async).__name__}, expected CommandResult"
                    )
                return res_async

            return asyncio.run(_drive())
        return CommandResult(
            reply=(
                f"({spec.aliases[0] if spec.aliases else spec.name} requires "
                "an async surface — try it from the channel adapter)"
            )
        )
    res = spec.handler(ctx)
    if inspect.isawaitable(res):
        # Caller declared a sync handler but returned a coroutine — the
        # sync body already ran once, so re-invoking it via asyncio.run
        # (like the coroutine-function branch above) could double its
        # side effects. Keep the historical refusal; we never block-
        # await here.
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


# ---------------------------------------------------------------------------
# $ARGUMENTS / $1.. substitution
# ---------------------------------------------------------------------------


#: Matches ``$ARGUMENTS``, ``$1`` … ``$9`` (and ``${1}`` brace form). The
#: brace form lets a template write ``${1}files`` without the digit
#: greedily swallowing the trailing word.
_ARG_TOKEN_RE = re.compile(r"\$(?:\{(\d+|ARGUMENTS)\}|(ARGUMENTS|\d+))")


def substitute_arguments(template: str, args_text: str) -> str:
    """Substitute ``$ARGUMENTS`` / ``$1``..``$N`` in ``template``.

    Mirrors the Claude-Code / hermes commands-dir convention used by
    ``*.md`` command bodies:

    * ``$ARGUMENTS`` → the full args string (everything the user typed
      after the command alias), verbatim.
    * ``$1`` … ``$N`` → the N-th whitespace-delimited positional token.
      Out-of-range positions substitute to ``""``.
    * ``${1}`` / ``${ARGUMENTS}`` brace forms are equivalent and useful
      when a digit would otherwise run into a following word.

    Unmatched ``$`` sequences (``$foo``, a bare ``$``) are left intact —
    only the recognised tokens are rewritten — so a template containing
    shell snippets or prices isn't mangled.
    """
    positional = args_text.split()

    def _repl(m: re.Match[str]) -> str:
        token = m.group(1) or m.group(2)
        if token == "ARGUMENTS":
            return args_text
        idx = int(token)
        if idx <= 0:
            return ""
        return positional[idx - 1] if idx <= len(positional) else ""

    return _ARG_TOKEN_RE.sub(_repl, template)


# ---------------------------------------------------------------------------
# Commands-dir (*.md) loader
# ---------------------------------------------------------------------------


def _parse_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    """Split a ``---``-delimited YAML-ish frontmatter block off ``raw``.

    Returns ``(meta, body)``. We deliberately do NOT depend on PyYAML
    (it is not a guaranteed dep and the prod VPS is memory-constrained);
    the frontmatter we care about is flat ``key: value`` scalars, which a
    line parser handles. Quotes around the value are stripped. A document
    without an opening ``---`` returns ``({}, raw)``.

    Values may be wrapped in matching single/double quotes; comma lists
    (e.g. ``aliases: /foo, /bar``) are left as the raw string for the
    caller to split — keeping this parser dependency-free and total.
    """
    if not raw.startswith("---"):
        return {}, raw
    # Find the closing fence. The first line is the opening ``---``.
    lines = raw.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, raw
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            meta[key] = value
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    return meta, body


def _aliases_for(name: str, meta: dict[str, str]) -> tuple[str, ...]:
    """Compute the alias tuple for a loaded command.

    The canonical alias is always ``/<name>``. Additional aliases come
    from a comma-separated ``aliases:`` frontmatter key. Aliases that
    don't already carry a leading ``/`` (and aren't a bare-word ergonomic
    form) are normalised to ``/<alias>`` so authors can write
    ``aliases: foo, bar`` ergonomically.
    """
    aliases: list[str] = [f"/{name}"]
    raw = meta.get("aliases", "")
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        # Bare CJK / ergonomic forms (no ASCII slug) pass through verbatim;
        # ASCII slugs without a slash get one prepended.
        if not item.startswith("/") and re.fullmatch(r"[A-Za-z0-9_-]+", item):
            item = f"/{item}"
        if item not in aliases:
            aliases.append(item)
    return tuple(aliases)


def _spec_from_md(name: str, raw: str) -> CommandSpec:
    """Build a :class:`CommandSpec` from one ``*.md`` command document.

    The markdown body becomes the ``wizard_prelude`` (with ``$ARGUMENTS``
    left intact — :func:`run_command_handler` / the router perform the
    per-invocation substitution). Frontmatter keys:

    * ``description`` / ``summary`` → :attr:`CommandSpec.summary`
    * ``aliases`` → extra aliases (comma list)
    * ``category`` → :attr:`CommandSpec.category`
    * ``args_hint`` / ``argument-hint`` → :attr:`CommandSpec.args_hint`
    * ``admin_only`` (``true``/``1``/``yes``) → :attr:`CommandSpec.admin_only`
    """
    meta, body = _parse_frontmatter(raw)
    summary = meta.get("description") or meta.get("summary") or name
    args_hint = meta.get("args_hint") or meta.get("argument-hint") or ""
    admin_only = meta.get("admin_only", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    return CommandSpec(
        name=name,
        aliases=_aliases_for(name, meta),
        summary=summary,
        category=meta.get("category", "Custom"),
        args_hint=args_hint,
        admin_only=admin_only,
        wizard_prelude=body or summary,
    )


def load_commands_dir(directory: str | Path) -> list[CommandSpec]:
    """Load every ``*.md`` command document under ``directory``.

    Returns a list of (un-registered) :class:`CommandSpec`. The command
    name is the file stem; the markdown body becomes the agent-injected
    prelude. A missing directory returns ``[]`` (callers can point this at
    an optional ``commands/`` folder that may not exist). Files are loaded
    in sorted order so two specs that collide surface deterministically.
    """
    path = Path(directory)
    if not path.is_dir():
        return []
    out: list[CommandSpec] = []
    for md in sorted(path.glob("*.md")):
        try:
            raw = md.read_text(encoding="utf-8")
        except OSError:
            continue
        out.append(_spec_from_md(md.stem, raw))
    return out


def register_commands_from_dir(directory: str | Path) -> list[CommandSpec]:
    """Load + :func:`register_command` every ``*.md`` under ``directory``.

    Returns the specs that were successfully registered. Specs whose name
    or aliases collide with an already-registered command are skipped (a
    bundled command already owns that surface) rather than raising — the
    directory is an additive extension point, not a source of truth.
    """
    registered: list[CommandSpec] = []
    for spec in load_commands_dir(directory):
        try:
            register_command(spec)
        except ValueError:
            continue
        registered.append(spec)
    return registered


# ---------------------------------------------------------------------------
# Skill → command bridge
# ---------------------------------------------------------------------------


def register_skill_command(
    *,
    name: str,
    summary: str,
    prelude: str | None = None,
    handler: CommandHandler | None = None,
    aliases: tuple[str, ...] = (),
    category: str = "Skills",
    args_hint: str = "",
    admin_only: bool = False,
) -> CommandSpec | None:
    """Register a slash command sourced from a skill's frontmatter.

    Skills may declare a ``command`` invocation surface in their
    ``SKILL.md`` frontmatter; this bridges that declaration into the
    channel command registry so ``/<name>`` triggers the skill on every
    channel + the web playground. The default delivery is a prelude that
    points the agent at the named skill.

    Returns the registered :class:`CommandSpec`, or ``None`` when the
    name/alias already exists (idempotent re-seed on reload).
    """
    alias_tuple: tuple[str, ...] = (f"/{name}", *aliases)
    if prelude is None and handler is None:
        prelude = (
            f"[SYSTEM-INSERTED] The user invoked /{name}. Use the "
            f"`{name}` skill to handle this request. User arguments: "
            "$ARGUMENTS"
        )
    spec = CommandSpec(
        name=name,
        aliases=alias_tuple,
        summary=summary,
        category=category,
        args_hint=args_hint,
        admin_only=admin_only,
        wizard_prelude=prelude,
        handler=handler,
    )
    try:
        register_command(spec)
    except ValueError:
        return None
    return spec


# ---------------------------------------------------------------------------
# Slash access policy (admin / DM / allowlist tiers)
# ---------------------------------------------------------------------------


class SlashAccessTier(StrEnum):
    """Coarse authorization tier required to invoke a command surface.

    Preserves corlinman's allow-by-default polarity: ``PUBLIC`` is the
    default and lets anyone run the command (matching the pre-policy
    behaviour). The stricter tiers are opt-in per command / per policy.
    """

    PUBLIC = "public"
    """Anyone on an enabled channel may invoke (default — allow-by-default)."""

    DM_ONLY = "dm_only"
    """Only direct messages (private chats), regardless of admin status.
    Used for commands that leak per-user state into a group otherwise."""

    ALLOWLIST = "allowlist"
    """Only senders on the configured admin/allowlist
    (:func:`is_command_admin`)."""

    ADMIN = "admin"
    """Synonym for ALLOWLIST kept for call-site readability."""


@dataclass(slots=True)
class SlashAccessPolicy:
    """Decide whether a caller may invoke a given command.

    Allow-by-default: a command with no explicit tier resolves to
    ``PUBLIC`` and is always permitted. The policy maps command *names*
    to a :class:`SlashAccessTier`; a spec's own ``admin_only`` flag is
    honoured as an implicit ``ALLOWLIST`` tier so the two mechanisms
    compose without surprising precedence.

    ``default_tier`` lets a deployment flip the global polarity (e.g.
    lock every command to ``ALLOWLIST`` for a private bot) while still
    allow-by-default for any deployment that doesn't configure it.
    """

    tiers: dict[str, SlashAccessTier] = field(default_factory=dict)
    default_tier: SlashAccessTier = SlashAccessTier.PUBLIC

    def tier_for(self, spec: CommandSpec) -> SlashAccessTier:
        """Resolve the effective tier for ``spec``.

        Precedence: an explicit per-name entry wins; otherwise the spec's
        ``admin_only`` flag implies ``ALLOWLIST``; otherwise the policy's
        ``default_tier``.
        """
        explicit = self.tiers.get(spec.name)
        if explicit is not None:
            return explicit
        if spec.admin_only:
            return SlashAccessTier.ALLOWLIST
        return self.default_tier

    def allows(
        self,
        spec: CommandSpec,
        binding: ChannelBinding,
        *,
        is_dm: bool,
        is_admin: bool | None = None,
    ) -> bool:
        """Return ``True`` when ``binding`` may invoke ``spec``.

        ``is_dm`` flags whether the inbound came from a private chat.
        ``is_admin`` is resolved via :func:`is_command_admin` when not
        supplied so callers can stay terse.
        """
        tier = self.tier_for(spec)
        if tier == SlashAccessTier.PUBLIC:
            return True
        if tier == SlashAccessTier.DM_ONLY:
            return is_dm
        # ALLOWLIST / ADMIN.
        if is_admin is None:
            is_admin = is_command_admin(binding)
        return bool(is_admin)


#: Env vars that configure the slash-access policy (CMP-06).
#:
#: * ``CORLINMAN_SLASH_DEFAULT_TIER`` — the global default tier applied to
#:   commands without an explicit per-name entry (``public`` / ``dm_only`` /
#:   ``allowlist`` / ``admin``). Unset / unrecognised → ``public`` (the
#:   historical allow-by-default polarity).
#: * ``CORLINMAN_SLASH_TIERS`` — comma list of ``<name>=<tier>`` per-command
#:   overrides (e.g. ``persona=dm_only,status=allowlist``).
_SLASH_DEFAULT_TIER_ENV: str = "CORLINMAN_SLASH_DEFAULT_TIER"
_SLASH_TIERS_ENV: str = "CORLINMAN_SLASH_TIERS"


def _parse_tier(raw: str) -> SlashAccessTier | None:
    """Map an env-string to a :class:`SlashAccessTier`. ``None`` on miss."""
    try:
        return SlashAccessTier(raw.strip().lower())
    except ValueError:
        return None


def slash_access_policy_from_env() -> SlashAccessPolicy | None:
    """Build a :class:`SlashAccessPolicy` from the environment (CMP-06).

    Returns ``None`` when neither env var is set / both are no-ops, so the
    dispatch path stays on the historical allow-by-default behaviour (no
    policy consulted). A deployment opts in by setting
    ``CORLINMAN_SLASH_DEFAULT_TIER`` (flip the global polarity) and/or
    ``CORLINMAN_SLASH_TIERS`` (per-command overrides).
    """
    default_raw = os.environ.get(_SLASH_DEFAULT_TIER_ENV, "").strip()
    tiers_raw = os.environ.get(_SLASH_TIERS_ENV, "").strip()
    if not default_raw and not tiers_raw:
        return None

    default_tier = _parse_tier(default_raw) or SlashAccessTier.PUBLIC
    tiers: dict[str, SlashAccessTier] = {}
    for item in tiers_raw.split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        name, _, tier_str = item.partition("=")
        name = name.strip().lstrip("/")
        tier = _parse_tier(tier_str)
        if name and tier is not None:
            tiers[name] = tier

    if default_tier == SlashAccessTier.PUBLIC and not tiers:
        # Nothing actually restricts anything — keep allow-by-default and
        # avoid the per-dispatch policy check.
        return None
    return SlashAccessPolicy(tiers=tiers, default_tier=default_tier)


# ---------------------------------------------------------------------------
# Unknown-command notice
# ---------------------------------------------------------------------------


def _looks_like_command(text: str) -> bool:
    """True when ``text`` *looks* like a slash command invocation.

    A leading-slash token of ASCII / CJK word characters. Used to decide
    whether an unmatched message warrants an "unknown command" hint vs.
    being plain prose that happens to start with a slash (a URL path, a
    fraction, ...). We require the first token to be reasonably command-
    shaped: ``/`` followed by 1+ word chars and no whitespace inside the
    token itself.
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return False
    first = stripped.split(maxsplit=1)[0]
    # ``/`` alone, ``//x`` (comment-ish), or a path with another slash is
    # not a command shape.
    body = first[1:]
    if not body or "/" in body:
        return False
    return bool(re.fullmatch(r"[\w\-]+", body, flags=re.UNICODE))


def unknown_command_notice(text: str, *, max_suggestions: int = 3) -> str | None:
    """Return a hint string when ``text`` is an unknown slash command.

    Contract:

    * Returns ``None`` when ``text`` matches a registered command
      (:func:`match_command`) — the caller dispatches normally.
    * Returns ``None`` when ``text`` doesn't look like a command at all
      (no leading slash / not command-shaped) — plain prose is forwarded
      to the agent untouched, preserving allow-by-default.
    * Otherwise returns a short notice listing the closest registered
      aliases so the user can self-correct.

    The suggestion set is the registered ``/``-prefixed aliases sharing
    the longest common prefix with the typed token, capped at
    ``max_suggestions``; falls back to ``/help``.
    """
    stripped = text.strip()
    if not _looks_like_command(stripped):
        return None
    if match_command(stripped) is not None:
        return None

    typed = stripped.split(maxsplit=1)[0]
    typed_lower = typed.lower()

    slash_aliases: list[str] = []
    for spec in all_specs():
        for alias in spec.aliases:
            if alias.startswith("/"):
                slash_aliases.append(alias)

    def _shared_prefix_len(a: str, b: str) -> int:
        n = 0
        for ca, cb in zip(a, b, strict=False):
            if ca == cb:
                n += 1
            else:
                break
        return n

    scored = sorted(
        slash_aliases,
        key=lambda a: (-_shared_prefix_len(a.lower(), typed_lower), a),
    )
    # Keep only candidates that share at least the leading slash + 1 char
    # so we don't suggest wholly unrelated commands.
    suggestions = [
        a for a in scored if _shared_prefix_len(a.lower(), typed_lower) >= 2
    ][:max_suggestions]

    if suggestions:
        hint = "，".join(suggestions)
        return (
            f"未知命令 {typed}。你是不是想输入：{hint}？"
            "发送 /help 查看全部命令。"
        )
    return f"未知命令 {typed}。发送 /help 查看全部可用命令。"
