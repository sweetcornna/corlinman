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
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

import grpc
from corlinman_grpc._generated.corlinman.v1 import (
    placeholder_pb2,
    placeholder_pb2_grpc,
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


# Mirrors the Rust ``TOKEN_RE`` lazy regex — same shape so the post-render
# unresolved-key scan finds the same tokens the engine would have tried
# to expand.
_TOKEN_RE: re.Pattern[str] = re.compile(r"\{\{([^{}]*?)\}\}")


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


# ─── Engine protocol (PlaceholderEngine port stub) ───────────────────


class PlaceholderCtx:
    """Render-time context handed to every resolver.

    Mirrors :rust:`corlinman_core::placeholder::PlaceholderCtx`. The
    actual Python ``PlaceholderEngine`` will own a richer version of
    this type; we keep a minimal shim here so the bridge can be
    constructed and tested without a hard dep on the (unported) engine.
    """

    __slots__ = ("session_key", "model_name", "metadata")

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


# ─── ctx→id resolver adapter ──────────────────────────────────────────


# Metadata keys the render ctx carries the per-agent / per-user id under.
# Mirrors the ``tenant_id`` key the episodes resolver already reads off
# ``ctx.metadata`` — the gateway middleware stamps these the same way.
AGENT_ID_METADATA_KEY: str = "agent_id"
USER_ID_METADATA_KEY: str = "user_id"


@runtime_checkable
class _IdResolverLike(Protocol):
    """A resolver whose ``resolve`` takes ``(key, id: str)`` rather than
    ``(key, ctx)`` — the shape of ``corlinman_persona.PersonaResolver``,
    ``corlinman_user_model.UserModelResolver`` and
    ``corlinman_goals.GoalsResolver``."""

    async def resolve(self, key: str, id_: str) -> str: ...


class _CtxIdResolverAdapter:
    """Adapt a ``resolve(key, id: str)`` resolver to the engine's
    :class:`DynamicResolverLike` ``resolve(key, ctx)`` surface.

    The persona / user_model / goals resolvers were written for a caller
    that already knows the agent / user id (the future context
    assembler). The engine, however, only hands a resolver the
    :class:`PlaceholderCtx`; the per-agent / per-user id lives in
    ``ctx.metadata`` under :data:`AGENT_ID_METADATA_KEY` /
    :data:`USER_ID_METADATA_KEY` (exactly how episodes reads
    ``ctx.metadata["tenant_id"]``).

    This adapter pulls the id from the ctx metadata and forwards the call,
    so no resolver has to learn about ``PlaceholderCtx`` and this file
    owns the single mapping point. A missing id resolves with the empty
    string, which every wrapped resolver treats as a "no data" lookup
    (returning ``""``) rather than raising — so the token is consumed,
    not leaked verbatim.
    """

    __slots__ = ("_inner", "_id_key")

    def __init__(self, inner: _IdResolverLike, *, id_key: str) -> None:
        self._inner = inner
        self._id_key = id_key

    async def resolve(self, key: str, ctx: PlaceholderCtx) -> str:
        metadata = getattr(ctx, "metadata", None) or {}
        id_ = metadata.get(self._id_key, "") if isinstance(metadata, dict) else ""
        return await self._inner.resolve(key, id_)


def _resolve_state_attr(state: Any, *attrs: str) -> Any | None:
    """Probe ``state`` (then its ``extras`` bag) for the first populated
    attribute in ``attrs``. Mirrors :func:`_resolve_memory_host`'s
    forward-compatible probe so a sibling boot path can attach a resolver
    under any of the documented names without touching this file."""
    for attr in attrs:
        value = getattr(state, attr, None)
        if value is not None:
            return value
    extras = getattr(state, "extras", None)
    if isinstance(extras, dict):
        for attr in attrs:
            value = extras.get(attr)
            if value is not None:
                return value
    return None


# ─── Default engine factory (boot ↔ test shared path) ─────────────────


def _resolve_state_data_dir(state: Any) -> Path | None:
    """Pull the per-tenant data root off the gateway ``AppState``.

    The real ``AppState`` (and the degraded fallback) carry ``data_dir``;
    the env-driven layout also stamps it. We probe the documented
    attribute names in order so this stays robust across the AppState
    shape — ``data_dir`` is the canonical one (stamped in
    ``entrypoint._build_state``).
    """
    for attr in ("data_dir", "resolved_data_dir"):
        value = getattr(state, attr, None)
        if value:
            return Path(value)
    # Free-form extras bag fallback.
    extras = getattr(state, "extras", None)
    if isinstance(extras, dict):
        value = extras.get("data_dir") or extras.get("resolved_data_dir")
        if value:
            return Path(value)
    return None


def build_default_engine(state: Any) -> PlaceholderEngineLike:
    """Construct the production :class:`PlaceholderEngine` for the gateway,
    registering the resolvers reachable from ``state``.

    This is the single shared path the server boot
    (:func:`serve_placeholder_in_background`) and the acceptance tests both
    use, so the test exercises exactly what production runs.

    Resolver construction is **best-effort**: a failure to build any one
    resolver is logged and skipped rather than crashing boot. If nothing
    can be wired (e.g. no data dir), we fall back to a :class:`_NullEngine`
    so the gRPC seam still serves (every token round-trips through
    ``unresolved_keys``), exactly the previous behaviour.

    Currently wired:

    * ``episodes`` — :class:`EpisodesResolver` rooted at the per-tenant
      data dir. Always registered when a data dir is resolvable.
    * ``memory``   — :class:`MemoryResolver` requires a
      :class:`corlinman_memory_host.MemoryHost`. No host is published on
      the gateway ``AppState`` today and constructing one is non-trivial
      (per-tenant async ``LocalSqliteHost.open`` + the agent-brain
      namespace layout), so it is skipped with a clear warning. See the
      module docstring / R4-F2 report for what finishing it needs.
    * ``persona`` / ``user`` / ``goals`` — registered through
      :class:`_CtxIdResolverAdapter` *only when* a store-backed resolver
      is published on ``state`` (``persona_resolver`` /
      ``user_model_resolver`` / ``goals_resolver``). Those resolvers wrap
      an already-opened async store; this synchronous builder can't open
      one and none is published on the gateway ``AppState`` today, so the
      namespaces are deferred-with-warning until a boot path attaches the
      resolver. The adapter (which maps the engine ctx → the per-agent /
      per-user id each resolver needs) is wired and exercised by the G8
      acceptance tests with stub stores on ``state``.
    """
    data_dir = _resolve_state_data_dir(state)
    if data_dir is None:
        log.warning(
            "gateway.grpc.placeholder.no_data_dir "
            "(falling back to echo-only NullEngine)"
        )
        return _NullEngine()

    engine = PlaceholderEngine()
    registered: list[str] = []

    # --- EPISODES (trivial: needs only the per-tenant data dir) ---------
    try:
        from corlinman_server.gateway.placeholder.episodes_stub import (
            EpisodesResolver,
        )

        engine.register_namespace("episodes", EpisodesResolver(data_dir))
        registered.append("episodes")
    except Exception as exc:  # best-effort: must not crash boot
        log.warning(
            "gateway.grpc.placeholder.episodes_resolver_unavailable error=%s",
            exc,
        )

    # --- MEMORY (best-effort: needs a MemoryHost we don't have on state) -
    memory_host = _resolve_memory_host(state)
    if memory_host is not None:
        try:
            from corlinman_server.gateway.placeholder.memory_stub import (
                MemoryResolver,
            )

            engine.register_namespace("memory", MemoryResolver(memory_host))
            registered.append("memory")
        except Exception as exc:  # best-effort: must not crash boot
            log.warning(
                "gateway.grpc.placeholder.memory_resolver_unavailable error=%s",
                exc,
            )
    else:
        log.warning(
            "gateway.grpc.placeholder.memory_resolver_unavailable "
            "reason=no_memory_host_on_state "
            "(MemoryResolver needs a corlinman_memory_host.MemoryHost; "
            "none is published on AppState — episodes registered only)"
        )

    # --- PERSONA / USER / GOALS (need a pre-opened store on state) ------
    # Unlike episodes (which builds itself from just ``data_dir`` and opens
    # connections lazily inside ``resolve``), these three resolvers wrap an
    # already-opened async store (``await PersonaStore.open_or_create`` etc.).
    # ``build_default_engine`` is synchronous, so it can neither open them
    # here nor reach them: no persona/user/goals store is published on the
    # gateway ``AppState`` this builder receives (``persona_store`` lives on
    # a *different* ``AdminState``, and no user/goals store is constructed
    # anywhere reachable). Wiring them from production therefore needs new
    # async plumbing through entrypoint/app-state — NOT a contained wire-up.
    #
    # So we register them only when a resolver IS published on state
    # (probed the same forward-compatible way as ``memory_host``). The
    # adapter maps the engine ctx → the agent/user id each resolver wants,
    # so the moment a boot path attaches a store-backed resolver under any
    # of the documented attribute names, the namespace lights up without
    # touching this file again. See followups for the entrypoint plumbing.
    for namespace, attrs, id_key in (
        ("persona", ("persona_resolver",), AGENT_ID_METADATA_KEY),
        ("user", ("user_model_resolver", "user_resolver"), USER_ID_METADATA_KEY),
        ("goals", ("goals_resolver",), AGENT_ID_METADATA_KEY),
    ):
        inner = _resolve_state_attr(state, *attrs)
        if inner is None:
            log.warning(
                "gateway.grpc.placeholder.%s_resolver_unavailable "
                "reason=no_resolver_on_state "
                "(needs a store-backed resolver published on AppState; "
                "none is — namespace deferred)",
                namespace,
            )
            continue
        try:
            engine.register_namespace(
                namespace, _CtxIdResolverAdapter(inner, id_key=id_key)
            )
            registered.append(namespace)
        except Exception as exc:  # best-effort: must not crash boot
            log.warning(
                "gateway.grpc.placeholder.%s_resolver_unavailable error=%s",
                namespace,
                exc,
            )

    if not registered:
        # Nothing wired — keep the previous echo-only behaviour rather
        # than serving a useless empty real engine.
        log.warning(
            "gateway.grpc.placeholder.no_resolvers "
            "(falling back to echo-only NullEngine)"
        )
        return _NullEngine()

    log.info(
        "gateway.grpc.placeholder.engine_built namespaces=%s data_dir=%s",
        registered,
        data_dir,
    )
    return engine


def _resolve_memory_host(state: Any) -> Any | None:
    """Probe the gateway ``AppState`` for an already-constructed
    :class:`MemoryHost`.

    No such handle is published on the main gateway ``AppState`` today
    (only ``routes_admin_b`` carries a ``memory_host`` on a *different*
    state object). We probe the documented attribute names so that if a
    sibling boot path later attaches one, ``{{memory.*}}`` lights up
    automatically without touching this file again.
    """
    for attr in ("memory_host",):
        value = getattr(state, attr, None)
        if value is not None:
            return value
    extras = getattr(state, "extras", None)
    if isinstance(extras, dict):
        value = extras.get("memory_host")
        if value is not None:
            return value
    return None


# ─── Helpers (pure) ───────────────────────────────────────────────────


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
