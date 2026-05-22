"""Tests for ``gateway.services.grpc_backend`` — P4 wiring helpers.

Covers:
* :func:`chat_backend_mode` — env var / config / default precedence
* :func:`resolve_agent_target` — endpoint resolution from config + env

Does NOT test ``build_grpc_chat_service`` / ``GrpcAgentChatBackend``
(those require a running gRPC agent — another parcel owns them) or
``grpc/`` internals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from corlinman_server.gateway.services.grpc_backend import (
    DEFAULT_AGENT_ENDPOINT,
    chat_backend_mode,
    resolve_agent_target,
)


# ---------------------------------------------------------------------------
# Minimal state stand-in
# ---------------------------------------------------------------------------


@dataclass
class _State:
    config: Any = None


# ---------------------------------------------------------------------------
# chat_backend_mode — precedence
# ---------------------------------------------------------------------------


def test_mode_defaults_to_direct() -> None:
    state = _State(config={})
    assert chat_backend_mode(state) == "direct"


def test_mode_none_config_defaults_to_direct() -> None:
    assert chat_backend_mode(_State(config=None)) == "direct"


def test_mode_config_direct() -> None:
    state = _State(config={"models": {"backend": "direct"}})
    assert chat_backend_mode(state) == "direct"


def test_mode_config_grpc_agent() -> None:
    state = _State(config={"models": {"backend": "grpc_agent"}})
    assert chat_backend_mode(state) == "grpc_agent"


def test_mode_config_unknown_falls_back_to_direct() -> None:
    state = _State(config={"models": {"backend": "unknown_mode"}})
    assert chat_backend_mode(state) == "direct"


def test_mode_env_var_direct(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORLINMAN_CHAT_BACKEND", "direct")
    # Config says grpc_agent but env wins
    state = _State(config={"models": {"backend": "grpc_agent"}})
    assert chat_backend_mode(state) == "direct"


def test_mode_env_var_grpc_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORLINMAN_CHAT_BACKEND", "grpc_agent")
    state = _State(config={})
    assert chat_backend_mode(state) == "grpc_agent"


def test_mode_env_var_unknown_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORLINMAN_CHAT_BACKEND", "whatever")
    assert chat_backend_mode(_State()) == "direct"


def test_mode_env_var_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORLINMAN_CHAT_BACKEND", "GRPC_AGENT")
    assert chat_backend_mode(_State()) == "grpc_agent"


def test_mode_env_var_beats_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env var has higher precedence than config knob."""
    monkeypatch.setenv("CORLINMAN_CHAT_BACKEND", "grpc_agent")
    state = _State(config={"models": {"backend": "direct"}})
    assert chat_backend_mode(state) == "grpc_agent"


# ---------------------------------------------------------------------------
# resolve_agent_target — endpoint precedence
# ---------------------------------------------------------------------------


def test_resolve_default_when_nothing_set(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("CORLINMAN_PY_SOCKET", "CORLINMAN_PY_ADDR", "CORLINMAN_PY_PORT"):
        monkeypatch.delenv(var, raising=False)
    result = resolve_agent_target(None)
    assert result == DEFAULT_AGENT_ENDPOINT


def test_resolve_corlinman_py_addr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORLINMAN_PY_SOCKET", raising=False)
    monkeypatch.setenv("CORLINMAN_PY_ADDR", "192.168.1.5:60000")
    monkeypatch.delenv("CORLINMAN_PY_PORT", raising=False)
    assert resolve_agent_target(None) == "192.168.1.5:60000"


def test_resolve_corlinman_py_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORLINMAN_PY_SOCKET", raising=False)
    monkeypatch.delenv("CORLINMAN_PY_ADDR", raising=False)
    monkeypatch.setenv("CORLINMAN_PY_PORT", "55555")
    assert resolve_agent_target(None) == "127.0.0.1:55555"


def test_resolve_corlinman_py_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORLINMAN_PY_SOCKET", "/tmp/agent.sock")
    assert resolve_agent_target(None) == "unix:/tmp/agent.sock"


def test_resolve_socket_beats_addr(monkeypatch: pytest.MonkeyPatch) -> None:
    """UDS socket has higher precedence than ADDR."""
    monkeypatch.setenv("CORLINMAN_PY_SOCKET", "/run/agent.sock")
    monkeypatch.setenv("CORLINMAN_PY_ADDR", "127.0.0.1:9999")
    assert resolve_agent_target(None).startswith("unix:")


def test_resolve_config_endpoint_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Declarative ``config[agent][endpoint]`` has top priority."""
    monkeypatch.setenv("CORLINMAN_PY_ADDR", "127.0.0.1:9999")
    state = _State(config={"agent": {"endpoint": "my-agent:7777"}})
    assert resolve_agent_target(state) == "my-agent:7777"


def test_resolve_no_state_falls_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORLINMAN_PY_SOCKET", raising=False)
    monkeypatch.setenv("CORLINMAN_PY_ADDR", "10.0.0.1:50051")
    monkeypatch.delenv("CORLINMAN_PY_PORT", raising=False)
    assert resolve_agent_target(None) == "10.0.0.1:50051"


def test_resolve_none_state_uses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("CORLINMAN_PY_SOCKET", "CORLINMAN_PY_ADDR", "CORLINMAN_PY_PORT"):
        monkeypatch.delenv(var, raising=False)
    assert resolve_agent_target(None) == DEFAULT_AGENT_ENDPOINT
