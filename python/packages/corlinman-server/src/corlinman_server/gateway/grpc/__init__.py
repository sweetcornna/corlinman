"""``corlinman_server.gateway.grpc`` — Rust-hosted gRPC service surfaces.

Most corlinman gRPC services are owned by the Python plane (Agent /
Embedding / Vector / LLM on ``/tmp/corlinman-py.sock`` over
``grpc.aio``). This module is the **reverse direction**: services that
the Python gateway hosts so other clients — historically the Rust
gateway, now the in-process callers and the Python ``context_assembler``
— can dial against it without re-implementing the resolver registry.

Currently hosts:

* :mod:`corlinman_server.gateway.grpc.placeholder` — wraps the
  ``PlaceholderEngine`` so a Python client can expand
  ``{{namespace.name}}`` tokens without re-implementing the resolver
  registry. Ports :rust:`corlinman_gateway::grpc::placeholder`.
* :mod:`corlinman_server.gateway.grpc.agent_server` — Parcel **P4**:
  optionally co-hosts the real ``corlinman.v1.Agent`` service
  (:class:`corlinman_server.agent_servicer.CorlinmanAgentServicer`)
  inside the gateway process so a single-process deployment does not
  need a separate ``corlinman-python-server``. Off by default.

Why a Python-side placeholder gRPC server?
------------------------------------------
The Rust gateway used to host this so the Python
``context_assembler`` could dial in. After the gateway crate's slow
migration to Python the Python side now both produces and consumes
``Placeholder.Render`` calls — keeping the same on-the-wire contract
means any external tool (admin shell, future Rust subsystems, replay
harness) can still dial the same UDS and get the same answers.

The ``serve_*_in_background`` family
------------------------------------
The gateway lifespan (``entrypoint.py``) looks up an optional
``serve_placeholder_in_background(state, cancel)`` on this package and,
if present, spawns it as a background task cancelled at shutdown.
:func:`serve_placeholder_in_background` and the P4
:func:`serve_agent_in_background` both honour that ``(state, cancel)``
signature.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import structlog

from corlinman_server.gateway.grpc.agent_server import (
    GrpcAgentBindError,
    agent_inproc_enabled,
    resolve_agent_bind,
    serve_agent,
    serve_agent_in_background,
)
from corlinman_server.gateway.grpc.placeholder import (
    DEFAULT_RUST_SOCKET,
    ENV_RUST_SOCKET,
    PlaceholderService,
)
from corlinman_server.gateway.grpc.placeholder import (
    serve as serve_placeholder,
)
from corlinman_server.gateway.grpc.plugin_invoker import (
    DEFAULT_TOOL_TIMEOUT_MS,
    McpToolBridge,
    ServicePluginDispatcher,
    build_registry_invoker,
    invoke_mcp_plugin,
    invoke_service_plugin,
    invoke_sync_plugin,
)

_log = structlog.get_logger(__name__)

__all__ = [
    "DEFAULT_RUST_SOCKET",
    "DEFAULT_TOOL_TIMEOUT_MS",
    "ENV_RUST_SOCKET",
    "GrpcAgentBindError",
    "McpToolBridge",
    "PlaceholderService",
    "ServicePluginDispatcher",
    "agent_inproc_enabled",
    "build_registry_invoker",
    "invoke_mcp_plugin",
    "invoke_service_plugin",
    "invoke_sync_plugin",
    "resolve_agent_bind",
    "serve_agent",
    "serve_agent_in_background",
    "serve_placeholder",
    "serve_placeholder_in_background",
]


def serve_placeholder_in_background(
    state: Any,
    cancel: asyncio.Event,
) -> asyncio.Task[None] | None:
    """Spawn the ``Placeholder`` gRPC server as a background task.

    The gateway lifespan calls this if it exists (it previously did
    ``getattr(grpc_mod, "serve_placeholder_in_background", None)`` and
    found nothing — the placeholder server therefore never started).
    Parcel P4 lands the symbol so the seam resolves.

    Binds the ``Placeholder`` service onto ``$CORLINMAN_UDS_PATH``
    (default :data:`~corlinman_server.gateway.grpc.placeholder.\
DEFAULT_RUST_SOCKET`), wrapping :meth:`PlaceholderService.\
with_empty_engine` — every ``{{token}}`` round-trips through
    ``unresolved_keys`` until the real ``PlaceholderEngine`` port lands.

    Returns the spawned :class:`asyncio.Task` (registered + cancelled by
    the lifespan). A bind failure inside :func:`serve_placeholder` is
    non-fatal — it logs and the task completes early.
    """
    socket_path = os.environ.get(ENV_RUST_SOCKET, DEFAULT_RUST_SOCKET)
    service = PlaceholderService.with_empty_engine()
    task = asyncio.create_task(
        serve_placeholder(socket_path, service, cancel),
        name="gateway.grpc.placeholder_server",
    )
    _log.info("gateway.grpc.placeholder.spawned", socket=socket_path)
    return task
