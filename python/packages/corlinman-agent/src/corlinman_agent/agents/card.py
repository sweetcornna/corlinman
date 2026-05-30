"""Agent-card dataclass.

An :class:`AgentCard` is the in-memory representation of a single
``agents/<name>.yaml`` file. The file is the source of truth for the
agent's identity (``name``), its operator-facing summary
(``description``), the prompt fragment the expander will splice into
system-role turns (``system_prompt``), and a small set of per-card
metadata (local variables, allowed tools, referenced skills).

Cards are immutable after load — the registry hands them out but never
mutates them, and the expander only reads from them. Cascade-variable
weaving (project / user / env vars) is *not* this module's job; it is
performed by B2-BE4 downstream. See :mod:`.expander` for the narrow
local-variable substitution we do perform during expansion.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

#: ``"inline"`` is the tier for ad-hoc / temporary cards built in memory by
#: ``subagent_spawn_inline`` — they are never loaded from disk and never
#: enter the registry, so the admin CRUD surface (which keys delete/mutate
#: refusals on ``"built-in"``) never sees them.
AgentSource = Literal["built-in", "user", "project", "inline"]


@dataclass(frozen=True)
class AgentCard:
    """Parsed ``agents/<name>.yaml`` record.

    Attributes
    ----------
    name
        Unique agent identifier (matches the yaml filename stem).
    description
        Short operator-facing summary; surfaced in admin UIs. Not used
        by the expander itself.
    system_prompt
        The prompt fragment the expander substitutes in place of the
        ``{{agent.<name>}}`` placeholder.
    variables
        Per-card local variables. Keys without a namespace are meant to
        be referenced as ``{{var.<key>}}`` inside ``system_prompt``; the
        expander does a narrow pre-substitution so cards can
        self-parameterise without involving the cascade layer.
    tools_allowed
        Whitelist of tool names this agent is permitted to invoke. The
        expander merely records them on the card — enforcement belongs
        to the reasoning loop / approval gate.
    skill_refs
        Names of skill cards this agent wants inlined. The expander
        leaves them as ``{{skill.<name>}}`` tokens in the output so the
        Rust placeholder engine can resolve them during the downstream
        render pass.
    source_path
        Path the card was loaded from; useful for error messages and
        registry hot-reload diffs.
    model
        Optional upstream model id (or alias) this agent binds to. When
        set, it overrides the global default at dispatch time *only* if
        the chat request did not specify a ``model`` of its own. Empty /
        unset preserves pre-W-D1 behaviour (request-body-driven routing).
    provider
        Optional provider slot name (matches a ``[providers.<name>]``
        entry). Threaded through to ``ProviderRegistry.resolve()`` as a
        ``provider_hint`` so the resolver can prefer this specific
        provider when ambiguity exists. ``None`` keeps the legacy
        resolution chain untouched.
    show_action_trace
        Whether the chat UI should expose this agent's reasoning/tool/
        subagent trajectory. Defaults to ``True`` so existing cards keep
        their current operator-visible trace surface.
    source
        Which tier of the stacked-directory loader this card came from.
        ``"built-in"`` is the repo's ``agents/`` dir, ``"user"`` is
        ``$DATA_DIR/agents/``, ``"project"`` is the current working
        directory's ``.corlinman/agents/`` overlay. The admin CRUD
        surface uses this to refuse deletes/mutations against built-ins
        and to flag overlay shadows to operators.
    """

    name: str
    description: str
    system_prompt: str
    variables: dict[str, str] = field(default_factory=dict)
    tools_allowed: list[str] = field(default_factory=list)
    skill_refs: list[str] = field(default_factory=list)
    source_path: Path | None = None
    model: str | None = None
    provider: str | None = None
    show_action_trace: bool = True
    source: AgentSource = "built-in"


_SLUG_STRIP_RE = re.compile(r"[^a-z0-9-]+")


def _safe_slug(name: str | None, *, fallback: str = "inline") -> str:
    """Reduce a freeform ``name`` to a safe agent slug.

    Claude-Code-style: lowercase, keep only ``[a-z0-9-]``, collapse runs of
    separators to a single ``-``, trim leading/trailing ``-``, cap at 50
    chars. Falls back to ``fallback`` when the cleaned result is shorter
    than 3 chars (so an ephemeral agent always has a stable, file-safe
    label even if the model passes junk or omits ``name``).
    """
    raw = (name or "").strip().lower()
    cleaned = _SLUG_STRIP_RE.sub("-", raw).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)[:50].rstrip("-")
    return cleaned if len(cleaned) >= 3 else fallback


def build_ephemeral_card(
    *,
    name: str | None,
    system_prompt: str,
    description: str | None = None,
    model: str | None = None,
) -> AgentCard:
    """Build a one-off, in-memory :class:`AgentCard` for an ad-hoc child.

    Backs ``subagent_spawn_inline`` — the temporary/purpose-built agent the
    main agent creates on the fly (Claude-Code's ad-hoc general-purpose
    pattern). The card is **never registered**: ``source_path=None`` and
    ``source="inline"``.

    ``tools_allowed=["*"]`` triggers the runner's wildcard rule ("inherit
    the parent's full tool set"); the child is still bounded by the
    parent's tools and the caller's ``tool_allowlist`` (escalation is
    rejected), so an inline agent can never exceed the parent's authority.
    """
    return AgentCard(
        name=_safe_slug(name),
        description=(description or "").strip() or "ad-hoc inline agent",
        system_prompt=system_prompt,
        variables={},
        tools_allowed=["*"],
        skill_refs=[],
        source_path=None,
        model=model or None,
        provider=None,
        show_action_trace=True,
        source="inline",
    )


__all__ = ["AgentCard", "AgentSource", "build_ephemeral_card"]
