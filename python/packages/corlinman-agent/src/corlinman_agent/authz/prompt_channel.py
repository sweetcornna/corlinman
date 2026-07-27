"""Prompt-channel abstraction — the transport layer of an ``ask`` verdict.

W3-3 of the unified-authorization plan: one protocol, three production
implementations.

* console — :class:`ResolverPromptChannel` wrapping the interactive
  ``ConsoleApprovalResolver`` (the legacy ``(tool, args, ctx) -> bool``
  resolver surface stays the console's native shape).
* web chat / channels — the gRPC stream bridge in the agent servicer
  (``_StreamPromptChannel``): emits an ``AwaitingApproval`` server frame
  and parks on the matching ``ApprovalDecision`` client frame.
* nothing wired — :class:`NullPromptChannel`: fail-closed deny with a
  diagnosable ``authz_no_channel`` marker (decision C1: an ``ask`` rule
  with no channel is a deny, never a silent allow).

The types here are deliberately dependency-light (stdlib + sibling
``model``) so the console, the servicer and tests all share one wire
vocabulary without import cycles.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import structlog

from corlinman_agent.authz.model import Memory, Subject

logger = structlog.get_logger(__name__)

#: Messaging surfaces whose senders are arbitrary end users. An ``always``
#: grant approved from one of these defaults to being scoped to
#: ``(surface, user)`` — the opposite of the console/web default, where the
#: approver is the operator themself (design plan §1.2.4 / §3.5).
CHANNEL_SCOPED_SURFACES: frozenset[str] = frozenset(
    {
        "qq",
        "telegram",
        "discord",
        "slack",
        "qq_official",
        "wechat_official",
        "feishu",
    }
)


def grant_scope_flags(subject: Subject | Any) -> tuple[bool, bool]:
    """``(scope_surface, scope_user)`` defaults for a grant from ``subject``.

    Channel senders get grants pinned to their surface + user id; trusted
    operator surfaces (console / web / voice / scheduler) grant globally.
    A subagent inherits its parent surface's posture.
    """
    surface = getattr(subject, "surface", None) or ""
    if surface == "subagent":
        surface = getattr(subject, "parent_surface", None) or ""
    scoped = surface in CHANNEL_SCOPED_SURFACES
    return scoped, scoped


@dataclass(frozen=True)
class AuthzRequest:
    """One pending interactive approval, as shown to a human."""

    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    subject: Subject | Any = None
    call_id: str = ""
    plugin: str = ""
    reason: str = ""


@dataclass(frozen=True)
class AuthzAnswer:
    """The human's decision (or the channel's terminal failure mode)."""

    approved: bool
    memory: Memory = Memory.ONCE
    deny_message: str | None = None
    #: The prompt was never answered in time — fail-closed deny, but the
    #: caller can surface a timeout-specific message to the user.
    timed_out: bool = False
    #: No channel could carry the prompt at all (``NullPromptChannel``) —
    #: the caller should mark the deny ``authz_no_channel`` so operators
    #: can tell "denied by a human" from "nobody could be asked".
    no_channel: bool = False


@runtime_checkable
class PromptChannel(Protocol):
    """Transport that can put an :class:`AuthzRequest` in front of a human."""

    async def request(self, req: AuthzRequest) -> AuthzAnswer: ...


class NullPromptChannel:
    """Fail-closed terminal channel — every request is denied.

    Used for surfaces that structurally cannot host a prompt (``--print``
    mode, attach mode, channel callers that did not declare
    ``approval_capable``).
    """

    async def request(self, req: AuthzRequest) -> AuthzAnswer:
        logger.warning(
            "agent.authz.no_channel",
            tool=req.tool,
            call_id=req.call_id or None,
        )
        return AuthzAnswer(
            approved=False,
            deny_message=(
                f"authz_no_channel: tool {req.tool!r} needs interactive "
                "approval but this surface has no prompt channel (fail-closed)"
            ),
            no_channel=True,
        )


class ResolverPromptChannel:
    """Adapter: a legacy ``(tool, args, ctx) -> bool`` resolver as a channel.

    The console's :class:`ConsoleApprovalResolver` records its own grants,
    so the answer's ``memory`` is always reported as :attr:`Memory.ONCE`
    here — double-recording would widen the grant key surface.
    """

    def __init__(self, resolver: Any) -> None:
        self._resolver = resolver

    async def request(self, req: AuthzRequest) -> AuthzAnswer:
        try:
            approved = bool(await self._resolver(req.tool, req.args, req.subject))
        except Exception as exc:  # noqa: BLE001 — fail-closed on resolver error
            logger.warning(
                "agent.authz.prompt_resolver_error",
                tool=req.tool,
                error=str(exc),
            )
            return AuthzAnswer(approved=False, deny_message=str(exc))
        return AuthzAnswer(
            approved=approved,
            deny_message=None if approved else f"tool {req.tool!r} denied by operator",
        )


__all__ = [
    "CHANNEL_SCOPED_SURFACES",
    "AuthzAnswer",
    "AuthzRequest",
    "NullPromptChannel",
    "PromptChannel",
    "ResolverPromptChannel",
    "grant_scope_flags",
]
