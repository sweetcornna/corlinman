"""Provider-backed completer for MCP ``sampling/createMessage`` (Dim 5).

v1.26.0 shipped the :class:`~corlinman_mcp_server.sampling.SamplingResponder`
mechanism (mode gate / rate limit / model whitelist) but nothing ever
injected a real :data:`~corlinman_mcp_server.sampling.Completer` — the
``state.extras["mcp_sampling_completer"]`` slot had no writer, so the
responder never advertised the capability and every server-initiated
sampling request short-circuited to ``sampling_unavailable``.

This module closes that gap: :func:`build_sampling_completer` wraps the
gateway's live :class:`~corlinman_providers.registry.ProviderRegistry`
into a completer. Two timing constraints shape the implementation:

* The MCP client manager is constructed **before** the sibling-bootstrap
  loop that populates ``AppState.provider_registry`` (P1), so the
  registry is read **per call**, never captured at build time.
* Provider hot-reload rebuilds ``provider_registry`` in place on
  ``AppState`` — the per-call read also picks up the fresh handle.

A call arriving before the registry exists raises ``RuntimeError``;
``SamplingResponder.handle`` catches completer exceptions and maps them
to a clean ``INTERNAL_ERROR`` result, so this degrades to a per-request
error rather than a crash.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog
from corlinman_mcp_server.sampling import (
    Completer,
    SamplingRequest,
    SamplingResult,
)

from corlinman_server.gateway.services.direct_backend import _alias_entries

logger = structlog.get_logger(__name__)

__all__ = ["build_sampling_completer"]


@dataclass
class _Msg:
    """Concrete :class:`corlinman_providers.base.ChatMessage` shape."""

    role: str
    content: str


# MCP ``stopReason`` values are camelCase enums; the provider protocol's
# normalised ``finish_reason`` is snake/flat. Anything unmapped falls
# back to ``endTurn`` — the spec treats it as the generic success stop.
_STOP_REASONS = {
    "stop": "endTurn",
    "length": "maxTokens",
    "tool_calls": "endTurn",
}


def build_sampling_completer(state: Any) -> Completer:
    """Wrap ``state.provider_registry`` into a sampling ``Completer``.

    The returned coroutine resolves ``request.model`` through the same
    registry + ``[models.aliases]`` path the direct chat backend uses,
    streams the completion, and folds the token deltas into a single
    :class:`SamplingResult`. Reasoning deltas are dropped (they are not
    answer text and must not leak to the requesting MCP server).
    """

    async def _complete(request: SamplingRequest) -> SamplingResult:
        registry = getattr(state, "provider_registry", None)
        if registry is None:
            raise RuntimeError(
                "provider registry not wired yet — sampling arrived "
                "before the providers bootstrap completed"
            )
        config = getattr(state, "config", None)
        models_cfg = (
            config.get("models") if isinstance(config, dict) else None
        ) or {}
        provider, upstream_model, _params = registry.resolve(
            request.model,
            aliases=_alias_entries(models_cfg),
        )

        messages: list[_Msg] = []
        if request.system_prompt:
            messages.append(_Msg(role="system", content=request.system_prompt))
        messages.extend(
            _Msg(role=str(m.get("role") or "user"), content=str(m.get("text") or ""))
            for m in request.messages
        )

        parts: list[str] = []
        finish_reason = "stop"
        async for chunk in provider.chat_stream(
            model=upstream_model,
            messages=messages,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
        ):
            kind = getattr(chunk, "kind", None)
            if kind == "token" and chunk.text and not chunk.is_reasoning:
                parts.append(chunk.text)
            elif kind == "done":
                finish_reason = chunk.finish_reason or "stop"
        # ``stop_sequences`` are accepted on the wire but not forwarded —
        # the provider protocol has no portable stop-sequence slot yet.
        # The whitelist + max_tokens cap (enforced upstream by the
        # responder) are the security-relevant knobs.
        logger.info(
            "gateway.mcp.sampling_completed",
            model=request.model,
            upstream_model=upstream_model,
            chars=sum(len(p) for p in parts),
        )
        return SamplingResult(
            text="".join(parts),
            model=request.model,
            stop_reason=_STOP_REASONS.get(finish_reason, "endTurn"),
        )

    return _complete
