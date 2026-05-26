"""Shared slash-command registry for inbound channels + web playground.

W8 of ``docs/PLAN_PERSONA_STUDIO.md``. The user wants a single keystroke
("/persona") to start a guided persona-setup conversation. Rather than
relying on the model to spontaneously notice intent, we intercept a
small set of literal commands at the channel-router seam (for QQ /
Telegram / Discord / Slack / Feishu / WeChat) AND at the gateway chat
bootstrap (for the admin / web playground) and rewrite the inbound user
turn to a **wizard prelude** before it reaches the agent. The original
literal text is preserved on the inbox row for audit; only the agent's
view of the user message is rewritten.

Why a shared module
-------------------

Two surfaces consume this:

* :mod:`corlinman_channels.router` (``ChannelRouter.dispatch``) — the
  channel-side rewrite, applied once per inbound :class:`MessageEvent`.
* :mod:`corlinman_server.gateway.services.chat_bootstrap` (the web
  message-assembly helper) — applied to the trailing user message of an
  :class:`InternalChatRequest` so the admin playground behaves the same
  as a QQ private message.

Sharing the registry guarantees the channel and the web playground stay
in lockstep: adding a new command (e.g. ``/skills``) is a one-file edit
that lights both surfaces up simultaneously.

Matching contract
-----------------

The matcher fires on **whole-stripped-message exact match** of any
registered alias, OR a message whose first whitespace-delimited token
equals an alias (the "command + args" form). Partial substring matches
inside longer prose intentionally do NOT trigger — that keeps the agent
free to discuss personas, slash-commands, etc. without invoking the
wizard accidentally. See :func:`match_command` for the precise rule.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "COMMAND_REGISTRY",
    "CommandSpec",
    "apply_command_prelude",
    "match_command",
]


# ---------------------------------------------------------------------------
# Wizard prelude texts
# ---------------------------------------------------------------------------
#
# Kept as module-level strings so they're easy to grep for and tweak
# without touching the spec definitions below.


_PERSONA_WIZARD_PRELUDE: str = (
    "[SYSTEM-INSERTED] The user invoked the /persona command. Walk them "
    "through configuring a persona using the persona.* tools and "
    "ask_user. Required fields: id (lowercase slug, 1-64 chars, "
    "[a-z0-9_-]), display_name, and a system_prompt that captures the "
    "persona's voice / style. Optional: upload emoji + reference images "
    "by directing them to the /admin/persona UI for drag-drop, OR ask "
    "them to paste image URLs and call persona.attach_asset_from_url "
    "yourself. Steps:\n"
    "1. Greet the user and ask whether they want to create a new "
    "persona or edit one. Use ask_user for the question.\n"
    "2. If create: ask for the id and display_name, then conduct a 3-5 "
    "turn voice/style interview (one ask_user question per turn).\n"
    "3. Compose a draft system_prompt from the interview answers, show "
    "it back to the user via ask_user, and only call persona.create "
    "after they confirm.\n"
    "4. Offer asset-upload paths (web UI or URL-paste).\n"
    "5. Summarise what was created and link to /admin/persona."
)


_PERSONA_LIST_PRELUDE: str = (
    "[SYSTEM-INSERTED] The user invoked the persona list shortcut. "
    "Call persona.list and render the result as a numbered list "
    "with each entry's id, display_name, and short_summary. Do not "
    "start a configuration wizard; this is a read-only listing."
)


_HELP_PRELUDE: str = (
    "[SYSTEM-INSERTED] The user invoked /help. Respond with a short "
    "intro line followed by a bullet list of every registered slash "
    "command and its summary:\n"
    "- /persona — 启动 persona 配置向导 (aliases: /角色, /人格, "
    "配置人格, 配置角色)\n"
    "- /persona-list — 列出已注册的 persona (aliases: /角色列表, "
    "/人格列表)\n"
    "- /help — 显示可用命令列表 (aliases: /帮助)\n"
    "Keep the response under 12 lines."
)


# ---------------------------------------------------------------------------
# CommandSpec + registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandSpec:
    """Static metadata for a single slash command.

    Frozen so the registry tuple can be safely shared across threads /
    coroutines without defensive copies. Fields:

    * ``name`` — canonical short identifier used in logs / metrics
      (no leading slash; e.g. ``"persona"``).
    * ``aliases`` — every literal string a user can type to invoke the
      command. The first entry is conventionally the primary
      Latin-alphabet form (``"/persona"``); subsequent entries add
      localised aliases (``"/角色"``) and bare-word ergonomic forms
      (``"配置人格"``). Matching is case-sensitive — slash commands are
      ASCII and the i18n aliases are Chinese, so case folding would only
      buy ambiguity.
    * ``summary`` — one-line description surfaced by ``/help``.
    * ``wizard_prelude`` — the SYSTEM-INSERTED text fed to the agent in
      place of the literal command. Encoded as a single multi-line
      string; downstream consumers feed it verbatim through
      :func:`apply_command_prelude` (which exists as a seam so future
      versions can interpolate argument tokens, e.g.
      ``/persona edit grantley`` injecting the slug).
    """

    name: str
    aliases: tuple[str, ...]
    summary: str
    wizard_prelude: str


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
        wizard_prelude=_PERSONA_WIZARD_PRELUDE,
    ),
    CommandSpec(
        name="persona-list",
        aliases=(
            "/persona-list",
            "/角色列表",
            "/人格列表",
        ),
        summary="列出已注册的 persona",
        wizard_prelude=_PERSONA_LIST_PRELUDE,
    ),
    CommandSpec(
        name="help",
        aliases=(
            "/help",
            "/帮助",
        ),
        summary="显示可用命令列表",
        wizard_prelude=_HELP_PRELUDE,
    ),
)


# ---------------------------------------------------------------------------
# Matching + substitution
# ---------------------------------------------------------------------------


def match_command(text: str) -> CommandSpec | None:
    """Return the matching :class:`CommandSpec` or ``None``.

    Matching rule (load-bearing — channel router + chat bootstrap both
    depend on this exact semantics, so any change must update the spec
    in :mod:`corlinman_channels.commands` and the W8.1 contract in
    ``docs/PLAN_PERSONA_STUDIO.md``):

    1. ``text`` is stripped of leading + trailing whitespace before any
       comparison. A pure-whitespace message returns ``None``.
    2. For each spec in :data:`COMMAND_REGISTRY`, for each alias in
       ``spec.aliases``:
         a. If the stripped text equals the alias exactly → match.
         b. If the stripped text starts with ``alias + " "`` (alias
            followed by an ASCII space) → match. This is the
            "command + args" form (``"/persona edit grantley"``); the
            args are visible on the inbox row but are intentionally
            **not** parsed here — the prelude is verbatim and the agent
            reads any args from its own context.
       The first match (registry order, alias order within a spec) wins.
    3. **Substring matches do not trigger.** ``"please run /persona"``
       returns ``None`` so the agent can discuss the command itself
       without the wizard hijacking the turn.

    Registry-order means earlier specs take precedence on the rare event
    of an alias collision; today the alias sets are disjoint so the
    ordering is incidental.
    """
    stripped = text.strip()
    if not stripped:
        return None
    for spec in COMMAND_REGISTRY:
        for alias in spec.aliases:
            if stripped == alias:
                return spec
            # Alias + " " prefix → the "command followed by args" form.
            # We intentionally do not consume the args here; the prelude
            # is verbatim and the agent can read the literal user turn
            # from the inbox row if it ever needs to act on them.
            if stripped.startswith(alias + " "):
                return spec
    return None


def apply_command_prelude(text: str, spec: CommandSpec) -> str:
    """Return the wizard prelude that should replace ``text``.

    Today this is a thin wrapper that returns ``spec.wizard_prelude``
    verbatim — the ``text`` argument is accepted but unused. The seam
    exists so a future revision can interpolate argument tokens (e.g.
    ``/persona edit grantley`` injecting the slug into the prelude)
    without churning every callsite. Callers should always go through
    this helper rather than reading ``spec.wizard_prelude`` directly.
    """
    # ``text`` is intentionally unused for now; we keep it in the
    # signature so the substitution seam is stable.
    del text
    return spec.wizard_prelude
