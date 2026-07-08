"""Tool-call approval gate — mediates between model intent and execution.

Responsibility: for each tool call the reasoning loop / servicer wants to
dispatch, turn a permission verdict into a final allow/deny, escalating an
``ask`` verdict to an interactive **prompt-and-wait** when a resolver is
wired. This unifies the previously-disjoint surfaces:

* the declarative per-tool :class:`~corlinman_agent.permission.PermissionGate`
  (allow / deny / log / **ask**), and
* the human-in-the-loop approval prompt (formerly only a gateway-side
  ``ApprovalGate`` middleware).

Design (gap ``permissions-no-ask-action`` + classifier unification):

* :class:`ApprovalVerdict` — the terminal outcome (``allow`` / ``deny``)
  plus the pre-resolution ``ask`` marker the gate may surface.
* :class:`ApprovalGate` — wraps a :class:`PermissionGate` and an optional
  async ``resolver`` callback. ``decide(...)`` runs the permission gate,
  then for an ``ask`` verdict calls the resolver (prompt-and-wait). With no
  resolver wired the gate is **fail-closed** for ``ask`` (treated as deny)
  so an unattended deployment never blocks a turn forever waiting on a
  human who will never answer — operators opt into prompts by wiring a
  resolver.

The gate is deliberately dependency-free (stdlib + the sibling
``permission`` module) so it imports cleanly in the agent package and is
unit-testable without the gRPC / channel stack.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

import structlog

from corlinman_agent.permission import (
    ALLOW,
    ASK,
    DENY,
    LOG,
    PermissionContext,
    PermissionGate,
)

logger = structlog.get_logger(__name__)


class ApprovalVerdict(str, Enum):
    """Terminal outcome of :meth:`ApprovalGate.decide`.

    ``ASK`` is only ever returned by the lower-level permission gate; the
    approval gate always resolves it to ``ALLOW`` / ``DENY`` before
    returning (via the resolver, or fail-closed to ``DENY``).
    """

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


#: A prompt-and-wait resolver. Given the tool name, the parsed args, and the
#: caller context, returns ``True`` to approve or ``False`` to deny. Async so
#: the channel round-trip (Telegram inline keyboard / QQ list / web modal)
#: can await an operator decision without blocking the event loop.
ApprovalResolver = Callable[
    [str, "dict[str, Any]", PermissionContext], Awaitable[bool]
]


@dataclass(frozen=True)
class ApprovalOutcome:
    """Structured result of :meth:`ApprovalGate.decide`.

    Attributes
    ----------
    verdict
        The terminal :class:`ApprovalVerdict` (always ``ALLOW`` or
        ``DENY`` — never ``ASK``).
    asked
        ``True`` when the underlying permission verdict was ``ask`` and the
        gate escalated to the resolver (or fail-closed). Lets the caller
        emit the right audit / metric label.
    reason
        Human-readable explanation, populated on a deny.
    rule_index
        Index of the matched permission rule, or ``None`` for a default /
        mode fallback.
    """

    verdict: ApprovalVerdict
    asked: bool = False
    reason: str | None = None
    rule_index: int | None = None

    @property
    def allowed(self) -> bool:
        return self.verdict is ApprovalVerdict.ALLOW


class ApprovalGate:
    """Unified verdict gate: permission rules + interactive approval.

    Parameters
    ----------
    permission_gate
        The declarative :class:`PermissionGate`. When ``None`` a stock
        allow-all gate is built from the environment.
    resolver
        Optional async prompt-and-wait callback for the ``ask`` verdict.
        ``None`` → ``ask`` is fail-closed (treated as deny).
    ask_timeout_s
        Max seconds to wait on the resolver before treating the prompt as
        unanswered (fail-closed → deny). ``None`` waits indefinitely.
    """

    __slots__ = ("_ask_timeout_s", "_permission_gate", "_resolver")

    def __init__(
        self,
        permission_gate: PermissionGate | None = None,
        *,
        resolver: ApprovalResolver | None = None,
        ask_timeout_s: float | None = 300.0,
    ) -> None:
        if permission_gate is None:
            # E1: stock gates come from the layered settings loader
            # (settings.json + settings.local.json + env) instead of
            # env-only. With no settings file present this is byte-
            # identical to the old ``from_env()``.
            from corlinman_agent.permission_settings import (  # noqa: PLC0415 — cycle guard
                build_permission_gate,
            )

            permission_gate = build_permission_gate()
        self._permission_gate = permission_gate
        self._resolver = resolver
        self._ask_timeout_s = ask_timeout_s

    @property
    def permission_gate(self) -> PermissionGate:
        return self._permission_gate

    @property
    def has_resolver(self) -> bool:
        return self._resolver is not None

    async def decide(
        self,
        tool: str,
        *,
        args: dict[str, Any] | None = None,
        model: str | None = None,
        session_key: str | None = None,
        user_id: str | None = None,
    ) -> ApprovalOutcome:
        """Resolve a tool call to a terminal allow/deny outcome.

        Runs the args-aware permission gate first. ``allow`` / ``log`` pass
        (``log`` is observer-only and treated as allow here), ``deny``
        short-circuits, and ``ask`` escalates to the resolver (or
        fail-closed deny when none is wired).
        """
        ctx = PermissionContext(
            model=model, session_key=session_key, user_id=user_id
        )
        action, rule_index = self._permission_gate.resolve_with_args(
            tool, ctx, args
        )

        if action == DENY:
            return ApprovalOutcome(
                verdict=ApprovalVerdict.DENY,
                reason=(
                    f"tool {tool!r} denied by permission rules"
                ),
                rule_index=rule_index,
            )
        if action in (ALLOW, LOG):
            return ApprovalOutcome(
                verdict=ApprovalVerdict.ALLOW, rule_index=rule_index
            )

        # action == ASK — escalate.
        if action == ASK:
            if self._resolver is None:
                logger.info(
                    "agent.approval.ask_no_resolver",
                    tool=tool,
                    session_key=session_key,
                )
                return ApprovalOutcome(
                    verdict=ApprovalVerdict.DENY,
                    asked=True,
                    reason=(
                        f"tool {tool!r} needs approval but no approval "
                        "resolver is wired (fail-closed)"
                    ),
                    rule_index=rule_index,
                )
            approved = await self._run_resolver(tool, args or {}, ctx)
            return ApprovalOutcome(
                verdict=ApprovalVerdict.ALLOW
                if approved
                else ApprovalVerdict.DENY,
                asked=True,
                reason=None if approved else f"tool {tool!r} denied by operator",
                rule_index=rule_index,
            )

        # Unknown action — conservative deny so a config typo can't silently
        # open the gate.
        return ApprovalOutcome(
            verdict=ApprovalVerdict.DENY,
            reason=f"unknown permission action {action!r}",
            rule_index=rule_index,
        )

    async def _run_resolver(
        self, tool: str, args: dict[str, Any], ctx: PermissionContext
    ) -> bool:
        """Run the prompt-and-wait resolver with timeout + error guards.

        A resolver that raises, times out, or returns a non-bool is
        fail-closed to deny so a broken prompt path never silently
        approves a sensitive call.
        """
        assert self._resolver is not None  # guarded by caller
        try:
            coro = self._resolver(tool, args, ctx)
            if self._ask_timeout_s is not None:
                result = await asyncio.wait_for(coro, timeout=self._ask_timeout_s)
            else:
                result = await coro
        except TimeoutError:
            logger.info("agent.approval.ask_timeout", tool=tool)
            return False
        except Exception as exc:  # noqa: BLE001 — fail-closed on resolver error
            logger.warning("agent.approval.resolver_error", tool=tool, error=str(exc))
            return False
        return bool(result)


__all__ = [
    "ApprovalGate",
    "ApprovalOutcome",
    "ApprovalResolver",
    "ApprovalVerdict",
]
