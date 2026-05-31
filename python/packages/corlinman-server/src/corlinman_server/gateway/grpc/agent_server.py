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
    "GrpcAgentBindError",
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

#: Hosts the co-hosted agent is willing to bind to without an explicit
#: operator opt-in. The Agent gRPC has **no** TLS or auth on
#: ``add_insecure_port`` — a non-loopback bind exposes ReasoningLoop
#: (and its auto-bound ``run_shell`` / ``write_file`` / ``apply_patch``
#: tools) to the network. Default-deny everything else; the operator
#: can flip :data:`_ALLOW_PUBLIC_ENV` to opt in.
_SAFE_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})

#: Env var that lets an operator opt in to a non-loopback bind despite
#: the missing auth layer. Strict comparison against ``"1"`` — any other
#: value (``true``, ``yes``, empty) refuses the bind, on the
#: defence-in-depth principle that a typo must not silently open an
#: unauthenticated network surface.
_ALLOW_PUBLIC_ENV: str = "CORLINMAN_GRPC_AGENT_ALLOW_PUBLIC"


class GrpcAgentBindError(RuntimeError):
    """Raised when :func:`resolve_agent_bind` is asked to bind the
    co-hosted Agent gRPC to a non-loopback address without the operator
    explicitly opting in via ``$CORLINMAN_GRPC_AGENT_ALLOW_PUBLIC=1``.

    The Agent service is hosted on ``add_insecure_port`` — no TLS, no
    auth — so a non-loopback bind exposes :class:`~corlinman_agent.\
reasoning_loop.ReasoningLoop` (which auto-binds the shell / write-file
    / apply-patch tools) to anyone on the network. Refusing the bind by
    default keeps a misconfigured ``$CORLINMAN_PY_ADDR=0.0.0.0:50051``
    from silently becoming an unauthenticated RCE.
    """


def _extract_host(bind: str) -> str | None:
    """Return the host portion of a ``host:port`` (or IPv6
    ``[host]:port``) bind. Returns ``None`` if the bind has no host —
    e.g. a ``unix://`` UDS path, which is always loopback by nature.
    """
    if bind.startswith("unix://") or bind.startswith("unix:"):
        return None
    # IPv6 literal: ``[::1]:50051`` or ``[::]:50051``.
    if bind.startswith("["):
        end = bind.find("]")
        if end == -1:
            return bind  # malformed — let the caller flag it
        return bind[1:end]
    # IPv4 / hostname: ``host:port`` — take the part before the **last**
    # colon to be robust to schemes already stripped.
    if ":" in bind:
        return bind.rsplit(":", 1)[0]
    return bind


def _assert_safe_bind(bind: str) -> None:
    """Refuse a non-loopback bind unless the operator opted in.

    A ``unix://`` UDS path is always allowed — UDS access is mediated by
    filesystem perms, not the network. For TCP binds, the host must be
    in :data:`_SAFE_HOSTS`, **or** the operator must set
    ``$CORLINMAN_GRPC_AGENT_ALLOW_PUBLIC=1`` (in which case a warning is
    logged so the choice is visible in audit logs).
    """
    host = _extract_host(bind)
    if host is None or host in _SAFE_HOSTS:
        return
    if os.environ.get(_ALLOW_PUBLIC_ENV) == "1":
        log.warning(
            "grpc.agent.public_bind",
            bind=bind,
            host=host,
            detail=(
                "co-hosted Agent gRPC bound to non-loopback host with "
                "no TLS or auth (operator opted in via "
                f"{_ALLOW_PUBLIC_ENV}=1); ensure an upstream firewall / "
                "mTLS proxy fronts this socket"
            ),
        )
        return
    raise GrpcAgentBindError(
        f"refusing to bind co-hosted Agent gRPC to non-loopback host "
        f"{host!r} (resolved bind={bind!r}) — the service uses "
        "add_insecure_port (no TLS, no auth) and exposing it to the "
        "network is an unauthenticated-RCE risk. Bind to 127.0.0.1, "
        "::1, localhost, or a unix:// UDS path; or, if the deployment "
        "fronts the socket with an mTLS / firewalled proxy, opt in "
        f"explicitly with {_ALLOW_PUBLIC_ENV}=1."
    )


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

    **Safety gate (SEC-204):** the Agent gRPC is served on
    ``add_insecure_port`` (no TLS, no auth) so binding it to a
    non-loopback host exposes :class:`~corlinman_agent.reasoning_loop.\
ReasoningLoop` (and its auto-bound ``run_shell`` / ``write_file`` /
    ``apply_patch`` tools) to anyone on the network. This function
    refuses any bind whose host is not in :data:`_SAFE_HOSTS` (or a
    ``unix://`` UDS) and raises :class:`GrpcAgentBindError` with an
    actionable remediation message. To opt in (e.g. when an upstream
    mTLS proxy fronts the socket) set
    ``$CORLINMAN_GRPC_AGENT_ALLOW_PUBLIC=1``.
    """
    sock = os.environ.get("CORLINMAN_PY_SOCKET")
    if sock:
        bind = f"unix://{sock}"
    else:
        addr = os.environ.get("CORLINMAN_PY_ADDR")
        if addr:
            bind = addr
        else:
            port = os.environ.get("CORLINMAN_PY_PORT")
            if port:
                bind = f"127.0.0.1:{port}"
            else:
                bind = f"unix://{_DEFAULT_SOCKET}"
    _assert_safe_bind(bind)
    return bind


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


async def serve_agent(
    bind: str,
    shutdown: asyncio.Event,
    *,
    event_emitter: Any | None = None,
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
    # W1.3 — thread the gateway-wide observability emitter through to
    # the servicer so every ReasoningLoop it constructs tees envelopes
    # into the journal + live SSE subscribers. ``None`` keeps the
    # legacy yield-only path active for the test smoke / degraded boot.
    #
    # BUG-01: build + pass a pre-tool HookRunner so the co-hosted agent's
    # blocking PreToolDispatch gate is live (mirrors main._serve). Best-
    # effort — a build failure degrades to no runner, identical to before.
    hook_runner: Any | None = None
    try:
        from corlinman_server.main import _build_hook_runner

        hook_runner = _build_hook_runner()
    except Exception as exc:  # noqa: BLE001 — no hooks degrades fine
        log.warning("gateway.grpc.agent.hook_runner_failed", error=str(exc))
    agent_pb2_grpc.add_AgentServicer_to_server(
        CorlinmanAgentServicer(
            event_emitter=event_emitter, hook_runner=hook_runner
        ),
        server,
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
    # W1.3 — fetch the gateway-wide JournalBackedEmitter from AppState
    # so the in-process servicer gets the same fan-out target as the
    # SSE routes. Looked up via ``getattr`` so a stale boot path that
    # didn't open the journal still constructs the servicer cleanly.
    event_emitter = getattr(state, "event_emitter", None)
    if event_emitter is None:
        extras = getattr(state, "extras", None)
        if isinstance(extras, dict):
            event_emitter = extras.get("event_emitter")
    task = asyncio.create_task(
        serve_agent(bind, cancel, event_emitter=event_emitter),
        name="gateway.grpc.agent_server",
    )
    log.info(
        "gateway.grpc.agent.inproc_spawned",
        bind=bind,
        event_emitter_wired=event_emitter is not None,
    )
    return task
