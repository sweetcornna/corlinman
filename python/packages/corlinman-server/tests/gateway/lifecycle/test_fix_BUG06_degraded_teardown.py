"""BUG-06 — unguarded ``state.extras`` aborts degraded-mode teardown.

When ``gateway.core`` is unavailable (a partial port / degraded boot)
:func:`_build_state` falls back to :class:`_DegradedAppState`, whose
``__slots__`` are ``("config", "data_dir")`` — it has no ``extras`` dict.

The lifespan-exit ``finally`` historically read
``state.extras.get("mcp_manager")`` *unguarded*. On a degraded boot that
raised ``AttributeError`` from inside ``finally``, which propagated out
and aborted every subsequent teardown step — leaking the C2 sqlite
stores (identity / persona-state) that ``_wire_c2_handles`` opened onto
``app.state``.

This test forces the degraded path (so the bug's exact trigger fires),
drives the real lifespan to completion, and asserts that shutdown
finishes cleanly AND the C2 stores were closed + their slots cleared.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from corlinman_server.gateway.lifecycle import entrypoint as ep
from corlinman_server.gateway.lifecycle.entrypoint import (
    _DegradedAppState,
    build_app,
)


class _async_lifespan:  # noqa: N801 — context-manager helper
    """Drive a FastAPI ``lifespan`` context directly on the current loop.

    Mirrors the helper in ``test_evolution_wiring.py``: TestClient runs
    the lifespan on a background thread, so to make async assertions
    against the same handles we open the context ourselves.
    """

    def __init__(self, app):
        self._app = app
        self._ctx = None

    async def __aenter__(self):
        self._ctx = self._app.router.lifespan_context(self._app)
        await self._ctx.__aenter__()
        return self._app

    async def __aexit__(self, exc_type, exc, tb):
        assert self._ctx is not None
        return await self._ctx.__aexit__(exc_type, exc, tb)


@pytest.mark.asyncio
async def test_degraded_boot_teardown_completes_and_closes_c2_stores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boot with ``_DegradedAppState`` (valid data_dir so C2 stores open)
    then exit the lifespan. Before the fix the ``finally`` raised
    ``AttributeError`` on ``state.extras`` and aborted cleanup, leaking
    the identity + persona-state stores. After the fix the shutdown
    completes and both stores are closed + their slots cleared."""
    real_build_state = ep._build_state

    captured: dict[str, object] = {}

    def _force_degraded(cfg, data_dir):
        # Ignore the real AppState builder — return the degraded stand-in
        # so the lifespan exercises the slotted-state teardown path.
        state = _DegradedAppState(config=cfg, data_dir=data_dir)
        captured["state"] = state
        return state

    monkeypatch.setattr(ep, "_build_state", _force_degraded)

    app = build_app(config_path=None, data_dir=tmp_path)

    # Sanity: the lifespan should be driving the degraded stand-in.
    state = captured.get("state")
    assert isinstance(state, _DegradedAppState)
    assert not hasattr(state, "extras"), (
        "_DegradedAppState must NOT have extras — that's the bug trigger"
    )

    async with _async_lifespan(app):
        # Runtime sqlite stores open onto app.state during startup even in
        # degraded mode (they're published on the FastAPI State, not the
        # slotted AppState). The evolution + scheduler stores both open and
        # are closed only by teardown steps that run AFTER the buggy
        # ``state.extras`` read — so they're the ones that leak when the
        # ``finally`` aborts early. Confirm they opened (acceptance precond).
        evo_store = getattr(app.state, "_evolution_store", None)
        sched_store = getattr(app.state, "scheduler_store", None)
        assert evo_store is not None, (
            "evolution store should open during degraded boot"
        )
        assert sched_store is not None, (
            "scheduler store should open during degraded boot"
        )

    # Exiting the lifespan ran the teardown. Before the fix this raised
    # AttributeError out of __aexit__; now it completes and the runtime
    # store slots are cleared (proving close ran rather than being aborted
    # at the unguarded ``state.extras`` read).
    assert getattr(app.state, "_evolution_store", None) is None, (
        "evolution store slot must be cleared by a completed teardown"
    )
    assert getattr(app.state, "scheduler_store", None) is None, (
        "scheduler store slot must be cleared by a completed teardown"
    )

    # restore (monkeypatch auto-undoes, but be explicit for clarity)
    assert ep._build_state is _force_degraded
    monkeypatch.setattr(ep, "_build_state", real_build_state)
