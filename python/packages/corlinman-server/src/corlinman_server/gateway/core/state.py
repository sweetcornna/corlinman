"""``AppState`` — bundle of shared handles injected via FastAPI's DI.

Python port of ``rust/crates/corlinman-gateway/src/state.rs``. Rust uses
``State<Arc<AppState>>``; FastAPI doesn't have an analogue, so we expose a
single dataclass instance via :func:`get_app_state` (a ``Depends`` factory)
and stash it on ``app.state.corlinman`` at boot.

The dataclass keeps every field optional so test harnesses that only
need a subset (e.g. metrics + log broadcast, but no approval gate) can
construct an empty :class:`AppState` and then assign the handles they
care about. Production boot (see :mod:`.server`) populates everything.

The Rust crate's ``AppState`` carries ``Arc``-wrapped handles into the
plugin registry, session store, gRPC service runtime, plugin supervisor,
approval gate, live config (via ``ArcSwap``), and the config file path.
The Python port keeps the same shape but uses ``typing.Any`` for the
opaque cross-package handles — concrete typing lands when the sibling
packages export their public types and this module gets a `from x import`
without creating a circular dependency. The :func:`get_app_state`
factory still works regardless of which fields the caller populated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:  # pragma: no cover — only for type checkers
    from fastapi import Request

    from corlinman_server.gateway.core.config_watcher import ConfigWatcher
    from corlinman_server.gateway.core.log_broadcast import LogBroadcaster


@dataclass
class AppState:
    """Process-wide shared handles. Replaces axum's ``State<Arc<AppState>>``.

    Every field is optional so test fixtures can build minimal states.
    Production wiring fills them all in via :mod:`.server`.
    """

    # Plugin registry & runtime stack (typed as Any to dodge a heavy
    # cross-package import in the gateway core layer — these resolve at
    # boot to concrete instances from corlinman-providers).
    plugin_registry: Any = None
    session_store: Any = None
    service_runtime: Any = None
    plugin_supervisor: Any = None

    # Approval gate ties to corlinman_providers.plugins.ApprovalStore
    # but the gate wrapper itself lives in
    # :mod:`corlinman_server.gateway.middleware.approval`.
    approval_gate: Any = None

    # Live config snapshot + on-disk path. ``ConfigWatcher`` owns the
    # ArcSwap-equivalent (a single mutable Python attribute behind a
    # lock); the path is the file the watcher reloads from.
    config: Any = None  # currently published Config snapshot
    config_path: Path | None = None
    config_watcher: ConfigWatcher | None = None

    # ---- gap-fill v1.15 wiring spine (CONTRACT C2) ------------------------
    #
    # wire-B (the boot lifespan) SETS these on the AppState; wire-A (the
    # agent servicer + scheduler) READS them. The names are load-bearing —
    # see the C2 contract — so the servicer's ``getattr(app_state, "...")``
    # calls resolve to a live handle once the lifespan has populated them.
    # Each defaults to ``None`` so a degraded boot / partial port still
    # constructs cleanly and the consumer degrades gracefully.

    # corlinman_memory_host.LocalSqliteHost — the conversational + agent-
    # callable memory backend. The agent servicer reads this for the
    # ``memory_search`` / ``session_search`` / ``memory_write`` /
    # ``memory_read`` tool family (WP17 + new-tools lane).
    memory_host: Any = None

    # corlinman_persona.PersonaResolver — read-only ``{{persona.*}}`` /
    # ``{{persona.life_*}}`` placeholder lookup keyed by ``agent_id``.
    persona_resolver: Any = None

    # corlinman_identity.IdentityStore (SqliteIdentityStore) — cross-channel
    # identity graph + verification-phrase redeem path. The admin
    # ``/admin/identity*`` routes resolve it off the AdminState; inbound
    # routing calls ``resolve_or_create`` on it.
    identity_store: Any = None

    # Async callable ``(prompt: str) -> Any`` the scheduler's ``run_agent``
    # action invokes for a real one-turn agent run on a cron schedule.
    agent_runner_fn: Any = None

    # corlinman_hooks.runner.HookRunner — in-process pre/post-tool + Stop
    # lifecycle hooks (C3). The agent servicer's PreToolDispatch + the
    # reasoning loop's turn-end Stop veto read this; ``None`` = no hooks.
    hook_runner: Any = None

    # Tenancy stack — boot wires these from corlinman_server.tenancy.
    admin_db: Any = None
    tenant_pool: Any = None

    # ---- runtime-wiring attach points (Wave 1, see -------------------------
    # docs/contracts/runtime-wiring.md) -------------------------------------
    #
    # Both default to ``None``; a sibling ``bootstrap(state)`` hook fills
    # them at gateway start. Routes/channels gate on presence and return
    # the typed 501/503 envelope when a slot is still ``None`` (degraded
    # mode). Typed as ``Any`` to avoid a heavy cross-package import here.

    # P1 — corlinman_providers.registry.ProviderRegistry, built from
    # ``config["providers"]``. ``/v1/models`` enumerates it.
    provider_registry: Any = None

    # P2 — corlinman_server.gateway.services.ChatService, wrapping a
    # ChatBackend (DirectProviderBackend or GrpcAgentChatBackend).
    # ``/v1/chat/completions`` and the channel adapters drive it.
    chat: Any = None

    # Logging fan-out. Populated by gateway boot once the broadcaster
    # task is spawned; routes/middleware pull a subscription off it.
    log_broadcaster: LogBroadcaster | None = None

    # Free-form bag for sibling-agent extensions (chat backend, eval
    # surface, etc.). Keep this minimal — anything load-bearing gets a
    # first-class field above.
    extras: dict[str, Any] = field(default_factory=dict)

    # ---- convenience factories ----------------------------------------------

    @classmethod
    def empty(cls) -> AppState:
        """Construct a blank state. Mirrors the Rust ``AppState::empty``
        convenience used by tests / stubs that don't need any plugins."""
        return cls()

    def with_log_broadcaster(self, broadcaster: LogBroadcaster) -> AppState:
        """Fluent: attach a log broadcaster. Returns ``self`` so callers
        can chain after construction."""
        self.log_broadcaster = broadcaster
        return self

    def with_config(self, config: Any, path: Path) -> AppState:
        """Fluent: attach a live config + the on-disk path it loaded
        from. Either field is allowed to be ``None`` at the call site,
        but in practice they always travel together."""
        self.config = config
        self.config_path = path
        return self

    def with_approval_gate(self, gate: Any) -> AppState:
        """Fluent: attach an approval gate. Mirrors Rust
        ``AppState::with_approval_gate``."""
        self.approval_gate = gate
        return self

    def with_tenancy(self, admin_db: Any, tenant_pool: Any) -> AppState:
        """Fluent: attach the tenancy stack so admin middleware can
        verify ``Authorization`` / cookie credentials against the per-
        tenant admin tables."""
        self.admin_db = admin_db
        self.tenant_pool = tenant_pool
        return self


def get_app_state(request: Request) -> AppState:
    """FastAPI dependency: pull the ``AppState`` out of ``app.state``.

    Routes declare ``state: AppState = Depends(get_app_state)`` and get
    the bundle the gateway boot stashed at app-build time. Raises
    :class:`RuntimeError` if the state was never attached — that is a
    wiring bug, not a runtime condition the caller should recover from.
    """

    state = getattr(request.app.state, "corlinman", None)
    if state is None:
        raise RuntimeError(
            "AppState missing from app.state.corlinman — "
            "did the gateway boot call build_app() with a state argument?"
        )
    # Stashed on ``app.state`` (an untyped namespace) at boot time.
    return cast("AppState", state)


__all__ = ["AppState", "get_app_state"]
