"""Placeholder render-context + structural protocol types.

Extracted verbatim from
:mod:`corlinman_server.gateway.grpc.placeholder`. This module MUST NOT
import the ``placeholder`` source module (no import cycle).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class PlaceholderCtx:
    """Render-time context handed to every resolver.

    Mirrors :rust:`corlinman_core::placeholder::PlaceholderCtx`. The
    actual Python ``PlaceholderEngine`` will own a richer version of
    this type; we keep a minimal shim here so the bridge can be
    constructed and tested without a hard dep on the (unported) engine.
    """

    __slots__ = ("metadata", "model_name", "session_key")

    def __init__(
        self,
        session_key: str,
        *,
        model_name: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        self.session_key = session_key
        self.model_name = model_name
        self.metadata: dict[str, str] = dict(metadata or {})


@runtime_checkable
class PlaceholderEngineLike(Protocol):
    """Structural surface the bridge needs.

    Mirrors the slice of the Rust ``PlaceholderEngine`` API used by the
    gRPC wrapper. Concrete impls will land alongside the
    ``PlaceholderEngine`` Python port; tests can wire in a fake.
    """

    async def render(self, template: str, ctx: PlaceholderCtx) -> str: ...

    def clone_with_max_depth(self, max_depth: int) -> PlaceholderEngineLike: ...


@runtime_checkable
class DynamicResolverLike(Protocol):
    """Structural surface a namespace resolver must satisfy.

    Mirrors the Rust ``DynamicResolver`` trait. The ``key`` is the token
    body *after* the namespace prefix (e.g. ``{{weather.beijing}}`` with
    a resolver on ``weather`` gets ``key = "beijing"``). The ``ctx`` is
    accepted positionally for parity with the engine; resolvers that
    don't consult it (memory) still accept it. Duck-typed: any object
    with an ``async resolve(self, key, ctx) -> str`` is accepted.
    """

    async def resolve(self, key: str, ctx: PlaceholderCtx) -> str: ...


@runtime_checkable
class _IdResolverLike(Protocol):
    """A resolver whose ``resolve`` takes ``(key, id: str)`` rather than
    ``(key, ctx)`` — the shape of ``corlinman_persona.PersonaResolver``,
    ``corlinman_user_model.UserModelResolver`` and
    ``corlinman_goals.GoalsResolver``."""

    async def resolve(self, key: str, id_: str) -> str: ...
