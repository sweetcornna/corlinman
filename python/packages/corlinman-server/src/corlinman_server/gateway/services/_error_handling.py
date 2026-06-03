"""Error-reason → proto mapping and terminal error-frame helpers.

Extracted verbatim from
:mod:`corlinman_server.gateway.services.direct_backend` (god-file split).
This module MUST NOT import the source module (no cycle).
"""

from __future__ import annotations

from corlinman_grpc._generated.corlinman.v1 import (
    agent_pb2,
    common_pb2,
)

# ─── Provider-id → OpenAI failover reason ────────────────────────────
#
# ``corlinman_providers.failover.CorlinmanError`` subclasses carry a
# stable lowercase ``reason`` (see ``failover.py``). ``ServerFrame.error``
# wants a ``common_pb2.FailoverReason`` enum value, so we map the
# adapter-side string back onto the proto enum. Anything we don't
# recognise falls through to ``UNKNOWN`` — a future error class can't
# crash the frame translation.
_REASON_TO_PROTO: dict[str, common_pb2.FailoverReason] = {
    "billing": common_pb2.BILLING,
    "rate_limit": common_pb2.RATE_LIMIT,
    "auth": common_pb2.AUTH,
    "auth_permanent": common_pb2.AUTH_PERMANENT,
    "timeout": common_pb2.TIMEOUT,
    "model_not_found": common_pb2.MODEL_NOT_FOUND,
    "format": common_pb2.FORMAT,
    "context_overflow": common_pb2.CONTEXT_OVERFLOW,
    "overloaded": common_pb2.OVERLOADED,
    "unknown": common_pb2.UNKNOWN,
    "unspecified": common_pb2.FAILOVER_REASON_UNSPECIFIED,
}


def _reason_to_proto(reason: str | None) -> common_pb2.FailoverReason:
    """Map a ``CorlinmanError.reason`` string onto the proto enum.

    Unknown / missing reasons collapse to ``UNKNOWN`` so an exception
    that isn't a typed :class:`CorlinmanError` still produces a valid
    terminal ``error`` frame.
    """
    if not reason:
        return common_pb2.UNKNOWN
    return _REASON_TO_PROTO.get(reason, common_pb2.UNKNOWN)


def _error_reason_of(exc: BaseException) -> str | None:
    """Best-effort extract of a ``reason`` discriminant off an exception.

    :class:`corlinman_providers.failover.CorlinmanError` subclasses
    expose a ``reason`` attribute; anything else returns ``None`` and
    the caller defaults to ``UNKNOWN``.
    """
    reason = getattr(exc, "reason", None)
    if isinstance(reason, str) and reason:
        return reason
    return None


def _error_frame(exc: BaseException) -> agent_pb2.ServerFrame:
    """Build a terminal ``ServerFrame.error`` from an exception."""
    return agent_pb2.ServerFrame(
        error=common_pb2.ErrorInfo(
            reason=_reason_to_proto(_error_reason_of(exc)),
            message=str(exc) or exc.__class__.__name__,
        ),
    )
