"""``main()`` — boot the grpc.aio server and handle SIGTERM → exit 143.

Usage (installed as console script ``corlinman-python-server``)::

    corlinman-python-server

Defaults to a Unix domain socket at ``$CORLINMAN_PY_SOCKET`` or
``/tmp/corlinman-py.sock`` so the Rust gateway can co-locate without a TCP
port. Servicers are registered below — each is currently a no-op stub
until M1/M2.

TODO(M1): register the Agent / Embedding servicers once proto stubs exist
in :mod:`corlinman_grpc`.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from typing import Any, Final

import grpc.aio
import structlog
from corlinman_grpc import agent_pb2_grpc
from corlinman_providers import AliasEntry, ProviderRegistry, ProviderSpec

from corlinman_server.agent_journal import AgentJournal
from corlinman_server.agent_servicer import CorlinmanAgentServicer, _resolve_data_dir
from corlinman_server.auto_resume import (
    open_inbox_for_boot_resume,
    run_boot_auto_resume,
)
from corlinman_server.middleware import install_tracecontext_interceptor
from corlinman_server.shutdown import GracefulShutdown
from corlinman_server.telemetry import init_telemetry, shutdown_telemetry

# Process-wide hook bus. Constructed lazily on first :func:`_build_hook_bus`
# call so unit-test imports of this module don't drag in the hooks package
# when there is no live server. The Agent servicer holds a reference, and
# downstream plugins (admin live feed, audit log, classifier prefilter)
# attach push-based subscribers via ``hook_bus.subscribe(predicate, fn)``.
_HOOK_BUS: Any | None = None


def _build_hook_bus() -> Any:
    """Return the process-wide :class:`HookBus`.

    The import is lazy so ``corlinman-hooks`` stays a soft dependency
    (a stripped-down test build can still boot the servicer with
    ``hook_bus=None``).
    """
    global _HOOK_BUS
    if _HOOK_BUS is not None:
        return _HOOK_BUS
    try:
        from corlinman_hooks import HookBus

        _HOOK_BUS = HookBus()
        logger.info("hooks.bus.ready")
    except Exception as exc:  # noqa: BLE001 — degrade silently
        logger.warning("hooks.bus.init_failed", error=str(exc))
        _HOOK_BUS = None
    return _HOOK_BUS


def _build_hook_runner() -> Any | None:
    """Build the pre-tool :class:`HookRunner` for the standalone server.

    BUG-01: the blocking ``PreToolDispatch`` gate only ran when a HookRunner
    was wired into the servicer, but only the gateway *lifespan* (a separate
    process) ever built one — the standalone ``corlinman-python-server`` never
    received it, so the gate was inert while the telemetry event still fired.
    This mirrors the gateway's ``[hooks]`` + ``CORLINMAN_HOOKS_DIR`` (falling
    back to ``<data_dir>/hooks``) discovery so a stock standalone boot gets a
    live gate. Best-effort: any failure (missing ``corlinman-hooks``, bad hook
    dir) degrades to ``None`` and the server boots with the gate disabled.
    """
    try:
        from corlinman_hooks.runner import HookRunner

        data_dir = _resolve_data_dir()
        # The agent-level shell-hook config lives under ``[hooks]`` of the
        # py-config drop the gateway writes; absent that we just discover
        # file-based HOOK.yaml hooks from the hooks dir.
        hooks_cfg: dict[str, Any] = {}
        path = os.environ.get("CORLINMAN_PY_CONFIG")
        if path:
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                section = data.get("hooks") if isinstance(data, dict) else None
                if isinstance(section, dict):
                    hooks_cfg = {"hooks": section}
            except Exception as exc:  # noqa: BLE001 — config read is best-effort
                logger.warning("hooks.runner.config_read_failed", error=str(exc))
        hooks_dir_env = os.environ.get("CORLINMAN_HOOKS_DIR")
        hooks_dir: Path | None
        if hooks_dir_env:
            hooks_dir = Path(hooks_dir_env)
        else:
            default_hooks_dir = data_dir / "hooks"
            hooks_dir = default_hooks_dir if default_hooks_dir.is_dir() else None
        runner = HookRunner(hooks_cfg, hooks_dir=hooks_dir)
        logger.info(
            "hooks.runner.ready",
            hooks_dir=str(hooks_dir) if hooks_dir else None,
            discovered=getattr(runner, "discovered_events", {}),
        )
        return runner
    except Exception as exc:  # noqa: BLE001 — no hooks degrades fine
        logger.warning("hooks.runner.init_failed", error=str(exc))
        return None

logger = structlog.get_logger(__name__)

_DEFAULT_SOCKET: Final[str] = "/tmp/corlinman-py.sock"
_DEFAULT_TCP_ADDR: Final[str] = "127.0.0.1:50051"
_SIGTERM_EXIT_CODE: Final[int] = 143


class _ReloadingProviderResolver:
    """File-mtime-aware wrapper around :class:`ProviderRegistry`.

    The Rust gateway re-writes ``$CORLINMAN_PY_CONFIG`` after every admin
    mutation (``POST /admin/{config,providers,embedding,models}``). This
    wrapper checks the file mtime before each resolve; on change it
    rebuilds the underlying registry + alias table atomically.

    The surface matches what :class:`CorlinmanAgentServicer` expects of a
    resolver: callable as ``(alias_or_model=..., aliases=...)`` returning
    ``(provider, upstream_model, merged_params)``. The ``aliases=`` kwarg
    passed by the servicer is *ignored* — this class owns the live alias
    map and the servicer's copy is a no-op pass-through (kept for signature
    compatibility).
    """

    def __init__(self, path: str | None) -> None:
        self._path = path
        self._mtime: float | None = None
        # Resolve the data dir once at construction so OAuth-aware
        # adapters (AnthropicProvider today) can locate their token
        # files under ``<data_dir>/.oauth/``. Held on the resolver so
        # every rebuild after a config-file write picks the same dir
        # up — env changes mid-run would be picked up only on next
        # restart, which is the same model as the rest of the gateway.
        self._data_dir = _resolve_data_dir()
        self._registry = ProviderRegistry([], data_dir=self._data_dir)
        self._aliases: dict[str, AliasEntry] = {}
        self._subagent_config: dict[str, Any] = {}
        if path:
            self._reload_if_changed()

    def _reload_if_changed(self) -> None:
        """Re-read the JSON file if its mtime moved. Logs on first load +
        every subsequent reload — the success-criterion grep for
        ``providers.registered`` in the doc walks this exact event."""
        if not self._path:
            return
        try:
            mtime = Path(self._path).stat().st_mtime
        except OSError:
            # File vanished — keep whatever registry we had. A subsequent
            # write will land a fresh mtime and we'll pick it up.
            return
        if self._mtime is not None and mtime == self._mtime:
            return
        is_first_load = self._mtime is None
        specs, aliases, subagent_config = _load_config()
        self._registry = ProviderRegistry(specs, data_dir=self._data_dir)
        self._aliases = aliases
        self._subagent_config = subagent_config
        self._mtime = mtime
        event = "providers.registered" if is_first_load else "providers.reloaded"
        logger.info(
            event,
            count=len(specs),
            enabled=sum(1 for s in specs if s.enabled),
            aliases=len(aliases),
        )

    @property
    def aliases(self) -> dict[str, AliasEntry]:
        """Snapshot of the current alias map."""
        return dict(self._aliases)

    @property
    def subagent_config(self) -> dict[str, Any]:
        """Snapshot of the current ``[subagent]`` policy config."""
        return dict(self._subagent_config)

    def __call__(
        self,
        alias_or_model: str,
        aliases: Any = None,
        provider_hint: str | None = None,
    ) -> tuple[Any, str, dict[str, Any]]:
        _ = aliases  # servicer-supplied aliases ignored; we own the live map
        self._reload_if_changed()
        return self._registry.resolve(
            alias_or_model=alias_or_model,
            aliases=self._aliases,
            provider_hint=provider_hint,
        )


def _load_config() -> tuple[list[ProviderSpec], dict[str, AliasEntry], dict[str, Any]]:
    """Read the Python-side config from ``CORLINMAN_PY_CONFIG`` if set.

    The Rust gateway writes a JSON file with ``providers`` + ``aliases``
    blocks translated from its ``config.toml`` before spawning this
    subprocess. The schema is:

    .. code-block:: json

        {
          "providers": [{"name": "...", "kind": "...",
                         "api_key": "...", "base_url": "...",
                         "enabled": true, "params": {...}}, ...],
          "aliases":   {"<alias>": {"provider": "...",
                                    "model": "...",
                                    "params": {...}}, ...},
          "subagent":  {"max_concurrent_per_parent": 10,
                        "max_concurrent_per_tenant": 15,
                        "max_depth": 1,
                        "max_wall_seconds_ceiling": 300}
        }

    When the env var is unset we return empty collections — the registry
    then serves every request via the legacy prefix fallback (M2
    behaviour), which keeps existing deployments working without any
    config-file migration.

    Env-based IPC (vs a second gRPC admin channel) was chosen because the
    Python side already learns about transport config from env vars
    (``CORLINMAN_PY_SOCKET`` / ``CORLINMAN_PY_PORT``); staying inside that
    same channel is the surgical path that doesn't introduce a new
    circular bootstrap problem between the two processes.
    """
    path = os.environ.get("CORLINMAN_PY_CONFIG")
    if not path:
        return [], {}, {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("py_config.load_failed", path=path, error=str(exc))
        return [], {}, {}

    specs: list[ProviderSpec] = []
    for entry in data.get("providers", []) or []:
        try:
            specs.append(ProviderSpec.model_validate(entry))
        except Exception as exc:
            logger.warning("py_config.provider_invalid", entry=entry, error=str(exc))

    aliases: dict[str, AliasEntry] = {}
    raw_aliases: Any = data.get("aliases") or {}
    if isinstance(raw_aliases, dict):
        for name, body in raw_aliases.items():
            try:
                aliases[name] = AliasEntry.model_validate(body)
            except Exception as exc:
                logger.warning(
                    "py_config.alias_invalid", alias=name, error=str(exc)
                )

    subagent: dict[str, Any] = {}
    raw_subagent: Any = data.get("subagent") or {}
    if isinstance(raw_subagent, dict):
        subagent = dict(raw_subagent)

    return specs, aliases, subagent


def _bind_address() -> str:
    """Resolve the gRPC bind address from env.

    Precedence:
      1. ``CORLINMAN_PY_SOCKET`` — Unix domain socket path.
      2. ``CORLINMAN_PY_ADDR``   — explicit ``host:port`` (e.g. ``127.0.0.1:50051``).
      3. ``CORLINMAN_PY_PORT``   — port only, bound to ``127.0.0.1``.
      4. default Unix socket at ``/tmp/corlinman-py.sock``.
    """
    sock = os.environ.get("CORLINMAN_PY_SOCKET")
    if sock:
        return f"unix://{sock}"
    addr = os.environ.get("CORLINMAN_PY_ADDR")
    if addr:
        return addr
    port = os.environ.get("CORLINMAN_PY_PORT")
    if port:
        return f"127.0.0.1:{port}"
    return f"unix://{_DEFAULT_SOCKET}"


async def _run_boot_auto_resume() -> None:
    """Open the journal + inbox and run one auto-resume scan.

    Best-effort: every failure path degrades to a warning and returns —
    a crashed journal must NEVER block the gateway from accepting fresh
    traffic. The chat handler's per-RPC ``find_resumable_turn`` lookup
    still works without this scan; the scan only adds the cross-channel
    re-delivery that turns "user has to resend" into "agent picks up
    automatically".
    """
    data_dir = _resolve_data_dir()
    try:
        journal = await AgentJournal.open_from_env(
            data_dir / "agent_journal.sqlite"
        )
    except Exception as exc:  # noqa: BLE001 — degrade silently
        logger.warning("agent.resume.journal_open_failed", error=str(exc))
        return

    inbox = await open_inbox_for_boot_resume(data_dir)
    try:
        await run_boot_auto_resume(journal, inbox)
    except Exception as exc:  # noqa: BLE001 — degrade silently
        logger.warning("agent.resume.scan_failed", error=str(exc))
    finally:
        # Close the boot-time handles so the servicer's lazy open
        # opens a fresh connection. SQLite handles cross-connection
        # consistency via WAL; Postgres handles it via the connection
        # pool.
        try:
            await journal.close()
        except Exception:  # noqa: BLE001
            pass
        if inbox is not None:
            try:
                await inbox.close()
            except Exception:  # noqa: BLE001
                pass


async def _build_event_emitter() -> tuple[Any | None, Any | None]:
    """Construct the journal-backed event emitter for the Agent servicer.

    Cross-process observability bridge (half A): production runs this
    standalone agent server in a SEPARATE process from the gateway, so
    the gateway-lifespan emitter wiring never reaches the servicer here
    — without this, no ``turn_events`` were journaled in prod and the
    admin ``/admin/sessions/{key}/events/live`` SSE stayed silent for
    chat turns. We open the SAME journal the servicer's lazy
    ``_get_journal`` resolves (``_resolve_data_dir() /
    "agent_journal.sqlite"`` through ``AgentJournal.open_from_env``, so
    ``CORLINMAN_JOURNAL_BACKEND`` overrides apply identically) and wrap
    it in the gateway's :class:`JournalBackedEmitter`. The gateway
    process serves live SSE for these rows via its journal-polling
    fallback (``gateway/routes_admin_b/infra/sessions_events.py``).

    Two handles onto one sqlite file (this emitter's + the servicer's
    lazy one) are safe: the backend opens WAL mode with a 5s
    ``busy_timeout``, the same posture as the gateway-vs-agent split
    already in production.

    The import is lazy and the whole construction is best-effort: any
    failure logs a warning and returns ``(None, None)`` — boot must
    never crash on observability wiring. (``gateway.observability``
    only pulls ``corlinman_agent.events`` + structlog, so the import
    itself is light; the try still guards a stripped-down build.)

    Returns ``(emitter, journal)`` so :func:`_serve` can close the
    journal handle — and reap its aiosqlite worker thread — at
    shutdown.
    """
    try:
        from corlinman_server.gateway.observability import (  # noqa: PLC0415
            JournalBackedEmitter,
        )

        path = _resolve_data_dir() / "agent_journal.sqlite"
        journal = await AgentJournal.open_from_env(path)
        emitter = JournalBackedEmitter(journal)
        logger.info("agent.event_emitter.ready", path=str(path))
        return emitter, journal
    except Exception as exc:  # noqa: BLE001 — boot must never crash here
        logger.warning("agent.event_emitter.init_failed", error=str(exc))
        return None, None


async def _serve() -> int:
    """Run the server until SIGTERM / SIGINT is received.

    Returns the process exit code (143 on SIGTERM, 0 on clean shutdown).
    """
    # S7.T1: install the OTLP tracer + structlog trace_id/span_id binding
    # once per process. No-op when OTEL_EXPORTER_OTLP_ENDPOINT is unset,
    # and warn-and-continue on any exporter failure.
    init_telemetry()

    shutdown = GracefulShutdown()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown.request, sig.name)

    server = grpc.aio.server(
        interceptors=[install_tracecontext_interceptor()],
        options=[
            ("grpc.max_send_message_length", 64 * 1024 * 1024),
            ("grpc.max_receive_message_length", 64 * 1024 * 1024),
            # Mirror the client's keepalive policy
            # (``corlinman_grpc.agent_client.connect_channel``) so long
            # agent turns don't trip the server's default
            # ``max_ping_strikes=2`` after the gateway sends two keepalive
            # pings without data, which surfaces upstream as
            # ``UNAVAILABLE: Too many pings``.
            ("grpc.keepalive_time_ms", 30_000),
            ("grpc.keepalive_timeout_ms", 10_000),
            ("grpc.keepalive_permit_without_calls", 1),
            ("grpc.http2.min_recv_ping_interval_without_data_ms", 10_000),
            ("grpc.http2.max_ping_strikes", 0),
        ],
    )

    # M2: real Agent servicer drives corlinman_agent.ReasoningLoop.
    # Feature C: load the spec-driven provider registry + alias table from
    # the Rust gateway's JSON drop (path in ``CORLINMAN_PY_CONFIG``). Empty
    # config is valid — the servicer falls back to the legacy prefix table.
    #
    # The resolver is a file-mtime-aware wrapper so admin writes on the
    # Rust side (which rewrite py-config.json atomically) propagate here
    # without a process restart.
    # Process-wide hook bus, shared across servicers. ``None`` is allowed
    # (the servicer treats it as "no hook fan-out") so a stripped-down
    # build without ``corlinman-hooks`` still boots.
    hook_bus = _build_hook_bus()
    # BUG-01: build the pre-tool hook runner so the standalone server's
    # blocking PreToolDispatch gate is actually live (not just telemetry).
    hook_runner = _build_hook_runner()

    # Cross-process observability bridge (half A): journal-backed event
    # emitter over the shared agent journal, so THIS process journals
    # turn_events and the gateway's live SSE can poll them. Best-effort
    # — ``(None, None)`` on any failure and the servicer runs exactly as
    # before. See :func:`_build_event_emitter`.
    event_emitter, observability_journal = await _build_event_emitter()

    if os.environ.get("CORLINMAN_TEST_MOCK_PROVIDER") is not None:
        # Test smoke path: leave provider_resolver unset so the Agent
        # servicer activates its offline mock provider instead of falling
        # through to legacy real-provider prefix matching.
        logger.info("providers.registered", count=0, enabled=0, aliases=0)
        specs, aliases, subagent_config = _load_config()
        _ = (specs, aliases)
        agent_servicer = CorlinmanAgentServicer(
            hook_bus=hook_bus,
            hook_runner=hook_runner,
            subagent_config=subagent_config,
            event_emitter=event_emitter,
        )
    else:
        py_config_path = os.environ.get("CORLINMAN_PY_CONFIG")
        resolver = _ReloadingProviderResolver(py_config_path)
        if py_config_path is None:
            # No config handshake → legacy prefix fallback for every resolve.
            # Log zeros so the boot-time grep target stays consistent.
            logger.info("providers.registered", count=0, enabled=0, aliases=0)
        agent_servicer = CorlinmanAgentServicer(
            provider_resolver=resolver,
            aliases=resolver.aliases,
            hook_bus=hook_bus,
            hook_runner=hook_runner,
            subagent_config=resolver.subagent_config,
            event_emitter=event_emitter,
        )
    agent_pb2_grpc.add_AgentServicer_to_server(agent_servicer, server)

    bind = _bind_address()
    server.add_insecure_port(bind)
    logger.info("grpc.server.start", bind=bind)
    print(f"corlinman-server ready (Agent servicer registered) — bind={bind}", flush=True)

    await server.start()

    # Auto-resume — scan the journal for in_progress rows left over by a
    # previous crash and re-deliver them through the right channel
    # surface. Runs once AFTER the gRPC server starts (so a synthesized
    # inbox row that triggers a fresh Chat RPC lands on a live socket)
    # but BEFORE we block on shutdown — the scan is best-effort and
    # never blocks boot. Operators grep ``agent.resume.scan_complete``
    # to confirm it fired.
    await _run_boot_auto_resume()

    reason = await shutdown.wait()
    logger.info("grpc.server.shutdown", reason=reason)

    # R4: close the servicer's owned resources (journal, memory store,
    # blackboard, hook bus) BEFORE the gRPC server stop. ``aclose`` is
    # individually wrapped per resource so a torn Postgres pool does
    # not block the SQLite memory host from flushing its WAL.
    try:
        await agent_servicer.aclose()
    except Exception as exc:  # noqa: BLE001 — never block shutdown
        logger.warning("server.shutdown.aclose_failed", error=str(exc))

    # Close the observability emitter's journal handle. It is a SECOND
    # handle onto the same WAL-mode sqlite file as the servicer's lazy
    # one (closed by ``aclose`` above) — safe to hold concurrently, but
    # it owns its own aiosqlite worker thread that must be reaped here.
    if observability_journal is not None:
        try:
            await observability_journal.close()
        except Exception as exc:  # noqa: BLE001 — never block shutdown
            logger.warning(
                "server.shutdown.observability_journal_close_failed",
                error=str(exc),
            )

    # 5s grace for in-flight RPCs, then force close.
    await server.stop(grace=5.0)
    logger.info("grpc.server.stopped")

    shutdown_telemetry()

    return _SIGTERM_EXIT_CODE if reason == "SIGTERM" else 0


def main() -> None:
    """Entrypoint wrapper — runs :func:`_serve` and exits with its code."""
    try:
        code = asyncio.run(_serve())
    except KeyboardInterrupt:
        code = _SIGTERM_EXIT_CODE
    sys.exit(code)


if __name__ == "__main__":  # pragma: no cover — module entrypoint
    main()
