"""Embedded brain — the full agent servicer hosted inside the console.

hermes-agent's model: the CLI process *is* the agent. We get there without
a parallel code path by booting the production
:class:`~corlinman_server.agent_servicer.CorlinmanAgentServicer` on a
**private per-process UDS** inside the console's event loop, then dialling
it through the exact gateway plumbing
(:class:`~corlinman_grpc.agent_client.AgentClient` →
``GrpcAgentChatBackend`` → ``ChatService``). Same wire contract, same
builtin tools (``run_shell`` / ``subagent.spawn*`` / memory / …), same
journal — only the process topology differs from production.

Differences from the gateway co-host (``gateway/grpc/agent_server.py``),
on purpose:

* **Private socket** — ``<data_dir>/run/console-<pid>.sock`` (0700 dir),
  never the shared ``/tmp/corlinman-py.sock``, so a console never
  cross-wires with a running gateway/agent pair. UDS only; the in-proc
  servicer has no auth, so no TCP bind exists in this path at all.
* **No boot auto-resume** — the gateway's resume scan re-delivers crashed
  turns through *channel* surfaces; an interactive console must never
  side-effect channels just because it was opened.

Provider resolution follows the standalone server: the servicer reads the
``CORLINMAN_PY_CONFIG`` JSON drop. When the env var is unset we point it at
the gateway's default drop location (``<data_dir>/py-config.json``) if one
exists — a host that has run the gateway before gets its full provider +
alias table; a bare host falls back to the legacy env-key prefix table.

When the gRPC stack is unavailable (stripped install), construction falls
back to :class:`DirectProviderBackend` — provider streaming only, no
tools — and says so in :attr:`EmbeddedBrain.descriptor`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from corlinman_server.console.events import ConsoleEvent, from_internal_events

__all__ = ["EmbeddedBrain", "EmbeddedBrainError"]

log = logging.getLogger(__name__)

_READY_TIMEOUT_S = 15.0


class EmbeddedBrainError(RuntimeError):
    """Embedded brain could not be constructed at all (neither the full
    agent path nor the direct-provider fallback)."""


def _ensure_py_config_env(data_dir: Path, config: dict[str, Any] | None = None) -> None:
    """Point ``CORLINMAN_PY_CONFIG`` at the gateway's drop when unset.

    The servicer and the ``_ReloadingProviderResolver`` both read this
    env var; exporting it here is how the console inherits the exact
    provider/alias table the admin UI manages. No-op when the operator
    already set the var.

    Standalone bootstrap: on a host where the gateway has never run
    (``corlinman init`` wrote a ``config.toml`` but no ``py-config.json``
    drop exists yet) the servicer's ``_load_config()`` would see zero
    providers and fall back to the legacy env-key prefix table, ignoring
    everything in the TOML. So when ``config`` carries provider/model
    blocks we render the drop ourselves with the gateway's own writer —
    the exact same translation a gateway boot performs.
    """
    if os.environ.get("CORLINMAN_PY_CONFIG"):
        return
    drop = data_dir / "py-config.json"
    has_config = isinstance(config, dict) and bool(
        config.get("providers") or config.get("models")
    )
    # Render (or refresh) the drop from config.toml when it is missing
    # OR the TOML was edited after the drop was last written. A running
    # gateway rewrites the drop on every admin mutation, so a TOML mtime
    # newer than the drop means the drop is stale either way — refreshing
    # is exactly what the next gateway boot would do.
    stale = False
    if drop.is_file() and has_config:
        toml_path = data_dir / "config.toml"
        with contextlib.suppress(OSError):
            stale = toml_path.stat().st_mtime > drop.stat().st_mtime
    if has_config and (not drop.is_file() or stale):
        try:
            from corlinman_server.gateway.lifecycle.py_config import (  # noqa: PLC0415
                write_py_config_sync,
            )

            write_py_config_sync(config, drop)
            log.info(
                "console.embedded.py_config_generated path=%s refreshed=%s",
                drop,
                stale,
            )
        except Exception as exc:  # noqa: BLE001 — bootstrap is best-effort
            log.warning("console.embedded.py_config_generate_failed err=%s", exc)
    if drop.is_file():
        os.environ["CORLINMAN_PY_CONFIG"] = str(drop)
        log.info("console.embedded.py_config path=%s", drop)


def _mcp_policy_from_config(
    config: dict[str, Any] | None,
) -> tuple[frozenset[str] | None, frozenset[str]]:
    """Read the ``[mcp]`` allow/deny server policy (claude-code
    ``allowedMcpServers``/``deniedMcpServers``) — same semantics as the
    gateway's ``_mcp_server_policy``: deny wins; a non-empty allow-list
    is exclusive. Returns ``(allowed_or_None, denied)``."""
    mcp_cfg = config.get("mcp") if isinstance(config, dict) else None
    mcp_cfg = mcp_cfg if isinstance(mcp_cfg, dict) else {}
    denied = frozenset(
        str(s)
        for s in (mcp_cfg.get("deniedMcpServers") or mcp_cfg.get("denied") or [])
    )
    allowed_raw = mcp_cfg.get("allowedMcpServers") or mcp_cfg.get("allowed")
    allowed = (
        frozenset(str(s) for s in allowed_raw)
        if isinstance(allowed_raw, (list, tuple, set)) and allowed_raw
        else None
    )
    return allowed, denied


def _load_subagent_config() -> dict[str, Any] | None:
    """Read the ``subagent`` policy block from the py-config drop, if any."""
    path = os.environ.get("CORLINMAN_PY_CONFIG")
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    block = data.get("subagent") if isinstance(data, dict) else None
    return dict(block) if isinstance(block, dict) else None


async def _build_plugin_tool_executor(
    data_dir: Path,
    mcp_manager: Any | None = None,
    mcp_policy: tuple[frozenset[str] | None, frozenset[str]] | None = None,
) -> tuple[Any | None, bytes, Any | None]:
    """Build the same plugin-tool executor the gateway wires.

    Production (``grpc_backend.build_tool_executor``) binds a
    :class:`RegistryToolExecutor` to ``AppState.plugin_registry`` — built
    by the lifecycle's ``_wire_plugin_hotload`` from the
    ``CORLINMAN_PLUGIN_DIRS`` roots plus the *enabled* marketplace plugins
    under ``<data_dir>/plugins``. Without it, a plugin/MCP tool call from
    a console turn would be acknowledged with the
    ``awaiting_plugin_runtime`` placeholder instead of executing.

    Dim 5 — when ``mcp_manager`` is live, the discovered MCP tools are
    surfaced the same way the gateway does it (``register_mcp_tools``:
    synthesized ``mcp``-kind registry entries for execution + a
    ``tools_json`` array for advertisement). Returns
    ``(executor_or_None, advertised_tools_json)``.

    Best-effort: any failure returns ``(None, b"", None)`` and the
    ChatService keeps its PlaceholderExecutor default (builtin tools are
    unaffected — they execute inside the servicer). The third element is
    the live :class:`PluginRegistry` so the ``/mcp`` hot-plug refresh can
    re-run the advertisement pass against it.
    """
    try:
        from corlinman_grpc.agent_client import RegistryToolExecutor  # noqa: PLC0415
        from corlinman_providers.plugins import (  # noqa: PLC0415
            PluginRegistry,
            roots_from_env_var,
        )
        from corlinman_providers.plugins.discovery import Origin  # noqa: PLC0415

        from corlinman_server.gateway.grpc.plugin_invoker import (  # noqa: PLC0415
            build_registry_invoker,
        )
        from corlinman_server.system.marketplace.plugin_runtime import (  # noqa: PLC0415
            sync_registry,
        )
    except Exception as exc:  # noqa: BLE001 — degraded console still works
        log.info("console.embedded.plugin_runtime_unavailable err=%s", exc)
        return None, b"", None

    try:
        registry = PluginRegistry.from_roots(
            list(roots_from_env_var("CORLINMAN_PLUGIN_DIRS", Origin.CONFIG))
        )
        enabled: set[str] = set()
        store_path = data_dir / "plugins.sqlite"
        if store_path.is_file():
            with contextlib.suppress(Exception):
                from corlinman_server.system.marketplace.plugin_store import (  # noqa: PLC0415
                    PluginStore,
                )

                store = PluginStore(store_path)
                enabled = {row.slug for row in store.list() if row.enabled}
        await sync_registry(registry, data_dir / "plugins", enabled)
        # Dim 5 — surface the connected MCP servers' tools exactly like
        # the gateway's ``_wire_mcp_tool_plane``: synthesized ``mcp``-kind
        # entries route execution through the invoker's MCP bridge; the
        # returned tools_json advertises them to the model.
        mcp_tools_json = b""
        if mcp_manager is not None:
            with contextlib.suppress(Exception):
                from corlinman_server.gateway.mcp.advertise import (  # noqa: PLC0415
                    register_mcp_tools,
                )

                allowed, denied = mcp_policy or (None, frozenset())
                _res_fn = getattr(mcp_manager, "discovered_resources", None)
                _added, mcp_tools_json, _servers = await register_mcp_tools(
                    registry,
                    mcp_manager.discovered_tools(),
                    allowed=allowed,
                    denied=denied,
                    resources=_res_fn() if callable(_res_fn) else None,
                )
        # Same invoker production uses (grpc_backend.build_tool_executor);
        # the console has no plugin supervisor, so that plugin kind
        # degrades exactly like a degraded gateway boot.
        invoker = build_registry_invoker(
            registry, supervisor=None, mcp_manager=mcp_manager
        )
        executor = RegistryToolExecutor(invoker)
        log.info(
            "console.embedded.plugin_executor_wired plugins=%d mcp_tools=%s",
            len(registry),
            bool(mcp_tools_json),
        )
        return executor, mcp_tools_json, registry
    except Exception as exc:  # noqa: BLE001
        log.warning("console.embedded.plugin_executor_failed err=%s", exc)
        return None, b"", None


class EmbeddedBrain:
    """Console-hosted full agent. Build via :meth:`start`."""

    def __init__(self) -> None:
        self.descriptor: str = "embedded (not started)"
        self._server: Any | None = None
        self._servicer: Any | None = None
        self._channel: Any | None = None
        self._service: Any | None = None
        self._sock_path: Path | None = None
        self._tools_enabled = False
        self._config: dict[str, Any] | None = None
        self._mcp_manager: Any | None = None
        self._plugin_registry: Any | None = None

    @property
    def tools_enabled(self) -> bool:
        """Whether the full agent path (builtin tools, subagents) is live —
        ``False`` means the direct-provider fallback is serving."""
        return self._tools_enabled

    @property
    def mcp_manager(self) -> Any | None:
        """The live :class:`McpClientManager` (Dim 5 ``/mcp``), or ``None``
        when no ``[mcp]`` servers are configured / the embedded MCP
        bring-up degraded."""
        return self._mcp_manager

    async def ensure_mcp_manager(self) -> Any | None:
        """Return the live MCP manager, lazily creating an empty one.

        ``/mcp add`` must work on a console booted with no ``[mcp]``
        config at all — an empty manager accepts ``add_server`` and the
        refresh pass advertises whatever comes up.
        """
        if self._mcp_manager is None and self._service is not None:
            try:
                from corlinman_mcp_server import McpClientManager  # noqa: PLC0415

                manager = McpClientManager.from_config(self._config or {})
                # Flip the manager into its connected state (no-op on an
                # empty spec list) so a subsequent ``add_server`` brings
                # the new server up immediately instead of parking it
                # until a connect_all that never comes.
                await manager.connect_all()
                self._mcp_manager = manager
            except Exception as exc:  # noqa: BLE001 — package missing
                log.warning("console.embedded.mcp_unavailable err=%s", exc)
                return None
        return self._mcp_manager

    async def refresh_mcp_tools(self) -> bool:
        """Re-advertise + re-route the MCP tool plane after ``/mcp``
        hot-plug (mirrors the gateway's ``refresh_mcp_advertisement``).

        Re-runs ``register_mcp_tools`` against the live plugin registry,
        prunes synthesized entries for servers that dropped out, and
        swaps the ChatService's advertised ``tools_json``. Returns
        ``False`` when the embedded tool plane isn't wired (direct
        fallback / degraded plugin runtime).
        """
        manager = self._mcp_manager
        service = self._service
        registry = self._plugin_registry
        if manager is None or service is None or registry is None:
            return False
        try:
            from corlinman_server.gateway.mcp.advertise import (  # noqa: PLC0415
                prune_stale_mcp_entries,
                register_mcp_tools,
            )

            allowed, denied = _mcp_policy_from_config(self._config)
            _res_fn = getattr(manager, "discovered_resources", None)
            _added, tools_json, advertised = await register_mcp_tools(
                registry,
                manager.discovered_tools(),
                allowed=allowed,
                denied=denied,
                resources=_res_fn() if callable(_res_fn) else None,
            )
            await prune_stale_mcp_entries(registry, advertised)
            service.with_advertised_tools(tools_json)
            return True
        except Exception as exc:  # noqa: BLE001 — best-effort refresh
            log.warning("console.embedded.mcp_refresh_failed err=%s", exc)
            return False

    # ── construction ─────────────────────────────────────────────────

    @classmethod
    async def start(
        cls, data_dir: Path, *, config: dict[str, Any] | None = None
    ) -> EmbeddedBrain:
        """Boot the brain: full agent over a private UDS, falling back to
        the direct provider backend when gRPC is unavailable.

        ``config`` is the parsed ``config.toml`` — used to bootstrap the
        provider drop on standalone hosts (see ``_ensure_py_config_env``)
        and to bring up the ``[mcp]``-configured external servers.
        """
        self = cls()
        self._config = config if isinstance(config, dict) else None
        _ensure_py_config_env(data_dir, config)
        try:
            await self._start_agent(data_dir)
            self._tools_enabled = True
        except Exception as exc:  # noqa: BLE001 — fall back, keep the console usable
            log.warning("console.embedded.agent_unavailable err=%s", exc)
            await self._start_direct(data_dir)
        return self

    async def _connect_mcp(self) -> Any | None:
        """Bring up ``[mcp]``-configured external servers (Dim 5).

        Mirrors the gateway lifespan's MCP block: the console's Mode A
        "full brain" contract includes the external MCP tool face, which
        was previously gateway-only (``build_registry_invoker`` got
        ``mcp_manager=None`` here, so every MCP tool call degraded).
        Best-effort — no config / missing package / all servers down
        yields ``None`` and the console boots exactly as before.
        """
        cfg = self._config
        if not isinstance(cfg, dict):
            return None
        try:
            from corlinman_mcp_server import McpClientManager  # noqa: PLC0415
            from corlinman_mcp_server.scoped_config import (  # noqa: PLC0415
                load_scoped_server_specs,
            )

            # Layered ``.mcp.json`` scopes over the inline config —
            # local > project > user > inline; the console's CWD is the
            # project scope, mirroring claude-code.
            manager = McpClientManager(load_scoped_server_specs(cfg))
            if manager.server_count == 0:
                return None
            await manager.connect_all()
            log.info(
                "console.embedded.mcp_connected servers=%d ready=%d",
                manager.server_count,
                len(manager.ready_servers()),
            )
            return manager
        except Exception as exc:  # noqa: BLE001 — degraded console still works
            log.warning("console.embedded.mcp_unavailable err=%s", exc)
            return None

    async def _start_agent(self, data_dir: Path) -> None:
        """Full path: in-process servicer on a private UDS."""
        import grpc.aio  # noqa: PLC0415 — soft dependency
        from corlinman_grpc import agent_pb2_grpc  # noqa: PLC0415
        from corlinman_grpc.agent_client import (  # noqa: PLC0415
            AgentClient,
            connect_channel,
        )

        from corlinman_server.agent_servicer import (  # noqa: PLC0415
            CorlinmanAgentServicer,
        )
        from corlinman_server.gateway.services.chat_service import (  # noqa: PLC0415
            ChatService,
            GrpcAgentChatBackend,
        )

        run_dir = data_dir / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            run_dir.chmod(0o700)
        sock = run_dir / f"console-{os.getpid()}.sock"
        with contextlib.suppress(FileNotFoundError, OSError):
            sock.unlink()
        bind = f"unix://{sock}"

        # Mirrors gateway/grpc/agent_server.py server options (keepalive
        # tuning is load-bearing for long agent turns — see SEC-204 notes
        # there). UDS-only here, so no _SAFE_HOSTS gate is needed.
        server = grpc.aio.server(
            options=[
                ("grpc.max_send_message_length", 64 * 1024 * 1024),
                ("grpc.max_receive_message_length", 64 * 1024 * 1024),
                ("grpc.keepalive_time_ms", 30_000),
                ("grpc.keepalive_timeout_ms", 10_000),
                ("grpc.keepalive_permit_without_calls", 1),
                ("grpc.http2.min_recv_ping_interval_without_data_ms", 10_000),
                ("grpc.http2.max_ping_strikes", 0),
            ],
        )

        hook_runner: Any | None = None
        with contextlib.suppress(Exception):
            from corlinman_server.main import _build_hook_runner  # noqa: PLC0415

            hook_runner = _build_hook_runner()

        # Provider resolver: WITHOUT this the servicer falls back to the
        # offline-mock / legacy prefix table and a custom provider configured
        # in the TOML (e.g. an ``openai_compatible`` relay named ``cornna``)
        # is never registered — so the console died with
        # ``no provider registered for 'cornna'`` while the gateway, which
        # builds this same resolver, worked fine. Mirror the agent server's
        # construction (``main._main`` ``else`` branch): a reloading resolver
        # over the ``CORLINMAN_PY_CONFIG`` drop, plus its alias table +
        # subagent policy, so the embedded brain sees the exact provider/model
        # catalog the admin UI manages. ``_ensure_py_config_env`` (run in
        # ``start`` before this) has already pointed CORLINMAN_PY_CONFIG at the
        # drop; ``None`` path keeps the legacy fallback (no worse than before).
        from corlinman_server.tencent_policy import (  # noqa: PLC0415
            ReloadingTencentPolicyResolver,
        )

        servicer_kwargs: dict[str, Any] = {
            "hook_runner": hook_runner,
            "subagent_config": _load_subagent_config(),
            "tencent_policy_resolver": ReloadingTencentPolicyResolver(
                os.environ.get("CORLINMAN_PY_CONFIG")
            ),
        }
        try:
            from corlinman_server.main import (  # noqa: PLC0415
                _ReloadingProviderResolver,
            )

            resolver = _ReloadingProviderResolver(
                os.environ.get("CORLINMAN_PY_CONFIG")
            )
            servicer_kwargs["provider_resolver"] = resolver
            servicer_kwargs["aliases"] = resolver.aliases
            servicer_kwargs["subagent_config"] = resolver.subagent_config
        except Exception as exc:  # noqa: BLE001 — degrade to mock/legacy resolver
            log.warning("console.embedded.provider_resolver_unavailable err=%s", exc)

        servicer = CorlinmanAgentServicer(**servicer_kwargs)
        agent_pb2_grpc.add_AgentServicer_to_server(servicer, server)
        server.add_insecure_port(bind)
        await server.start()

        channel = connect_channel(bind)
        ready = channel.channel_ready()
        await asyncio.wait_for(ready, timeout=_READY_TIMEOUT_S)

        self._server = server
        self._servicer = servicer
        self._channel = channel
        self._sock_path = sock
        # Dim 5 — external MCP servers come up in Mode A too (the "full
        # brain" contract): connect, then advertise + route their tools
        # through the same seams the gateway uses.
        self._mcp_manager = await self._connect_mcp()
        tool_executor, mcp_tools_json, plugin_registry = (
            await _build_plugin_tool_executor(
                data_dir,
                mcp_manager=self._mcp_manager,
                mcp_policy=_mcp_policy_from_config(self._config),
            )
        )
        self._plugin_registry = plugin_registry
        service = ChatService(
            GrpcAgentChatBackend(AgentClient(channel)),
            advertised_tools_json=mcp_tools_json,
        )
        if tool_executor is not None:
            service.with_tool_executor(tool_executor)
        self._service = service
        self.descriptor = f"embedded full-agent ({bind})"
        log.info("console.embedded.serving bind=%s", bind)
        # Dim 9 — ``setup`` fires once per process after the embedded
        # brain is fully assembled. Advisory + best-effort.
        _setup_run = getattr(hook_runner, "run_event_async", None)
        if _setup_run is not None:
            with contextlib.suppress(Exception):
                await _setup_run("setup", {"surface": "console"}, {})

    async def _start_direct(self, data_dir: Path) -> None:
        """Fallback path: provider streaming only, no tools."""
        try:
            from corlinman_providers import ProviderRegistry  # noqa: PLC0415

            from corlinman_server.gateway.services.chat_service import (  # noqa: PLC0415
                ChatService,
            )
            from corlinman_server.gateway.services.direct_backend import (  # noqa: PLC0415
                DirectProviderBackend,
            )
            from corlinman_server.main import _load_config  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            raise EmbeddedBrainError(
                f"neither the agent gRPC stack nor the provider fallback "
                f"is importable: {exc}"
            ) from exc

        specs, aliases, _subagent = _load_config()
        # data_dir is load-bearing: OAuth-aware adapters (Anthropic today)
        # locate their token files under <data_dir>/.oauth/ — same as the
        # standalone server's _ReloadingProviderResolver.
        registry = ProviderRegistry(specs, data_dir=data_dir)
        models_config = {
            "aliases": {
                name: {
                    "provider": entry.provider,
                    "model": entry.model,
                    "params": dict(entry.params or {}),
                }
                for name, entry in aliases.items()
            }
        }
        backend = DirectProviderBackend(registry, models_config=models_config)
        self._service = ChatService(backend)
        self.descriptor = "embedded direct-provider (no tools — gRPC stack unavailable)"

    # ── permission surface (console /permissions + interactive approval) ──

    def set_permission_mode(self, mode: str) -> str | None:
        """Swap the agent's runtime permission mode; returns the resolved mode
        string, or ``None`` when the full agent path is not live (the
        direct-provider fallback runs no tools, so there is no gate)."""
        if self._servicer is None:
            return None
        return str(self._servicer.set_permission_mode(mode))

    def get_permission_mode(self) -> str | None:
        """Current permission mode string, or ``None`` without a live gate."""
        if self._servicer is None:
            return None
        return str(self._servicer.get_permission_mode())

    def set_approval_resolver(self, resolver: Any | None) -> bool:
        """Wire the interactive ``ask``-approval resolver
        (``async (tool, args, ctx) -> bool``) into the in-process servicer.
        Returns ``False`` when unavailable (direct fallback)."""
        if self._servicer is None:
            return False
        self._servicer.set_approval_resolver(resolver)
        return True

    def get_hook_runner(self) -> Any | None:
        """The live :class:`HookRunner` (console ``/hooks``), or ``None``
        when the full agent path is not live (direct-provider fallback runs
        no tools, so no hooks fire either)."""
        if self._servicer is None:
            return None
        resolver = getattr(self._servicer, "_resolve_hook_runner", None)
        return resolver() if callable(resolver) else None

    # ── Brain protocol ────────────────────────────────────────────────

    def run_turn(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        session_key: str,
        cancel: asyncio.Event,
    ) -> AsyncIterator[ConsoleEvent]:
        from corlinman_server.gateway_api import (  # noqa: PLC0415 — lazy by design
            InternalChatRequest,
            Message,
            Role,
        )

        if self._service is None:  # pragma: no cover — guarded by start()
            raise EmbeddedBrainError("brain not started")

        req = InternalChatRequest(
            model=model,
            messages=[
                Message(role=Role(m["role"]), content=m["content"])
                for m in messages
            ],
            session_key=session_key,
            stream=True,
            persona_id=None,
        )
        return from_internal_events(self._service.run(req, cancel))

    async def aclose(self) -> None:
        if self._mcp_manager is not None:
            with contextlib.suppress(Exception):
                await self._mcp_manager.aclose()
            self._mcp_manager = None
        if self._channel is not None:
            with contextlib.suppress(Exception):
                await self._channel.close()
            self._channel = None
        if self._servicer is not None:
            with contextlib.suppress(Exception):
                await self._servicer.aclose()
            self._servicer = None
        if self._server is not None:
            with contextlib.suppress(Exception):
                await self._server.stop(grace=2.0)
            self._server = None
        if self._sock_path is not None:
            with contextlib.suppress(FileNotFoundError, OSError):
                self._sock_path.unlink()
            self._sock_path = None
