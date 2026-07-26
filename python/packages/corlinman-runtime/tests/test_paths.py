from __future__ import annotations

from pathlib import Path

from corlinman_runtime import (
    resolve_agent_workspace,
    resolve_data_dir,
    resolve_execution_state_dir,
)


def test_data_dir_precedence() -> None:
    env = {"CORLINMAN_DATA_DIR": "/env-data"}

    assert resolve_data_dir("/explicit", configured="/configured", environ=env) == Path(
        "/explicit"
    )
    assert resolve_data_dir(configured="/configured", environ=env) == Path("/env-data")
    assert resolve_data_dir(configured="/configured", environ={}) == Path("/configured")


def test_execution_state_dir_preserves_flat_layout_by_default() -> None:
    assert resolve_execution_state_dir(data_dir="/control", environ={}) == Path(
        "/control"
    )


def test_execution_state_dir_override_is_independent() -> None:
    env = {
        "CORLINMAN_DATA_DIR": "/control",
        "CORLINMAN_EXECUTION_STATE_DIR": "/execution",
    }

    assert resolve_data_dir(environ=env) == Path("/control")
    assert resolve_execution_state_dir(environ=env) == Path("/execution")


def test_agent_workspace_uses_execution_state_root() -> None:
    env = {
        "CORLINMAN_DATA_DIR": "/control",
        "CORLINMAN_EXECUTION_STATE_DIR": "/execution",
    }

    assert resolve_agent_workspace(environ=env) == Path("/execution/workspace")
    assert resolve_agent_workspace(
        environ={**env, "CORLINMAN_AGENT_WORKSPACE": "/workspace"}
    ) == Path("/workspace")
