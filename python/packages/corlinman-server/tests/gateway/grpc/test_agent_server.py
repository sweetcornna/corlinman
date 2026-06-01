"""Parcel **P4** — gRPC Agent backend wiring tests.

Covers the two P4 deliverables:

* ``gateway.services.grpc_backend`` — deployment-mode selection,
  endpoint resolution, and the :class:`GrpcAgentChatBackend`-backed
  :class:`ChatService` builder.
* ``gateway.grpc.agent_server`` — the opt-in in-process ``Agent`` gRPC
  server and its ``serve_*_in_background`` seam helper.

The end-to-end test boots the **real**
:class:`corlinman_server.agent_servicer.CorlinmanAgentServicer` over the
co-hosted server and drives a chat turn through
:class:`GrpcAgentChatBackend` → :class:`ChatService` — proving the full
agent path is wired and runnable.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest
from corlinman_server.gateway.grpc import agent_server
from corlinman_server.gateway.services import grpc_backend


class _State:
    """Minimal AppState stand-in — just a ``config`` dict."""

    def __init__(self, config: dict | None = None) -> None:
        self.config = config
        self.chat = None


# ─── chat_backend_mode ───────────────────────────────────────────────


def test_mode_defaults_to_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORLINMAN_CHAT_BACKEND", raising=False)
    assert grpc_backend.chat_backend_mode(_State()) == "direct"
    assert grpc_backend.chat_backend_mode(_State({})) == "direct"


def test_mode_from_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORLINMAN_CHAT_BACKEND", raising=False)
    state = _State({"models": {"backend": "grpc_agent"}})
    assert grpc_backend.chat_backend_mode(state) == "grpc_agent"


def test_mode_env_overrides_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORLINMAN_CHAT_BACKEND", "direct")
    state = _State({"models": {"backend": "grpc_agent"}})
    assert grpc_backend.chat_backend_mode(state) == "direct"


def test_mode_unknown_value_falls_back_to_direct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CORLINMAN_CHAT_BACKEND", raising=False)
    state = _State({"models": {"backend": "nonsense"}})
    assert grpc_backend.chat_backend_mode(state) == "direct"


# ─── resolve_agent_target ────────────────────────────────────────────


def test_target_default(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("CORLINMAN_PY_SOCKET", "CORLINMAN_PY_ADDR", "CORLINMAN_PY_PORT"):
        monkeypatch.delenv(var, raising=False)
    assert grpc_backend.resolve_agent_target(None) == "127.0.0.1:50051"


def test_target_config_endpoint_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORLINMAN_PY_ADDR", "1.2.3.4:9999")
    state = _State({"agent": {"endpoint": "10.0.0.1:7000"}})
    assert grpc_backend.resolve_agent_target(state) == "10.0.0.1:7000"


def test_target_env_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("CORLINMAN_PY_ADDR", "CORLINMAN_PY_PORT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CORLINMAN_PY_SOCKET", "/tmp/x.sock")
    assert grpc_backend.resolve_agent_target(_State()) == "unix:/tmp/x.sock"


# ─── build_grpc_chat_service ─────────────────────────────────────────


async def test_build_grpc_chat_service_returns_service() -> None:
    """A lazily-connecting channel means the builder succeeds even with
    no agent running — the failure surfaces later, per the contract.

    Async so a running event loop is present: ``build_grpc_chat_service``
    opens a ``grpc.aio`` channel, which binds to the running loop — the
    same condition as the production async lifespan. A sync test would
    instead depend on the polluter-prone global loop."""
    service = grpc_backend.build_grpc_chat_service(_State())
    assert service is not None
    assert hasattr(service, "run")


async def test_build_chat_service_grpc_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORLINMAN_CHAT_BACKEND", "grpc_agent")
    service = grpc_backend.build_chat_service(_State())
    assert service is not None
    # The backend behind it must be the gRPC one, not the direct path.
    from corlinman_server.gateway.services.chat_service import (
        GrpcAgentChatBackend,
    )

    assert isinstance(service._backend, GrpcAgentChatBackend)


# ─── resolve_agent_bind safety (SEC-204) ─────────────────────────────
#
# Defence-in-depth against an unauth-RCE footgun: ``serve_agent`` ends
# in ``server.add_insecure_port(bind)`` with **no** TLS or auth. If an
# operator co-hosts the agent AND points ``CORLINMAN_PY_ADDR`` (or
# ``CORLINMAN_PY_PORT`` on a host bound to a public NIC) at a non-
# loopback address, anyone on the network can dial the Agent gRPC and
# issue ReasoningLoop chat turns — which auto-bind ``run_shell`` /
# ``write_file`` / ``apply_patch`` → unauth RCE.
#
# Policy: ``resolve_agent_bind`` refuses any bind whose host is not in
# ``{127.0.0.1, ::1, localhost}`` (or a ``unix://`` UDS path). The
# operator can explicitly opt in via
# ``CORLINMAN_GRPC_AGENT_ALLOW_PUBLIC=1`` to permit a non-loopback
# bind — and a warning is logged when they do.


@pytest.fixture
def _clear_bind_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "CORLINMAN_PY_SOCKET",
        "CORLINMAN_PY_ADDR",
        "CORLINMAN_PY_PORT",
        "CORLINMAN_GRPC_AGENT_ALLOW_PUBLIC",
    ):
        monkeypatch.delenv(var, raising=False)


def test_resolve_agent_bind_accepts_loopback_ipv4(
    monkeypatch: pytest.MonkeyPatch, _clear_bind_env: None
) -> None:
    monkeypatch.setenv("CORLINMAN_PY_ADDR", "127.0.0.1:50051")
    assert agent_server.resolve_agent_bind(None) == "127.0.0.1:50051"


def test_resolve_agent_bind_accepts_loopback_ipv6(
    monkeypatch: pytest.MonkeyPatch, _clear_bind_env: None
) -> None:
    monkeypatch.setenv("CORLINMAN_PY_ADDR", "[::1]:50051")
    assert agent_server.resolve_agent_bind(None) == "[::1]:50051"


def test_resolve_agent_bind_accepts_localhost(
    monkeypatch: pytest.MonkeyPatch, _clear_bind_env: None
) -> None:
    monkeypatch.setenv("CORLINMAN_PY_ADDR", "localhost:50051")
    assert agent_server.resolve_agent_bind(None) == "localhost:50051"


def test_resolve_agent_bind_accepts_unix_socket(
    monkeypatch: pytest.MonkeyPatch, _clear_bind_env: None
) -> None:
    monkeypatch.setenv("CORLINMAN_PY_SOCKET", "/tmp/agent.sock")
    assert agent_server.resolve_agent_bind(None) == "unix:///tmp/agent.sock"


def test_resolve_agent_bind_accepts_py_port_default_loopback(
    monkeypatch: pytest.MonkeyPatch, _clear_bind_env: None
) -> None:
    """CORLINMAN_PY_PORT always renders 127.0.0.1:<port> — must stay safe."""
    monkeypatch.setenv("CORLINMAN_PY_PORT", "55555")
    assert agent_server.resolve_agent_bind(None) == "127.0.0.1:55555"


def test_resolve_agent_bind_default_is_unix_socket(
    monkeypatch: pytest.MonkeyPatch, _clear_bind_env: None
) -> None:
    """No env vars → default UDS path; must continue to work unchanged."""
    bind = agent_server.resolve_agent_bind(None)
    assert bind.startswith("unix://")


def test_resolve_agent_bind_rejects_wildcard_v4(
    monkeypatch: pytest.MonkeyPatch, _clear_bind_env: None
) -> None:
    monkeypatch.setenv("CORLINMAN_PY_ADDR", "0.0.0.0:50051")
    with pytest.raises(agent_server.GrpcAgentBindError) as exc_info:
        agent_server.resolve_agent_bind(None)
    # Error must name the override env var so the operator knows what
    # to flip if they really want a public bind.
    assert "CORLINMAN_GRPC_AGENT_ALLOW_PUBLIC" in str(exc_info.value)


def test_resolve_agent_bind_rejects_wildcard_v6(
    monkeypatch: pytest.MonkeyPatch, _clear_bind_env: None
) -> None:
    monkeypatch.setenv("CORLINMAN_PY_ADDR", "[::]:50051")
    with pytest.raises(agent_server.GrpcAgentBindError):
        agent_server.resolve_agent_bind(None)


def test_resolve_agent_bind_rejects_private_lan(
    monkeypatch: pytest.MonkeyPatch, _clear_bind_env: None
) -> None:
    monkeypatch.setenv("CORLINMAN_PY_ADDR", "192.168.1.5:50051")
    with pytest.raises(agent_server.GrpcAgentBindError):
        agent_server.resolve_agent_bind(None)


def test_resolve_agent_bind_rejects_public_ip(
    monkeypatch: pytest.MonkeyPatch, _clear_bind_env: None
) -> None:
    monkeypatch.setenv("CORLINMAN_PY_ADDR", "8.8.8.8:50051")
    with pytest.raises(agent_server.GrpcAgentBindError):
        agent_server.resolve_agent_bind(None)


def test_resolve_agent_bind_accepts_public_with_allow_flag(
    monkeypatch: pytest.MonkeyPatch, _clear_bind_env: None
) -> None:
    monkeypatch.setenv("CORLINMAN_PY_ADDR", "0.0.0.0:50051")
    monkeypatch.setenv("CORLINMAN_GRPC_AGENT_ALLOW_PUBLIC", "1")
    assert agent_server.resolve_agent_bind(None) == "0.0.0.0:50051"


def test_resolve_agent_bind_allow_flag_logs_warning(
    monkeypatch: pytest.MonkeyPatch,
    _clear_bind_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The opt-in opens an unauthenticated network surface — log it.

    structlog's default ConsoleRenderer writes to stdout/stderr (not the
    stdlib ``logging`` framework), so we assert against the captured
    stream rather than ``caplog``.
    """
    monkeypatch.setenv("CORLINMAN_PY_ADDR", "0.0.0.0:50051")
    monkeypatch.setenv("CORLINMAN_GRPC_AGENT_ALLOW_PUBLIC", "1")
    agent_server.resolve_agent_bind(None)
    captured = capsys.readouterr()
    rendered = captured.out + captured.err
    assert "grpc.agent.public_bind" in rendered, rendered
    assert "warning" in rendered.lower(), rendered


def test_resolve_agent_bind_allow_flag_only_accepts_one(
    monkeypatch: pytest.MonkeyPatch, _clear_bind_env: None
) -> None:
    """A non-``1`` value must NOT open the door — be strict, not permissive."""
    monkeypatch.setenv("CORLINMAN_PY_ADDR", "0.0.0.0:50051")
    monkeypatch.setenv("CORLINMAN_GRPC_AGENT_ALLOW_PUBLIC", "true")
    with pytest.raises(agent_server.GrpcAgentBindError):
        agent_server.resolve_agent_bind(None)


# ─── agent_server gate ───────────────────────────────────────────────


def test_inproc_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORLINMAN_GRPC_AGENT_INPROC", raising=False)
    assert agent_server.agent_inproc_enabled(None) is False
    assert agent_server.agent_inproc_enabled(_State()) is False


def test_inproc_enabled_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORLINMAN_GRPC_AGENT_INPROC", "1")
    assert agent_server.agent_inproc_enabled(None) is True


def test_inproc_enabled_via_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORLINMAN_GRPC_AGENT_INPROC", raising=False)
    state = _State({"agent": {"in_process": True}})
    assert agent_server.agent_inproc_enabled(state) is True


def test_serve_in_background_returns_none_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CORLINMAN_GRPC_AGENT_INPROC", raising=False)
    cancel = asyncio.Event()
    assert agent_server.serve_agent_in_background(_State(), cancel) is None


@pytest.mark.asyncio
async def test_serve_in_background_threads_subagent_config(
    monkeypatch: pytest.MonkeyPatch,
    _clear_bind_env: None,
) -> None:
    monkeypatch.delenv("CORLINMAN_GRPC_AGENT_INPROC", raising=False)
    monkeypatch.setenv("CORLINMAN_PY_PORT", "55556")
    seen: dict[str, object] = {}

    async def _fake_serve_agent(
        bind: str,
        shutdown: asyncio.Event,
        *,
        event_emitter: object | None = None,
        subagent_dispatcher: object | None = None,
        subagent_config: dict | None = None,
    ) -> None:
        seen["bind"] = bind
        seen["event_emitter"] = event_emitter
        seen["subagent_dispatcher"] = subagent_dispatcher
        seen["subagent_config"] = subagent_config

    monkeypatch.setattr(agent_server, "serve_agent", _fake_serve_agent)
    cancel = asyncio.Event()
    state = _State(
        {
            "agent": {"in_process": True},
            "subagent": {
                "max_concurrent_per_parent": 2,
                "max_concurrent_per_tenant": 4,
                "max_depth": 3,
                "max_wall_seconds_ceiling": 120,
            },
        }
    )

    task = agent_server.serve_agent_in_background(state, cancel)
    assert task is not None
    await task

    assert seen["bind"] == "127.0.0.1:55556"
    assert seen["subagent_config"] == {
        "max_concurrent_per_parent": 2,
        "max_concurrent_per_tenant": 4,
        "max_depth": 3,
        "max_wall_seconds_ceiling": 120,
    }


# ─── end-to-end: co-hosted server + GrpcAgentChatBackend ─────────────


@pytest.mark.asyncio
async def test_inproc_agent_serves_chat_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boot the real Agent servicer over a UDS, dial it through
    ``GrpcAgentChatBackend`` + ``ChatService``, and assert a full turn."""
    monkeypatch.setenv(
        "CORLINMAN_TEST_MOCK_PROVIDER", "co-hosted agent reply"
    )
    sock = os.path.join(tempfile.mkdtemp(), "p4-agent.sock")
    cancel = asyncio.Event()

    server_task = asyncio.create_task(
        agent_server.serve_agent(f"unix://{sock}", cancel)
    )
    # Wait for the socket file to appear (server bound).
    for _ in range(100):
        if Path(sock).exists():
            break
        await asyncio.sleep(0.02)
    assert Path(sock).exists(), "agent server never bound its UDS"

    from corlinman_grpc.agent_client import AgentClient, connect_channel
    from corlinman_server.gateway.services.chat_service import (
        ChatService,
        GrpcAgentChatBackend,
    )
    from corlinman_server.gateway_api import (
        InternalChatRequest,
        Message,
        Role,
    )

    client = AgentClient(connect_channel(f"unix:{sock}"))
    service = ChatService(GrpcAgentChatBackend(client))

    req = InternalChatRequest(
        model="mock-model",
        messages=[Message(role=Role.USER, content="hi")],
        session_key="p4-test::1",
        stream=True,
    )
    events = [
        type(ev).__name__
        async for ev in service.run(req, asyncio.Event())
    ]

    await client.close()
    cancel.set()
    await asyncio.wait_for(server_task, timeout=10.0)

    assert "TokenDeltaEvent" in events, events
    assert "DoneEvent" in events, events
