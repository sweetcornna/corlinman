"""Boot-seed of the built-in grantley persona-STATE row (root cause R1).

The :class:`~corlinman_persona.PersonaResolver` reads ``{{persona.*}}`` off
the ``agent_state.sqlite`` row keyed ``(tenant_id="default",
agent_id="grantley")``. ``_wire_c2_handles`` opens that store at boot and now
calls :func:`_seed_builtin_persona_state` to upsert a defaults-only row when
absent — so placeholders resolve to real values instead of ``""``.

These exercise the seeder helper directly + through the wiring spine:

* the grantley row exists after the seeding step (and the resolver reads it);
* a pre-existing row is **never** clobbered (insert-if-absent semantics,
  mirroring :func:`corlinman_persona.seeder.seed_from_card`).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from corlinman_persona import PersonaState
from corlinman_persona.store import PersonaStore
from corlinman_server.gateway.core.state import AppState
from corlinman_server.gateway.lifecycle.c2_wiring import (
    _seed_builtin_persona_state,
)
from corlinman_server.persona.default_grantley import DEFAULT_GRANTLEY_ID


async def test_seed_creates_grantley_state_row(tmp_path: Path) -> None:
    """A fresh agent_state.sqlite gains a default-shaped grantley row."""
    store = await PersonaStore.open_or_create(tmp_path / "agent_state.sqlite")
    try:
        assert await store.get(DEFAULT_GRANTLEY_ID) is None

        await _seed_builtin_persona_state(store)

        row = await store.get(DEFAULT_GRANTLEY_ID)
        assert row is not None
        assert row.agent_id == DEFAULT_GRANTLEY_ID
        assert row.mood == "neutral"
        assert row.fatigue == 0.0
        assert row.recent_topics == []
        assert row.state_json == {}
        # ``upsert`` stamped updated_at with "now" (we passed 0).
        assert row.updated_at_ms > 0
    finally:
        await store.close()


async def test_seed_is_idempotent_and_does_not_clobber(tmp_path: Path) -> None:
    """A pre-existing grantley row survives a seed pass untouched."""
    store = await PersonaStore.open_or_create(tmp_path / "agent_state.sqlite")
    try:
        # Pre-existing live state (as the persona_life_* tools / EvolutionLoop
        # would have written it).
        await store.upsert(
            PersonaState(
                agent_id=DEFAULT_GRANTLEY_ID,
                mood="嘚瑟",
                fatigue=0.42,
                recent_topics=["篮球", "天气"],
                state_json={"life_last_meal": "螺蛳粉"},
            )
        )

        await _seed_builtin_persona_state(store)

        row = await store.get(DEFAULT_GRANTLEY_ID)
        assert row is not None
        # Nothing was overwritten with defaults.
        assert row.mood == "嘚瑟"
        assert row.fatigue == 0.42
        assert row.recent_topics == ["篮球", "天气"]
        assert row.state_json == {"life_last_meal": "螺蛳粉"}

        # A second pass is also a no-op.
        await _seed_builtin_persona_state(store)
        row2 = await store.get(DEFAULT_GRANTLEY_ID)
        assert row2 is not None
        assert row2.mood == "嘚瑟"
    finally:
        await store.close()


async def test_wire_c2_handles_seeds_grantley_state(tmp_path: Path) -> None:
    """Through the boot wiring spine: after _wire_c2_handles, the resolver
    sees a real grantley row (default mood) instead of an empty placeholder."""
    from corlinman_server.gateway.lifecycle.entrypoint import _wire_c2_handles

    state = AppState()
    state.data_dir = tmp_path
    app = SimpleNamespace(state=SimpleNamespace())

    await _wire_c2_handles(app, state, None, tmp_path, cfg={})

    assert state.persona_resolver is not None
    mood = await state.persona_resolver.resolve("mood", DEFAULT_GRANTLEY_ID)
    assert mood == "neutral"

    # The seeded row is on the SAME store the lifespan teardown closes.
    seeded = await app.state.corlinman_persona_state_store.get(
        DEFAULT_GRANTLEY_ID
    )
    assert seeded is not None

    await app.state.corlinman_persona_state_store.close()
    if state.memory_host is not None:
        await state.memory_host.close()
