"""Shared pytest fixtures for the corlinman-server suite.

The autouse :func:`_isolate_data_dir` fixture points ``CORLINMAN_DATA_DIR``
at a per-test temp directory. Without it the agent servicer's automatic
conversation memory (``LocalSqliteHost`` opened under
``<data_dir>/memory.sqlite``) would fall back to the real
``~/.corlinman`` and leak state between tests — and between a developer's
test run and their actual deployment.

Tests that need a specific data dir still ``monkeypatch.setenv`` it
themselves; that call simply runs after this fixture and wins.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin ``CORLINMAN_DATA_DIR`` to a fresh temp dir for every test."""
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))


@pytest.fixture
def close_c2_handles():  # type: ignore[no-untyped-def]
    """Async closer for every sqlite handle ``_wire_c2_handles`` opens.

    A leaked aiosqlite connection parks a non-daemon worker thread that
    blocks interpreter exit AFTER the test summary — the historical
    "py-test hangs to the CI cap" failure mode. Any test invoking the C2
    wiring helper must close through this.
    """
    from contextlib import suppress
    from typing import Any

    async def _close(state: Any, app: Any) -> None:
        for handle in (
            getattr(state, "memory_host", None),
            getattr(state, "memory_kernel", None),
            getattr(state, "identity_store", None),
            getattr(getattr(app, "state", None), "corlinman_persona_state_store", None),
        ):
            if handle is None:
                continue
            with suppress(Exception):
                await handle.close()

    return _close
