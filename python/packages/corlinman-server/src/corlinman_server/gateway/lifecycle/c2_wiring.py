"""Module-level CONTRACT C2 wiring helpers for the gateway entrypoint.

Extracted verbatim from
:mod:`corlinman_server.gateway.lifecycle.entrypoint` (Phase 5). These are
the module-level wiring functions the boot lifespan / app factory call:

* :func:`_wire_plugin_hotload` — build the live ``PluginRegistry`` + wire
  true plugin hot-load.
* :func:`_build_agent_runner_fn` — build the scheduler ``run_agent``
  one-turn agent runner closure.
* :func:`_wire_c2_handles` — construct + publish the CONTRACT C2 handles
  (memory_host / persona_resolver / identity_store / agent_runner_fn /
  hook_runner) onto the AppState.

This module never imports ``entrypoint`` (no import cycle): ``entrypoint``
re-imports these three names back so ``build_app`` / ``_lifespan`` keep
calling them, and so external importers (``tests/test_gf_c2_wiring.py``)
keep resolving ``_wire_c2_handles`` off the ``entrypoint`` namespace.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

import structlog

from corlinman_server.gateway.lifecycle.config_resolve import (
    _extract_section,
)

logger = structlog.get_logger(__name__)


async def _wire_plugin_hotload(
    state: Any,
    admin_b_state: Any,
    plugin_store: Any,
    data_dir: Path,
) -> None:
    """Build the live :class:`PluginRegistry` + wire true plugin hot-load.

    Constructs the registry from the env-configured roots, syncs in the
    *enabled* marketplace plugins under ``<data_dir>/plugins`` (so their
    tools are callable), and publishes:

    * ``state.plugin_registry`` — read by ``build_tool_executor`` at the
      sibling-bootstrap that runs right after this, so the agent tool
      plane binds the registry.
    * ``admin_b_state.plugins`` — so ``/admin/plugins`` reflects it.
    * ``admin_b_state.extras["plugin_registry_reload"]`` — the callable the
      ``/admin/plugins/market/{slug}/{enable,disable}`` routes fire to
      re-sync the live registry to the persisted enabled set with no
      restart.

    Best-effort: any failure (missing providers package, slotted degraded
    state) logs and leaves the plugin plane simply unwired.
    """
    try:
        from corlinman_providers.plugins import PluginRegistry, roots_from_env_var
        from corlinman_providers.plugins.discovery import Origin

        from corlinman_server.system.marketplace.plugin_runtime import (
            make_reload_hook,
            sync_registry,
        )
    except Exception as exc:  # pragma: no cover — providers package absent
        logger.warning("gateway.marketplace.plugin_runtime_missing", error=str(exc))
        return

    plugins_dir = data_dir / "plugins"

    def _enabled_slugs() -> set[str]:
        try:
            return {row.slug for row in plugin_store.list() if row.enabled}
        except Exception:  # pragma: no cover — store hiccup → nothing enabled
            return set()

    try:
        registry = PluginRegistry.from_roots(
            list(roots_from_env_var("CORLINMAN_PLUGIN_DIRS", Origin.CONFIG))
        )
        await sync_registry(registry, plugins_dir, _enabled_slugs())
        state.plugin_registry = registry
        admin_b_state.plugins = registry
        admin_b_state.extras["plugin_registry_reload"] = make_reload_hook(
            registry, plugins_dir, _enabled_slugs
        )
        logger.info(
            "gateway.marketplace.plugin_registry_wired",
            plugins=len(registry),
        )
    except Exception as exc:  # pragma: no cover — best-effort
        logger.warning(
            "gateway.marketplace.plugin_registry_failed", error=str(exc)
        )


def _build_agent_runner_fn(state: Any) -> Any:
    """Build the ``agent_runner_fn`` the scheduler's ``run_agent`` action
    invokes for a real one-turn agent run on a cron schedule.

    The returned coroutine accepts a ``prompt`` string, drives one turn
    through the live :class:`ChatService` parked on ``state.chat``, and
    returns a small result dict (``{"ok": ..., "reply"/"error": ...}``).
    It resolves the chat service lazily *per firing* so a scheduler job
    that fires before the chat bootstrap completed (or after a hot-reload
    swapped the service) always sees the current handle. When no chat
    service is wired the runner returns ``{"ok": False,
    "error": "chat_service_unavailable"}`` and the scheduler surfaces the
    firing as a failure on the hook bus rather than crashing.

    Channel delivery: when the job metadata carries a home-channel hint
    the result is logged on the structlog feed (the same best-effort
    delivery seam the restart-broadcast uses); a future outbound-handle
    wave can route through it without re-touching this closure.
    """

    async def _runner(prompt: str) -> dict[str, Any]:
        chat_service = getattr(state, "chat", None)
        if chat_service is None:
            extras = getattr(state, "extras", None)
            if isinstance(extras, dict):
                chat_service = extras.get("chat")
        if chat_service is None:
            return {"ok": False, "error": "chat_service_unavailable"}
        try:
            from corlinman_server.gateway_api.types import (
                InternalChatRequest,
                Message,
                Role,
            )
        except Exception as exc:  # noqa: BLE001 — degrade cleanly
            return {"ok": False, "error": "gateway_api_unavailable",
                    "message": str(exc)}

        model = ""
        cfg = getattr(state, "config", None)
        if isinstance(cfg, dict):
            models_cfg = cfg.get("models")
            if isinstance(models_cfg, dict):
                m = models_cfg.get("default")
                if isinstance(m, str) and m:
                    model = m

        request = InternalChatRequest(
            model=model,
            messages=[Message(role=Role.USER, content=prompt)],
            session_key=f"scheduler:run_agent:{uuid.uuid4().hex[:8]}",
            stream=False,
            max_tokens=None,
            temperature=None,
            attachments=[],
            binding=None,
        )
        cancel = asyncio.Event()
        reply_parts: list[str] = []
        try:
            stream = chat_service.run(request, cancel)
            async for event in stream:
                kind = getattr(event, "kind", None)
                if kind == "token_delta":
                    delta = getattr(event, "delta", None)
                    if isinstance(delta, str):
                        reply_parts.append(delta)
                elif kind == "error":
                    inner = getattr(event, "error", None)
                    return {
                        "ok": False,
                        "error": "chat_error",
                        "reason": str(getattr(inner, "reason", "unknown")),
                    }
                elif kind == "done":
                    break
        except Exception as exc:  # noqa: BLE001 — surface, never raise
            return {"ok": False, "error": "chat_service_failed",
                    "message": str(exc)}
        return {"ok": True, "reply": "".join(reply_parts)}

    return _runner


async def _seed_builtin_persona_state(state_store: Any) -> None:
    """Insert a default-shaped persona-STATE row for the built-in grantley
    persona when absent (gap persona-life-resolver-dead boot-seed).

    The :class:`~corlinman_persona.PersonaResolver` reads
    ``{{persona.mood/fatigue/recent_topics/life_*}}`` off the
    ``agent_state.sqlite`` row keyed ``(tenant_id="default",
    agent_id="grantley")``. No other lifecycle creates that row, so without
    this seed the resolver returns ``""`` for every placeholder until the
    agent's ``persona_life_*`` tools happen to write one.

    Idempotent — mirrors :func:`corlinman_persona.seeder.seed_from_card`'s
    insert-if-absent semantics: an existing row is **never** overwritten
    (mutations there belong to the EvolutionLoop / persona tools). We don't
    reuse ``seed_from_card`` because it requires an on-disk agent-card YAML
    path; the built-in grantley body ships as markdown, not a card, so we
    upsert a defaults-only :class:`PersonaState` directly.
    """
    from corlinman_persona.state import PersonaState

    from corlinman_server.persona.default_grantley import DEFAULT_GRANTLEY_ID

    existing = await state_store.get(DEFAULT_GRANTLEY_ID)
    if existing is not None:
        return
    await state_store.upsert(
        PersonaState(
            agent_id=DEFAULT_GRANTLEY_ID,
            mood="neutral",
            fatigue=0.0,
            recent_topics=[],
            # ``upsert`` stamps updated_at with "now" because we pass 0.
            updated_at_ms=0,
            state_json={},
        )
    )
    logger.info(
        "gateway.c2.persona_state_seeded", agent_id=DEFAULT_GRANTLEY_ID
    )


async def _wire_c2_handles(
    app: Any, state: Any, admin_a_state: Any, data_dir: Path, cfg: Any | None
) -> None:
    """Construct + publish the CONTRACT C2 handles onto ``state``.

    Sets (each best-effort, ``None`` on failure so the consumer degrades):

    * ``state.memory_host``     — corlinman_memory_host.LocalSqliteHost.
    * ``state.persona_resolver``— corlinman_persona.PersonaResolver over
      the runtime persona-STATE store (``agent_state.sqlite``); also stashed
      on ``extras`` + admin_a for the qzone builtin / future producers.
    * ``state.identity_store``  — corlinman_identity.SqliteIdentityStore;
      also stamped onto ``admin_a_state`` so the ``/admin/identity*`` routes
      un-503.
    * ``state.agent_runner_fn`` — async ``(prompt) -> dict`` for the
      scheduler ``run_agent`` action.
    * ``state.hook_runner``     — corlinman_hooks.runner.HookRunner with
      file-discovery (``CORLINMAN_HOOKS_DIR``).
    """
    # --- memory_host -----------------------------------------------------
    try:
        from corlinman_memory_host import LocalSqliteHost

        if getattr(state, "memory_host", None) is None:
            mem_path = data_dir / "memory.sqlite"
            state.memory_host = await LocalSqliteHost.open(
                "local", str(mem_path)
            )
            logger.info("gateway.c2.memory_host_wired", path=str(mem_path))
    except Exception as exc:  # noqa: BLE001 — memory-free chat degrades fine
        logger.warning("gateway.c2.memory_host_failed", error=str(exc))
        with suppress(AttributeError, TypeError):
            state.memory_host = None

    # --- memory_kernel (W1 — shadow mode) ---------------------------------
    # mk_* tables co-habit memory.sqlite with the legacy host above; the
    # kernel is a second WAL connection to the same file. Gated at the
    # call sites by CORLINMAN_MEMORY_KERNEL, so wiring it unconditionally
    # here is safe (off-mode servicers simply never touch it).
    try:
        from corlinman_memory_kernel import MemoryKernel

        if getattr(state, "memory_kernel", None) is None:
            state.memory_kernel = await MemoryKernel.open(
                data_dir / "memory.sqlite"
            )
            logger.info("gateway.c2.memory_kernel_wired")
    except Exception as exc:  # noqa: BLE001 — kernel-free chat degrades fine
        logger.warning("gateway.c2.memory_kernel_failed", error=str(exc))
        with suppress(AttributeError, TypeError):
            state.memory_kernel = None

    # W2: the /admin/identity/merge route re-homes the merged user's
    # memory through these handles (best-effort — merge works without).
    if admin_a_state is not None:
        with suppress(AttributeError, TypeError):
            admin_a_state.memory_host = getattr(state, "memory_host", None)
            admin_a_state.memory_kernel = getattr(state, "memory_kernel", None)

    # --- memory embed seam (W6) -------------------------------------------
    # A live-state closure: reads state.provider_registry and
    # state.config["embedding"] PER CALL, so config-mutation hot swaps
    # (which rebuild the registry) are picked up without rewiring.
    # Returns None when no embedding provider is configured/enabled —
    # consumers (reconcile affect/vector stamping) then simply skip.
    async def _memory_embed(text: str) -> list[float] | None:
        registry = getattr(state, "provider_registry", None)
        config = getattr(state, "config", None)
        emb = config.get("embedding") if isinstance(config, dict) else None
        if registry is None or not isinstance(emb, dict):
            return None
        if not emb.get("enabled", True):
            return None
        provider_name = emb.get("provider")
        model = emb.get("model")
        if not provider_name or not model:
            return None
        provider = registry.get(str(provider_name))
        if provider is None:
            return None
        vectors = await provider.embed(model=str(model), inputs=[text])
        return list(vectors[0]) if vectors else None

    async def _memory_embed_many(texts: list[str]) -> list[list[float]] | None:
        registry = getattr(state, "provider_registry", None)
        config = getattr(state, "config", None)
        emb = config.get("embedding") if isinstance(config, dict) else None
        if registry is None or not isinstance(emb, dict):
            return None
        if not emb.get("enabled", True):
            return None
        provider_name = emb.get("provider")
        model = emb.get("model")
        if not provider_name or not model:
            return None
        provider = registry.get(str(provider_name))
        if provider is None:
            return None
        vectors = await provider.embed(model=str(model), inputs=list(texts))
        return [list(v) for v in vectors] if vectors else None

    with suppress(AttributeError, TypeError):
        state.memory_embed_fn = _memory_embed
        state.memory_embed_many_fn = _memory_embed_many

    # --- memory recall config ---------------------------------------------
    # ``[memory.recall]`` TOML knobs for the servicer's conversational
    # recall (recent-turn count, notes top_k, query char cap). Published as
    # a plain dict; the servicer sanitises values and falls back to legacy
    # defaults for anything missing, so an absent/partial section is fine.
    try:
        recall_cfg: dict[str, Any] = {}
        kernel_cfg: dict[str, Any] = {}
        scope_cfg: dict[str, Any] = {}
        curator_cfg: dict[str, Any] = {}
        affect_cfg: dict[str, Any] = {}
        memory_section = _extract_section(cfg, "memory")
        if isinstance(memory_section, dict):
            recall_section = memory_section.get("recall")
            if isinstance(recall_section, dict):
                recall_cfg = dict(recall_section)
            kernel_section = memory_section.get("kernel")
            if isinstance(kernel_section, dict):
                kernel_cfg = dict(kernel_section)
            scope_section = memory_section.get("scope")
            if isinstance(scope_section, dict):
                scope_cfg = dict(scope_section)
            curator_section = memory_section.get("curator")
            if isinstance(curator_section, dict):
                curator_cfg = dict(curator_section)
            affect_section = memory_section.get("affect")
            if isinstance(affect_section, dict):
                affect_cfg = dict(affect_section)
        state.memory_recall_config = recall_cfg
        state.memory_kernel_config = kernel_cfg
        state.memory_scope_config = scope_cfg
        state.memory_curator_config = curator_cfg
        state.memory_affect_config = affect_cfg
        if recall_cfg:
            logger.info("gateway.c2.memory_recall_config_wired", **recall_cfg)
        if kernel_cfg:
            logger.info("gateway.c2.memory_kernel_config_wired", **kernel_cfg)
        if scope_cfg:
            logger.info("gateway.c2.memory_scope_config_wired", **scope_cfg)
    except Exception as exc:  # noqa: BLE001 — defaults apply
        logger.warning("gateway.c2.memory_recall_config_failed", error=str(exc))
        with suppress(AttributeError, TypeError):
            state.memory_recall_config = {}
            state.memory_kernel_config = {}
            state.memory_scope_config = {}
            state.memory_curator_config = {}
            state.memory_affect_config = {}

    # --- persona_resolver (gap persona-life-resolver-dead) ---------------
    # The resolver reads ``{{persona.mood}}`` / ``{{persona.life_*}}`` off
    # the SAME runtime persona-STATE DB (``agent_state.sqlite``) the agent
    # ``persona_life_*`` tools write to, keyed by ``agent_id``. Publishing
    # it on AppState gives the prompt-render path a live read surface.
    try:
        from corlinman_persona import PersonaResolver
        from corlinman_persona.store import PersonaStore as _StateStore

        state_store = await _StateStore.open_or_create(
            data_dir / "agent_state.sqlite"
        )
        await _seed_builtin_persona_state(state_store)
        resolver = PersonaResolver(state_store)
        state.persona_resolver = resolver
        # Stash the open store handle so the lifespan-exit can close it and
        # the qzone builtin can reach the same DB without re-opening.
        app.state.corlinman_persona_state_store = state_store
        extras = getattr(state, "extras", None)
        if isinstance(extras, dict):
            extras["persona_resolver"] = resolver
            extras["persona_state_store"] = state_store
        if admin_a_state is not None:
            with suppress(AttributeError, TypeError):
                admin_a_state.persona_resolver = resolver
        logger.info("gateway.c2.persona_resolver_wired")
    except Exception as exc:  # noqa: BLE001 — placeholders fall back to ""
        logger.warning("gateway.c2.persona_resolver_failed", error=str(exc))
        with suppress(AttributeError, TypeError):
            state.persona_resolver = None

    # --- identity_store (gap identity-unwired-and-no-auth-gate) ----------
    try:
        from corlinman_identity import (
            SqliteIdentityStore,
            identity_db_path,
            legacy_default,
        )

        id_path = identity_db_path(data_dir, legacy_default())
        id_store = await SqliteIdentityStore.open(id_path)
        state.identity_store = id_store
        app.state.corlinman_identity_store = id_store
        # Un-503 the /admin/identity* routes by stamping the store onto the
        # AdminState the routes resolve via get_admin_state().
        if admin_a_state is not None:
            with suppress(AttributeError, TypeError):
                admin_a_state.identity_store = id_store
        extras = getattr(state, "extras", None)
        if isinstance(extras, dict):
            extras["identity_store"] = id_store
        # Memory W2: the high-level resolve/link facade over the same
        # store. The servicer's _memory_scope reads this to map
        # (channel, sender) → canonical UserId for per-user memory.
        try:
            from corlinman_identity import UserIdentityResolver

            state.identity_resolver = UserIdentityResolver(id_store)
            logger.info("gateway.c2.identity_resolver_wired")
        except Exception as resolver_exc:  # noqa: BLE001 — scope falls back to raw sender
            logger.warning(
                "gateway.c2.identity_resolver_failed", error=str(resolver_exc)
            )
            with suppress(AttributeError, TypeError):
                state.identity_resolver = None
        logger.info("gateway.c2.identity_store_wired", path=str(id_path))
    except Exception as exc:  # noqa: BLE001 — routes 503 cleanly
        logger.warning("gateway.c2.identity_store_failed", error=str(exc))
        with suppress(AttributeError, TypeError):
            state.identity_store = None
            state.identity_resolver = None

    # --- agent_runner_fn (gap goals-cron-run-agent-dead) -----------------
    try:
        state.agent_runner_fn = _build_agent_runner_fn(state)
        logger.info("gateway.c2.agent_runner_fn_wired")
    except Exception as exc:  # noqa: BLE001
        logger.warning("gateway.c2.agent_runner_fn_failed", error=str(exc))
        with suppress(AttributeError, TypeError):
            state.agent_runner_fn = None

    # --- hook_runner (C3) ------------------------------------------------
    try:
        from corlinman_hooks.runner import HookRunner

        # The agent-level hooks config lives under ``[hooks]`` in the
        # loaded config; file-discovered HOOK.yaml/handler.py hooks load
        # from CORLINMAN_HOOKS_DIR (falls back to <data_dir>/hooks).
        hooks_cfg: dict[str, Any] = {}
        section = _extract_section(cfg, "hooks")
        if isinstance(section, dict):
            hooks_cfg = {"hooks": section}
        hooks_dir_env = os.environ.get("CORLINMAN_HOOKS_DIR")
        hooks_dir: Path | None
        if hooks_dir_env:
            hooks_dir = Path(hooks_dir_env)
        else:
            default_hooks_dir = data_dir / "hooks"
            hooks_dir = default_hooks_dir if default_hooks_dir.is_dir() else None
        # Declarative-hook ``if`` matchers reuse the permission-rule
        # grammar; corlinman-hooks cannot import corlinman-agent, so the
        # grammar is injected here (design-once contract).
        _rule_matcher: Any = None
        try:
            from corlinman_agent.permission import match_hook_rule

            _rule_matcher = match_hook_rule
        except ImportError:  # pragma: no cover — agent pkg absent in minimal installs
            pass
        runner = HookRunner(hooks_cfg, hooks_dir=hooks_dir, rule_matcher=_rule_matcher)
        state.hook_runner = runner
        app.state.corlinman_hook_runner = runner
        extras = getattr(state, "extras", None)
        if isinstance(extras, dict):
            extras["hook_runner"] = runner
        logger.info(
            "gateway.c2.hook_runner_wired",
            hooks_dir=str(hooks_dir) if hooks_dir else None,
            discovered=getattr(runner, "discovered_events", {}),
        )
    except Exception as exc:  # noqa: BLE001 — no hooks degrades fine
        logger.warning("gateway.c2.hook_runner_failed", error=str(exc))
        with suppress(AttributeError, TypeError):
            state.hook_runner = None
