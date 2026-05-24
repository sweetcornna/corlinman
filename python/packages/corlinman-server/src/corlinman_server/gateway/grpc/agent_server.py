"""In-process ``corlinman.v1.Agent`` gRPC server — Parcel **P4**.

The real ``Agent`` service is :class:`corlinman_server.agent_servicer.\
CorlinmanAgentServicer` (it drives :class:`corlinman_agent.reasoning_loop.\
ReasoningLoop` with the full tool / subagent / context-assembler
surface). The canonical way to run it is the ``corlinman-python-server``
console script (:func:`corlinman_server.main.main`) as a **separate
process** the gateway dials over a UDS / TCP.

This module is the **co-hosted** alternative: it lets the gateway boot
the same servicer *in its own event loop* so a single-process
deployment (a small VPS, a dev box) does not need a second supervised
process. Same servicer class, same proto, same wire contract — only the
process boundary differs.

It is **off by default**. The gateway co-hosts the agent only when the
operator opts in via ``$CORLINMAN_GRPC_AGENT_INPROC=1`` (or
``config["agent"]["in_process"] = true``). When it is off this module's
:func:`serve_agent_in_background` returns ``None`` and the gateway
expects an external ``corlinman-python-server`` — that is the
production-recommended topology (independent restart, independent
resource accounting).

Mirrors the binding precedence of :func:`corlinman_server.main._bind_\
address` so the in-process server listens exactly where
:func:`corlinman_server.gateway.services.grpc_backend.resolve_agent_\
target` dials.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Any

import structlog

__all__ = [
    "agent_inproc_enabled",
    "resolve_agent_bind",
    "serve_agent",
    "serve_agent_in_background",
]

log = structlog.get_logger(__name__)

#: Default UDS path for the co-hosted agent. Matches
#: :data:`corlinman_server.main._DEFAULT_SOCKET` so an external dialler
#: configured against the standard socket still finds the in-process one.
_DEFAULT_SOCKET: str = "/tmp/corlinman-py.sock"
_DEFAULT_TCP_ADDR: str = "127.0.0.1:50051"


# ---------------------------------------------------------------------------
# Opt-in gate + bind resolution
# ---------------------------------------------------------------------------


def agent_inproc_enabled(state: Any | None = None) -> bool:
    """Return whether the gateway should co-host the agent in-process.

    Precedence:

    1. ``$CORLINMAN_GRPC_AGENT_INPROC`` — ``1`` / ``true`` / ``yes`` /
       ``on`` enables it; anything else (or unset) defers to config.
    2. ``config["agent"]["in_process"]`` — declarative bool.
    3. Default ``False`` — production runs ``corlinman-python-server``
       as a separate process.
    """
    raw = (os.environ.get("CORLINMAN_GRPC_AGENT_INPROC") or "").strip().lower()
    if raw:
        return raw in ("1", "true", "yes", "on")
    if state is not None:
        cfg = getattr(state, "config", None) or {}
        agent_cfg = cfg.get("agent") if isinstance(cfg, dict) else None
        if isinstance(agent_cfg, dict):
            return bool(agent_cfg.get("in_process", False))
    return False


def resolve_agent_bind(state: Any | None = None) -> str:
    """Resolve the ``grpc.aio`` bind address for the co-hosted agent.

    Mirrors :func:`corlinman_server.main._bind_address`:

    1. ``$CORLINMAN_PY_SOCKET`` → ``unix://<path>``.
    2. ``$CORLINMAN_PY_ADDR`` → explicit ``host:port``.
    3. ``$CORLINMAN_PY_PORT`` → ``127.0.0.1:<port>``.
    4. Default ``unix://`` :data:`_DEFAULT_SOCKET`.

    ``config["agent"]["endpoint"]`` is intentionally *not* consulted
    here — that key is the **dial** target for an external agent; the
    co-hosted server owns its own bind so the two cannot be confused.
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


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


async def serve_agent(
    bind: str,
    shutdown: asyncio.Event,
) -> None:
    """Bind a ``grpc.aio`` server hosting the ``Agent`` service and serve
    until ``shutdown`` fires.

    Registers :class:`corlinman_server.agent_servicer.CorlinmanAgentServicer`
    — the *real* agent, identical to what ``corlinman-python-server``
    runs. The servicer is constructed with no explicit ``provider_resolver``
    so it resolves the same way the standalone process does: the
    ``CORLINMAN_TEST_MOCK_PROVIDER`` mock path, or
    :func:`corlinman_providers.registry.resolve` (legacy prefix table),
    or a ``CORLINMAN_PY_CONFIG`` JSON drop if the gateway emitted one.

    Best-effort: a bind failure (permission denied, port taken) is
    logged and the coroutine returns — the gateway keeps running and
    chat falls through to whatever ``ChatService`` is wired. A stale UDS
    file is unlinked before binding so a previous crash does not block
    the rebind.
    """
    try:
        import grpc.aio
        from corlinman_grpc import agent_pb2_grpc

        from corlinman_server.agent_servicer import CorlinmanAgentServicer
    except Exception as exc:
        log.warning("gateway.grpc.agent.import_failed", error=str(exc))
        return

    # Clean up a stale UDS file from a prior crash before binding.
    if bind.startswith("unix://"):
        sock_path = Path(bind[len("unix://") :])
        with contextlib.suppress(FileNotFoundError, OSError):
            sock_path.unlink()
        with contextlib.suppress(OSError):
            sock_path.parent.mkdir(parents=True, exist_ok=True)

    server = grpc.aio.server(
        options=[
            ("grpc.max_send_message_length", 64 * 1024 * 1024),
            ("grpc.max_receive_message_length", 64 * 1024 * 1024),
            # Match the client (corlinman-grpc.connect_channel) keepalive
            # policy so long agent turns don't trip the server's default
            # ``max_ping_strikes=2`` and surface as "Too many pings".
            ("grpc.keepalive_time_ms", 30_000),
            ("grpc.keepalive_timeout_ms", 10_000),
            ("grpc.keepalive_permit_without_calls", 1),
            ("grpc.http2.min_recv_ping_interval_without_data_ms", 10_000),
            ("grpc.http2.max_ping_strikes", 0),
        ],
    )
    agent_pb2_grpc.add_AgentServicer_to_server(
        CorlinmanAgentServicer(), server
    )
    try:
        server.add_insecure_port(bind)
        await server.start()
    except Exception as exc:
        log.warning("gateway.grpc.agent.bind_failed", bind=bind, error=str(exc))
        return

    # Auto-resume — same boot scan the standalone server runs, mirrored
    # here so a single-process co-hosted deployment also picks up
    # in-progress turns left by a crash. Best-effort; the agent path is
    # fully functional without it.
    try:
        from corlinman_server.main import _run_boot_auto_resume

        await _run_boot_auto_resume()
    except Exception as exc:  # noqa: BLE001 — never block boot
        log.warning("gateway.grpc.agent.resume_scan_failed", error=str(exc))

    log.info("gateway.grpc.agent.serving", bind=bind)
    try:
        await shutdown.wait()
    finally:
        with contextlib.suppress(Exception):
            await server.stop(grace=5.0)
        if bind.startswith("unix://"):
            with contextlib.suppress(FileNotFoundError, OSError):
                Path(bind[len("unix://") :]).unlink()
        log.info("gateway.grpc.agent.stopped", bind=bind)


def serve_agent_in_background(
    state: Any,
    cancel: asyncio.Event,
) -> asyncio.Task[None] | None:
    """Spawn the co-hosted ``Agent`` gRPC server as a background task.

    The gateway lifespan (``entrypoint.py``) registers the returned task
    in its ``background`` list and cancels + awaits it at shutdown under
    the shared ``cancel`` event.

    Returns ``None`` (spawns nothing) when the operator has not opted
    into in-process hosting — see :func:`agent_inproc_enabled`. In that
    case the gateway expects an external ``corlinman-python-server``.

    Signature matches the ``serve_placeholder_in_background`` /
    ``serve_*_in_background`` family the entrypoint already calls
    (``(state, cancel)``).
    """
    if not agent_inproc_enabled(state):
        log.info(
            "gateway.grpc.agent.inproc_disabled",
            detail=(
                "co-hosted agent off; gateway will dial an external "
                "corlinman-python-server (set CORLINMAN_GRPC_AGENT_INPROC=1 "
                "to co-host)"
            ),
        )
        return None

    bind = resolve_agent_bind(state)
    task = asyncio.create_task(
        serve_agent(bind, cancel),
        name="gateway.grpc.agent_server",
    )
    log.info("gateway.grpc.agent.inproc_spawned", bind=bind)
    return task
