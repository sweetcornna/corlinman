"""Domain objects of the unified authorization model (audit W3-1).

Everything here is dependency-light on purpose: the matcher, the gate, the
legacy ``corlinman_agent.permission`` facade and the console all share these
types, so this module must import nothing from any of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

#: The four verdicts. ``log`` is observer-only (equivalent to allow with an
#: audit line); ``ask`` defers to an interactive approval prompt.
ALLOW: str = "allow"
DENY: str = "deny"
LOG: str = "log"
ASK: str = "ask"
_VALID_ACTIONS: tuple[str, ...] = (ALLOW, DENY, LOG, ASK)


class Memory(str, Enum):
    """How long an interactive approval is remembered (C2 vocabulary).

    The single canonical vocabulary — chosen to match the proto / web-UI
    wording (``once`` / ``session`` / ``always``), NOT the old console
    wording where ``always`` meant "this session" and ``persist`` meant
    durable. ``always`` here is durable: it survives the session and the
    process (backed by the :class:`~corlinman_agent.authz.grants.GrantStore`
    SQLite file).
    """

    ONCE = "once"
    SESSION = "session"
    ALWAYS = "always"

    @classmethod
    def coerce(cls, raw: Any) -> Memory:
        """Best-effort parse; unknown / falsy values map to :attr:`ONCE`."""
        if isinstance(raw, Memory):
            return raw
        if isinstance(raw, str):
            text = raw.strip().lower()
            for member in cls:
                if member.value == text:
                    return member
        return cls.ONCE


class PermissionMode(str, Enum):
    """Coarse operating mode layered ABOVE the per-tool rule list.

    Mirrors Claude Code's permission modes. The mode is consulted only when
    the rule list does not produce an explicit ``allow`` / ``deny`` / ``ask``
    for a tool:

    * :attr:`DEFAULT` — fall through to the gate's ``default_action`` /
      strict-mode fallback (legacy behaviour).
    * :attr:`ACCEPT_EDITS` — auto-allow file-edit tools (write/edit/patch)
      without prompting; everything else falls through to default.
    * :attr:`PLAN` — deny every mutating tool (planning only, no side
      effects); read-only tools still fall through to default.
    * :attr:`BYPASS` — allow everything (no gating). Operator opt-in for
      trusted automation.

    Stored as lowercase strings to match a ``mode = "..."`` config knob.
    """

    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    PLAN = "plan"
    BYPASS = "bypass"

    @classmethod
    def coerce(cls, raw: Any) -> PermissionMode:
        """Parse a mode value; unknown values map to :attr:`DEFAULT` **loudly**.

        W3-1: the old silent degradation meant a typo like ``"planning"``
        quietly re-enabled every mutating tool. Unknown non-empty values now
        log an ERROR (never raise — permissions are config-driven and must
        not crash agent boot) while still returning :attr:`DEFAULT` so the
        agent keeps running.
        """
        if isinstance(raw, PermissionMode):
            return raw
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return cls.DEFAULT
            for member in cls:
                if member.value.lower() == text.lower():
                    return member
            logger.error(
                "agent.authz.invalid_permission_mode",
                value=text,
                detail=(
                    "unknown permission mode; keeping 'default' — check "
                    "[permissions].mode / CORLINMAN_AGENT_PERMISSION_MODE"
                ),
            )
            return cls.DEFAULT
        if raw is not None and raw != "" and raw is not False:
            logger.error(
                "agent.authz.invalid_permission_mode",
                value=repr(raw),
                detail="permission mode must be a string; keeping 'default'",
            )
        return cls.DEFAULT


@dataclass(frozen=True)
class Subject:
    """The caller identity a rule's ``scope`` block matches against.

    Extends the legacy ``PermissionContext`` (model / session_key / user_id)
    with the two new dimensions of the unified model:

    * ``tenant_id`` — the authenticated tenant (``ChatStart.tenant_id`` /
      the ``<tenant>::`` session-key prefix).
    * ``surface`` — where the call physically came from. A CLOSED set:
      ``console`` / ``web`` / ``qq`` / ``telegram`` / ``discord`` / ``slack``
      / ``qq_official`` / ``wechat_official`` / ``feishu`` / ``voice`` /
      ``scheduler`` / ``subagent``.
    * ``parent_surface`` — kept when a subagent call inherits its parent's
      surface (W3-2 wires it; declared now so the shape is stable).

    Field order deliberately keeps the legacy triple FIRST so existing
    positional constructions keep meaning what they meant. All fields are
    optional; the matcher treats a missing value as "never matches a
    non-empty pattern".
    """

    model: str | None = None
    session_key: str | None = None
    user_id: str | None = None
    tenant_id: str | None = None
    surface: str | None = None
    parent_surface: str | None = None


__all__ = [
    "ALLOW",
    "ASK",
    "DENY",
    "LOG",
    "Memory",
    "PermissionMode",
    "Subject",
]
