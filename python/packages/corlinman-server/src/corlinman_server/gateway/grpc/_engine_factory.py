"""Default placeholder engine factory + boot-time state probes.

Extracted verbatim from
:mod:`corlinman_server.gateway.grpc.placeholder`. This module MUST NOT
import the ``placeholder`` source module at module scope (no import
cycle): :class:`PlaceholderEngine` / ``_NullEngine`` live in the source
module and are imported lazily *inside* :func:`build_default_engine`
(the same best-effort, function-local import style already used for the
resolver stubs), so importing this module never pulls in ``placeholder``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from corlinman_server.gateway.grpc._protocols import (
    PlaceholderCtx,
    PlaceholderEngineLike,
    _IdResolverLike,
)

log = logging.getLogger(__name__)


# Metadata keys the render ctx carries the per-agent / per-user id under.
# Mirrors the ``tenant_id`` key the episodes resolver already reads off
# ``ctx.metadata`` — the gateway middleware stamps these the same way.
AGENT_ID_METADATA_KEY: str = "agent_id"
USER_ID_METADATA_KEY: str = "user_id"


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

    __slots__ = ("_id_key", "_inner")

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
    # Lazy import (avoids a placeholder→_engine_factory→placeholder import
    # cycle): the engine + null fallback live in the source module.
    from corlinman_server.gateway.grpc.placeholder import (
        PlaceholderEngine,
        _NullEngine,
    )

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
