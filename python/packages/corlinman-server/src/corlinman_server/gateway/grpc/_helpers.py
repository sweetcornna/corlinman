"""Pure placeholder helpers — token regex + wire-form encoding/scanning.

Extracted verbatim from
:mod:`corlinman_server.gateway.grpc.placeholder`. This module MUST NOT
import the ``placeholder`` source module (no import cycle).
"""

from __future__ import annotations

import re

from corlinman_server.gateway.grpc._errors import (
    CycleError,
    DepthExceededError,
    ResolverError,
)

# Mirrors the Rust ``TOKEN_RE`` lazy regex — same shape so the post-render
# unresolved-key scan finds the same tokens the engine would have tried
# to expand.
_TOKEN_RE: re.Pattern[str] = re.compile(r"\{\{([^{}]*?)\}\}")


def encode_error(err: Exception) -> str:
    """Encode a Python placeholder error back into the stable wire form.

    Mirrors :rust:`encode_error` byte-for-byte:

    * :class:`CycleError`        → ``"cycle:<k>"``
    * :class:`DepthExceededError`→ ``"depth_exceeded"``
    * :class:`ResolverError`     → ``"resolver:<msg>"``
    * unknown / generic          → ``"resolver:<str(err)>"``
    """
    if isinstance(err, CycleError):
        return f"cycle:{err.key}"
    if isinstance(err, DepthExceededError):
        return "depth_exceeded"
    if isinstance(err, ResolverError):
        return f"resolver:{err.message}"
    # Tolerate "wrapped" errors coming up through a future
    # ``CorlinmanError::Parse`` lookalike: match the prefixes the Rust
    # encoder strips before classifying.
    raw = str(err)
    inner = raw.removeprefix("parse error (placeholder): ")

    if inner.startswith("placeholder cycle detected at key '") and inner.endswith("'"):
        key = inner[len("placeholder cycle detected at key '") : -1]
        return f"cycle:{key}"
    if inner.startswith("placeholder recursion depth "):
        return "depth_exceeded"
    if inner.startswith("resolver for '"):
        # "resolver for '<ns>' failed: <inner>"
        rest = inner[len("resolver for '") :]
        marker = "' failed: "
        if marker in rest:
            _, tail = rest.split(marker, 1)
            return f"resolver:{tail}"

    return f"resolver:{inner}"


def collect_unresolved(rendered: str) -> list[str]:
    """Harvest still-literal ``{{…}}`` tokens from a rendered template.

    The engine preserves unknown tokens verbatim, so a post-render scan
    is the cheapest way to surface them without modifying the engine.
    Mirrors :rust:`collect_unresolved` 1:1, including the
    empty-body skip (``{{}}`` / ``{{ }}`` are intentionally preserved
    so callers can use them as literal markup).
    """
    if "{{" not in rendered:
        return []
    out: list[str] = []
    for match in _TOKEN_RE.finditer(rendered):
        body = match.group(1).strip()
        if not body:
            continue
        if body not in out:
            out.append(body)
    return out
