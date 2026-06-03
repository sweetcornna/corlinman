"""Sampling-knob and extra-param extraction from proto / alias config.

Extracted verbatim from
:mod:`corlinman_server.gateway.services.direct_backend` (god-file split).
This module MUST NOT import the source module (no cycle).
"""

from __future__ import annotations

import contextlib
from typing import Any

from corlinman_grpc._generated.corlinman.v1 import (
    agent_pb2,
)


def _sampling_from_proto(
    start: agent_pb2.ChatStart,
    params: dict[str, Any],
) -> tuple[float | None, int | None]:
    """Pick the sampling knobs for the provider call.

    The :class:`ChatService` stamps ``temperature`` / ``max_tokens`` onto
    :class:`agent_pb2.ChatStart` from the :class:`InternalChatRequest`,
    but proto scalars have no "unset" — ``temperature`` defaults to
    ``0.0`` and ``max_tokens`` to ``0``. We treat ``0`` as "not set" and
    fall back to any provider/alias-level ``params`` default, so an
    operator's ``[models.aliases.*.params]`` block still applies.
    """
    temperature: float | None = None
    if start.temperature:
        temperature = float(start.temperature)
    elif "temperature" in params:
        with contextlib.suppress(TypeError, ValueError):
            temperature = float(params["temperature"])

    max_tokens: int | None = None
    if start.max_tokens:
        max_tokens = int(start.max_tokens)
    elif params.get("max_tokens"):
        with contextlib.suppress(TypeError, ValueError):
            max_tokens = int(params["max_tokens"])

    return temperature, max_tokens


def _extra_params(params: dict[str, Any]) -> dict[str, Any] | None:
    """Forward non-sampling provider params as the adapter ``extra`` map.

    ``temperature`` / ``max_tokens`` are passed as first-class kwargs, so
    they're stripped here to avoid a double-set. Everything else (e.g.
    ``top_p``, ``reasoning_effort``) flows through ``extra`` — the
    adapters merge it straight into the vendor request kwargs.
    """
    if not params:
        return None
    extra = {
        k: v
        for k, v in params.items()
        if k not in ("temperature", "max_tokens")
    }
    return extra or None
