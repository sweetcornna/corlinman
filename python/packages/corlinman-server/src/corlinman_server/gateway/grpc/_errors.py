"""Placeholder error hierarchy.

Extracted verbatim from
:mod:`corlinman_server.gateway.grpc.placeholder`. This module MUST NOT
import the ``placeholder`` source module (no import cycle).
"""

from __future__ import annotations


class PlaceholderError(Exception):
    """Base class for the three documented placeholder error shapes."""


class CycleError(PlaceholderError):
    """Cycle detected at ``key``."""

    def __init__(self, key: str) -> None:
        super().__init__(f"placeholder cycle detected at key '{key}'")
        self.key = key


class DepthExceededError(PlaceholderError):
    """Recursion depth limit reached."""

    def __init__(self, depth: int) -> None:
        super().__init__(f"placeholder recursion depth {depth} exceeded")
        self.depth = depth


class ResolverError(PlaceholderError):
    """Resolver raised for ``namespace``."""

    def __init__(self, namespace: str, message: str) -> None:
        super().__init__(f"resolver for '{namespace}' failed: {message}")
        self.namespace = namespace
        self.message = message
