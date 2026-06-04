"""R2 persona-liveness — scheduler ``persona.decay`` builtin contract tests.

Two surfaces:

* Registry — importing the builtins package registers ``persona.decay``
  so the scheduler-runtime ``run_tool`` dispatch resolves it by name.
* Action — running the builtin against a real ``agent_state.sqlite``
  sweeps decay across every row: a ``"tired"`` row with high fatigue and
  a stale ``updated_at_ms`` trends back toward ``"neutral"`` / lower
  fatigue after the sweep (the whole point — without the scheduled sweep
  mood/fatigue stay frozen on every install).

Plus the typed-envelope degradation branches the scheduler tick loop
relies on (``data_dir_unavailable`` — never raise out of a builtin).
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from corlinman_persona.state import PersonaState
from corlinman_persona.store import PersonaStore
from corlinman_server.scheduler.builtins import (
    BUILTIN_ACTIONS,
    PERSONA_DECAY_BUILTIN_NAME,
    BuiltinContext,
    _persona_decay_action,
    run_builtin,
)

# ---------------------------------------------------------------------------
# Registry surface
# ---------------------------------------------------------------------------


def test_persona_decay_is_registered_by_name() -> None:
    """Importing the builtins module registers ``persona.decay`` so the
    scheduler-runtime hook can resolve it without re-importing every
    callsite."""
    assert PERSONA_DECAY_BUILTIN_NAME == "persona.decay"
    assert PERSONA_DECAY_BUILTIN_NAME in BUILTIN_ACTIONS
    assert BUILTIN_ACTIONS[PERSONA_DECAY_BUILTIN_NAME] is _persona_decay_action


# ---------------------------------------------------------------------------
# Degradation branches — builtins must NEVER raise.
# ---------------------------------------------------------------------------


async def test_data_dir_unavailable_returns_typed_envelope() -> None:
    """``app_state`` with no ``data_dir`` slot → typed envelope, no raise."""
    context = BuiltinContext(app_state=SimpleNamespace())
    out = await _persona_decay_action(context)
    assert out == {"ok": False, "reason": "data_dir_unavailable"}


async def test_none_app_state_returns_data_dir_unavailable() -> None:
    """Degraded boot before the state bundle attaches — same short-circuit."""
    context = BuiltinContext(app_state=None)
    out = await _persona_decay_action(context)
    assert out == {"ok": False, "reason": "data_dir_unavailable"}


# ---------------------------------------------------------------------------
# Happy path — the sweep actually applies decay.
# ---------------------------------------------------------------------------


async def test_sweep_drifts_tired_high_fatigue_toward_baseline(tmp_path) -> None:
    """A ``"tired"`` row with high fatigue and a stale timestamp trends
    toward ``"neutral"`` / lower fatigue after the builtin sweep.

    Seeds the row two full days in the past so the per-row elapsed-hours
    math (now - updated_at) drives enough fatigue recovery to cross the
    ``tired_to_neutral_below`` threshold and flip the mood label back.
    """
    state_db = tmp_path / "agent_state.sqlite"
    two_days_ago_ms = int(time.time() * 1000) - 48 * 3_600_000

    async with PersonaStore(state_db) as store:
        await store.upsert(
            PersonaState(
                agent_id="grantley",
                mood="tired",
                fatigue=0.9,
                recent_topics=["a", "b"],
                updated_at_ms=two_days_ago_ms,
            )
        )

    context = BuiltinContext(app_state=SimpleNamespace(data_dir=tmp_path))
    out = await _persona_decay_action(context)

    assert out["ok"] is True
    assert out["rows_scanned"] == 1
    assert out["rows_changed"] == 1

    async with PersonaStore(state_db) as store:
        after = await store.get("grantley")
    assert after is not None
    # Fatigue recovered toward baseline ...
    assert after.fatigue < 0.9
    # ... far enough that the "tired" label flipped back to "neutral".
    assert after.mood == "neutral"


async def test_run_builtin_dispatches_persona_decay(tmp_path) -> None:
    """End-to-end via the public ``run_builtin`` entry point — the
    registry indirection is transparent and the sweep still runs."""
    state_db = tmp_path / "agent_state.sqlite"
    one_hour_ago_ms = int(time.time() * 1000) - 3_600_000

    async with PersonaStore(state_db) as store:
        await store.upsert(
            PersonaState(
                agent_id="grantley",
                mood="neutral",
                fatigue=0.5,
                updated_at_ms=one_hour_ago_ms,
            )
        )

    context = BuiltinContext(app_state=SimpleNamespace(data_dir=tmp_path))
    out = await run_builtin(PERSONA_DECAY_BUILTIN_NAME, context)

    assert out["ok"] is True
    assert out["rows_scanned"] == 1

    async with PersonaStore(state_db) as store:
        after = await store.get("grantley")
    assert after is not None
    # One hour at the 0.1/hr recovery rate → 0.5 - 0.1 = 0.4.
    assert after.fatigue < 0.5
