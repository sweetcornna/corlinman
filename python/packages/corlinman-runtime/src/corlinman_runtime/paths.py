"""Pure path resolution for control-plane and execution-state storage."""

from __future__ import annotations

import os
from collections.abc import Mapping
from os import PathLike
from pathlib import Path

DATA_DIR_ENV = "CORLINMAN_DATA_DIR"
EXECUTION_STATE_DIR_ENV = "CORLINMAN_EXECUTION_STATE_DIR"
AGENT_WORKSPACE_ENV = "CORLINMAN_AGENT_WORKSPACE"

PathInput = str | PathLike[str]


def resolve_data_dir(
    explicit: PathInput | None = None,
    *,
    configured: PathInput | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the gateway-private control-plane data directory.

    Precedence is explicit argument, ``CORLINMAN_DATA_DIR``, configured value,
    then ``~/.corlinman`` with a relative fallback when the home directory is
    unavailable. The function is pure and never creates or canonicalizes paths.
    """
    if explicit is not None and str(explicit).strip():
        return Path(explicit)
    env = os.environ if environ is None else environ
    configured_env = env.get(DATA_DIR_ENV, "").strip()
    if configured_env:
        return Path(configured_env)
    if configured is not None and str(configured).strip():
        return Path(configured)
    try:
        return Path.home() / ".corlinman"
    except (OSError, RuntimeError):
        return Path(".corlinman")


def resolve_execution_state_dir(
    explicit: PathInput | None = None,
    *,
    data_dir: PathInput | None = None,
    configured_data_dir: PathInput | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve state shared by the gateway and model execution process.

    ``CORLINMAN_EXECUTION_STATE_DIR`` is the split-process override. Falling
    back to the ordinary data directory preserves the historical flat layout
    for combined deployments and installations that have not opted into the
    isolated execution-state mount.
    """
    if explicit is not None and str(explicit).strip():
        return Path(explicit)
    env = os.environ if environ is None else environ
    execution_env = env.get(EXECUTION_STATE_DIR_ENV, "").strip()
    if execution_env:
        return Path(execution_env)
    if data_dir is not None and str(data_dir).strip():
        return Path(data_dir)
    return resolve_data_dir(
        configured=configured_data_dir,
        environ=env,
    )


def resolve_agent_workspace(
    explicit: PathInput | None = None,
    *,
    execution_state_dir: PathInput | None = None,
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Resolve the model-controlled workspace directory."""
    if explicit is not None and str(explicit).strip():
        return Path(explicit)
    env = os.environ if environ is None else environ
    workspace_env = env.get(AGENT_WORKSPACE_ENV, "").strip()
    if workspace_env:
        return Path(workspace_env)
    return (
        resolve_execution_state_dir(
            data_dir=execution_state_dir,
            environ=env,
        )
        / "workspace"
    )
