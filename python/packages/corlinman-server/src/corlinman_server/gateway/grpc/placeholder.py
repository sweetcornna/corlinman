"""gRPC wrapper over a Python ``PlaceholderEngine``.

Port of :rust:`corlinman_gateway::grpc::placeholder`. Direction: a
client (the Rust subsystem, the admin shell, the future Python
``context_assembler``) dials this service on the UDS path in
``$CORLINMAN_UDS_PATH`` (default ``/tmp/corlinman.sock``) and calls
``Render`` for every template it wants expanded before a provider call.

The :class:`PlaceholderEngine` Python sibling has not landed yet (it's
the W3 port of ``corlinman-core::placeholder``); we accept a structural
:class:`PlaceholderEngineLike` protocol so this module is testable today
and the eventual concrete engine drops in without touching this file.

Tokens with a namespace that has no resolver round-trip back unchanged
and are surfaced in ``RenderResponse.unresolved_keys`` for observability
— same contract as the Rust ``collect_unresolved`` post-render scan.

Error mapping preserves the enum shape of the Rust ``PlaceholderError``
so a single client library can dial either implementation:

==========================  =========================
engine error                ``error`` string
==========================  =========================
``CycleError(k)``           ``"cycle:<k>"``
``DepthExceededError(...)`` ``"depth_exceeded"``
``ResolverError(ns, msg)``  ``"resolver:<msg>"``
==========================  =========================
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import grpc
from corlinman_grpc._generated.corlinman.v1 import (
    placeholder_pb2,
    placeholder_pb2_grpc,
)

# Re-export the extracted siblings so external importers (the package
# ``__init__``, ``test_placeholder_engine``) keep resolving every name off
# this module without being edited.
from corlinman_server.gateway.grpc._engine_factory import build_default_engine
from corlinman_server.gateway.grpc._errors import (
    CycleError,
    DepthExceededError,
    PlaceholderError,
    ResolverError,
)
from corlinman_server.gateway.grpc._helpers import (
    _TOKEN_RE,
    collect_unresolved,
    encode_error,
)
from corlinman_server.gateway.grpc._protocols import (
    DynamicResolverLike,
    PlaceholderCtx,
    PlaceholderEngineLike,
)

__all__ = [
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_NAMESPACE",
    "DEFAULT_RUST_SOCKET",
    "ENV_RUST_SOCKET",
    "RESERVED_NAMESPACES",
    "CycleError",
    "DepthExceededError",
    "DynamicResolverLike",
    "PlaceholderCtx",
    "PlaceholderEngine",
    "PlaceholderEngineLike",
    "PlaceholderError",
    "PlaceholderService",
    "ResolverError",
    "build_default_engine",
    "collect_unresolved",
    "encode_error",
    "serve",
]


log = logging.getLogger(__name__)


# ─── Constants ────────────────────────────────────────────────────────

DEFAULT_RUST_SOCKET: str = "/tmp/corlinman.sock"
"""Default UDS path the gateway binds for Python→gateway traffic.

Kept separate from ``/tmp/corlinman-py.sock`` (the agent socket) so the
two sides can be restarted independently without stepping on each
other's socket file. Mirrors the Rust ``DEFAULT_RUST_SOCKET`` constant.
"""

ENV_RUST_SOCKET: str = "CORLINMAN_UDS_PATH"
"""Env var the Python ``PlaceholderClient`` honours, and the server
respects when set."""


# Mirrors :rust:`corlinman_core::placeholder` module constants 1:1.

DEFAULT_NAMESPACE: str = "default"
"""Namespace assumed when a token has no ``.`` separator (``{{today}}``
is looked up as ``default.today``). Matches the Rust
``DEFAULT_NAMESPACE``."""

DEFAULT_MAX_DEPTH: int = 4
"""Default maximum recursive expansion depth. Matches the Rust
``DEFAULT_MAX_DEPTH = 4``."""

RESERVED_NAMESPACES: tuple[str, ...] = (
    "var",
    "sar",
    "tar",
    "agent",
    "session",
    "tool",
    "vector",
    "skill",
    "episodes",
)
"""Namespace prefixes reserved by the corlinman runtime. Verbatim port of
the Rust ``RESERVED_NAMESPACES`` slice (informational, not exclusive)."""


# ─── Engine ───────────────────────────────────────────────────────────


class PlaceholderEngine:
    """Faithful Python port of :rust:`corlinman_core::placeholder::\
PlaceholderEngine`.

    Static values are resolved first (O(1) dict lookup, keyed by the full
    ``namespace.name``); if absent, the token's namespace is matched
    against a dynamic resolver registry. Values produced by resolvers (or
    static entries) are themselves re-scanned for ``{{…}}`` tokens up to
    :attr:`max_depth`; an in-flight key set guards against cycles.

    The class satisfies :class:`PlaceholderEngineLike` so
    :class:`PlaceholderService` accepts it directly.
    """

    __slots__ = ("_dynamic", "_max_depth", "_values")

    def __init__(
        self,
        *,
        values: dict[str, str] | None = None,
        dynamic: dict[str, DynamicResolverLike] | None = None,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> None:
        self._values: dict[str, str] = dict(values or {})
        self._dynamic: dict[str, DynamicResolverLike] = dict(dynamic or {})
        self._max_depth = int(max_depth)

    # ---- introspection ---------------------------------------------------

    @property
    def max_depth(self) -> int:
        """Current recursion ceiling. Mirrors the Rust ``max_depth``."""
        return self._max_depth

    @property
    def namespaces(self) -> tuple[str, ...]:
        """Registered dynamic namespaces (debug / boot logging)."""
        return tuple(self._dynamic.keys())

    @staticmethod
    def is_reserved_namespace(prefix: str) -> bool:
        """Whether ``prefix`` is one of the reserved runtime namespaces.
        Mirrors :rust:`PlaceholderEngine::is_reserved_namespace`."""
        return prefix in RESERVED_NAMESPACES

    def __repr__(self) -> str:  # pragma: no cover — debug only
        return (
            f"PlaceholderEngine(values={len(self._values)}, "
            f"dynamic_namespaces={list(self._dynamic)}, "
            f"max_depth={self._max_depth})"
        )

    # ---- builders --------------------------------------------------------

    def with_max_depth(self, max_depth: int) -> PlaceholderEngine:
        """Builder: override the recursion ceiling *in place* and return
        ``self``. Depth 0 disables recursive expansion (single pass).
        Mirrors the Rust ``with_max_depth`` consuming builder."""
        self._max_depth = int(max_depth)
        return self

    def with_static(self, key: str, value: str) -> PlaceholderEngine:
        """Register a static ``namespace.name`` entry (or bare ``name``).
        Builder-style; returns ``self``. Mirrors Rust ``with_static``."""
        self._values[key] = str(value)
        return self

    def with_dynamic(
        self, namespace: str, resolver: DynamicResolverLike
    ) -> PlaceholderEngine:
        """Builder-style sibling of :meth:`register_namespace`. Mirrors
        the Rust ``with_dynamic``."""
        self._dynamic[namespace] = resolver
        return self

    def register_namespace(
        self, prefix: str, resolver: DynamicResolverLike
    ) -> DynamicResolverLike | None:
        """Register (or replace) a dynamic resolver for ``prefix``.

        Returns the previous resolver if one was registered. Mirrors the
        Rust ``register_namespace`` ``HashMap::insert`` return.
        """
        previous = self._dynamic.get(prefix)
        self._dynamic[prefix] = resolver
        return previous

    def clone_with_max_depth(self, max_depth: int) -> PlaceholderEngine:
        """Clone this engine's registrations with a different recursion
        ceiling. Shares the same resolver instances + static values
        (shallow copy of the registries) so callers don't rebuild the
        registry. Mirrors the Rust ``clone_with_max_depth``."""
        return PlaceholderEngine(
            values=self._values,
            dynamic=self._dynamic,
            max_depth=max_depth,
        )

    # ---- render ----------------------------------------------------------

    async def render(self, template: str, ctx: PlaceholderCtx) -> str:
        """Render ``template``, replacing each ``{{namespace.name}}``
        token. Resolved values are re-scanned for placeholders up to
        :attr:`max_depth`; cycles raise :class:`CycleError`. Unknown
        tokens are returned verbatim. Mirrors the Rust ``render``."""
        in_flight: set[str] = set()
        return await self._render_inner(template, ctx, in_flight, 0)

    async def _render_inner(
        self,
        template: str,
        ctx: PlaceholderCtx,
        in_flight: set[str],
        depth: int,
    ) -> str:
        """Internal recursive render. Keeps the ``in_flight`` / ``depth``
        invariants inside the class. Mirrors the Rust ``render_inner``."""
        if depth > self._max_depth:
            raise DepthExceededError(depth)

        # Fast path: no ``{{`` at all → skip the regex + allocation. Same
        # short-circuit as the Rust impl.
        if "{{" not in template:
            return template

        out: list[str] = []
        cursor = 0
        # Collect matches up-front so we can ``await`` inside the loop
        # without holding the iterator across an await point (mirrors the
        # Rust ``find_iter().collect()`` dance).
        for match in list(_TOKEN_RE.finditer(template)):
            out.append(template[cursor : match.start()])
            raw = match.group(0)
            body = match.group(1).strip()

            if not body:
                # Empty ``{{}}`` / ``{{ }}`` preserved verbatim.
                out.append(raw)
                cursor = match.end()
                continue

            value = await self._resolve_once(body, ctx)
            if value is None:
                # Unknown token → preserve verbatim.
                out.append(raw)
            elif "{{" in value and self._max_depth > 0:
                # Recurse only when the resolved value still contains a
                # token AND recursion is enabled. Cycle guard keyed on the
                # token body, exactly like the Rust in-flight ``HashSet``.
                if body in in_flight:
                    raise CycleError(body)
                in_flight.add(body)
                try:
                    expanded = await self._render_inner(
                        value, ctx, in_flight, depth + 1
                    )
                finally:
                    in_flight.discard(body)
                out.append(expanded)
            else:
                out.append(value)
            cursor = match.end()

        out.append(template[cursor:])
        return "".join(out)

    async def _resolve_once(
        self, body: str, ctx: PlaceholderCtx
    ) -> str | None:
        """Resolve a single trimmed token body (one hop, no recursion).
        Returns ``None`` for unknown tokens so the caller preserves the
        original text. Mirrors the Rust ``resolve_once`` order:
        static → split on first ``.`` → synthesised ``default.<name>`` →
        dynamic resolver → ``None``."""
        # Phase 1: flat static lookup (legacy full-key form).
        static = self._values.get(body)
        if static is not None:
            return static

        # Split into (namespace, key) on the first ``.`` only; a bare
        # token becomes (default, body).
        ns_split = body.split(".", 1)
        if len(ns_split) == 2:
            namespace, key = ns_split[0], ns_split[1]
        else:
            namespace, key = DEFAULT_NAMESPACE, body
            # Phase 1b: synthesised ``default.<name>`` form.
            synth = f"{DEFAULT_NAMESPACE}.{body}"
            synth_value = self._values.get(synth)
            if synth_value is not None:
                return synth_value

        # Phase 2: dynamic namespace resolver.
        resolver = self._dynamic.get(namespace)
        if resolver is not None:
            try:
                return await resolver.resolve(key, ctx)
            except PlaceholderError:
                # Already the documented error shape — re-raise untouched.
                raise
            except Exception as exc:  # wrap any other raise into ResolverError
                raise ResolverError(namespace, str(exc)) from exc

        # Unknown → preserve verbatim.
        return None


# ─── Service ──────────────────────────────────────────────────────────


class PlaceholderService(placeholder_pb2_grpc.PlaceholderServicer):
    """gRPC service shell.

    Wraps a shared :class:`PlaceholderEngineLike` so multiple concurrent
    ``Render`` RPCs share the same resolver registry. The engine is
    accepted as ``Optional`` so callers can stand up a no-resolver
    service for tests / boot-time bridges where every token round-trips
    back through ``unresolved_keys``.
    """

    def __init__(self, engine: PlaceholderEngineLike | None) -> None:
        self._engine = engine

    @classmethod
    def with_empty_engine(cls) -> PlaceholderService:
        """Convenience for tests + the equivalent of the Rust
        ``PlaceholderService::with_empty_engine``.

        Returns a service whose engine echoes every template back
        verbatim (i.e. no resolvers registered). Every ``{{ns.name}}``
        token is surfaced via ``unresolved_keys``.
        """
        return cls(_NullEngine())

    async def Render(  # noqa: N802 — gRPC casing
        self,
        request: placeholder_pb2.RenderRequest,
        context: grpc.aio.ServicerContext,
    ) -> placeholder_pb2.RenderResponse:
        # Re-hydrate the engine context. The proto message allows an
        # empty ``model_name`` to mean "none"; the Python ctx encodes
        # that as ``None`` so round-trip the sentinel.
        ctx_msg = request.ctx
        ctx = PlaceholderCtx(
            session_key=ctx_msg.session_key if ctx_msg is not None else "",
            model_name=(ctx_msg.model_name or None) if ctx_msg is not None else None,
            metadata=dict(ctx_msg.metadata) if ctx_msg is not None else None,
        )

        # Honour per-call ``max_depth`` override. 0 = use engine default
        # (matches the proto docstring + the Rust branch).
        engine = self._engine
        if engine is None:
            return placeholder_pb2.RenderResponse(
                rendered="",
                unresolved_keys=[],
                error="resolver:engine not configured",
            )
        if request.max_depth != 0:
            engine = engine.clone_with_max_depth(int(request.max_depth))

        try:
            rendered = await engine.render(request.template, ctx)
        except PlaceholderError as err:
            return placeholder_pb2.RenderResponse(
                rendered="",
                unresolved_keys=[],
                error=encode_error(err),
            )
        except Exception as err:  # noqa: BLE001 — surface as resolver error
            # Unknown shapes — surface verbatim so the client can still
            # log something actionable. Mirrors the Rust ``encode_error``
            # fallback branch.
            return placeholder_pb2.RenderResponse(
                rendered="",
                unresolved_keys=[],
                error=f"resolver:{err}",
            )

        unresolved = collect_unresolved(rendered)
        return placeholder_pb2.RenderResponse(
            rendered=rendered,
            unresolved_keys=unresolved,
            error="",
        )


class _NullEngine:
    """Engine sibling of :rust:`PlaceholderEngine::new()` with zero
    resolvers — every template echoes back verbatim so the post-render
    scan surfaces every token as unresolved."""

    async def render(self, template: str, ctx: PlaceholderCtx) -> str:
        return template

    def clone_with_max_depth(self, max_depth: int) -> _NullEngine:
        return self


# ─── Server helper ────────────────────────────────────────────────────


async def serve(
    socket_path: str | os.PathLike[str],
    service: PlaceholderService,
    shutdown: asyncio.Event | Awaitable[None],
) -> None:
    """Bind a ``grpc.aio`` server onto ``socket_path`` and serve the
    ``Placeholder`` service until ``shutdown`` fires.

    Removes the socket file on exit so subsequent boots can rebind
    cleanly. Mirrors :rust:`serve` — the call is non-fatal in spirit:
    callers wrap it in a task and log-and-continue if binding fails
    (e.g. permission denied on a read-only fs).

    ``shutdown`` may be either an :class:`asyncio.Event` (set when ready
    to stop) or any awaitable that resolves when the server should
    shut down. Mirrors the Rust ``F: Future<Output = ()>`` bound.
    """
    path = Path(os.fspath(socket_path))

    # Best-effort cleanup of a stale socket — a previous crash may have
    # left the file behind. Matches the Rust cleanup-before-bind dance.
    with contextlib.suppress(FileNotFoundError, OSError):
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)

    server = grpc.aio.server()
    placeholder_pb2_grpc.add_PlaceholderServicer_to_server(service, server)
    # gRPC supports ``unix:`` URIs for UDS listeners.
    server.add_insecure_port(f"unix:{path}")
    await server.start()
    log.info("placeholder gRPC bound socket=%s", path)

    try:
        if isinstance(shutdown, asyncio.Event):
            await shutdown.wait()
        else:
            await shutdown
    finally:
        # Mirror the Rust ``serve_with_incoming_shutdown`` cleanup: try
        # a graceful stop first, then unlink the socket file.
        await server.stop(grace=1.0)
        with contextlib.suppress(FileNotFoundError, OSError):
            path.unlink()


# Re-export for typing convenience (matches Rust ``pub use`` pattern).
_unused_typing: tuple[Any, ...] = (
    Callable,
)  # keep imports flake-clean across linters that strip unused.
