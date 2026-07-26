from __future__ import annotations

from pathlib import Path

import pytest
from corlinman_server.standalone_app_state import (
    StandaloneAppState,
    build_standalone_app_state,
)


class _Closable:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


@pytest.mark.asyncio
async def test_standalone_app_state_closes_owned_handles_once() -> None:
    memory_host = _Closable()
    memory_kernel = _Closable()
    identity_resolver = _Closable()
    identity_store = object()
    state = StandaloneAppState(
        memory_host=memory_host,
        memory_kernel=memory_kernel,
        identity_store=identity_store,
        identity_resolver=identity_resolver,
    )

    await state.aclose()
    await state.aclose()

    assert memory_host.closed == 1
    assert memory_kernel.closed == 1
    assert identity_resolver.closed == 1
    assert state.memory_host is None
    assert state.memory_kernel is None
    assert state.identity_store is None
    assert state.identity_resolver is None


@pytest.mark.asyncio
async def test_standalone_app_state_opens_handles_under_execution_root(
    tmp_path: Path,
) -> None:
    execution_root = tmp_path / "execution"

    state = await build_standalone_app_state(execution_root)
    try:
        assert (execution_root / "memory.sqlite").exists()
        identity_files = list(execution_root.glob("*identity*.sqlite"))
        assert identity_files
    finally:
        await state.aclose()
