"""Tests for ``corlinman_server.persona.store`` — async aiosqlite-backed
persona registry. Mirrors the assertions in the spec the agent received.
"""

from __future__ import annotations

import time

import pytest

from corlinman_server.persona import (
    DEFAULT_GRANTLEY_ID,
    Persona,
    PersonaError,
    PersonaExists,
    PersonaProtected,
    PersonaStore,
    seed_builtin_personas,
)


@pytest.fixture
async def store(tmp_path):
    s = await PersonaStore.open(tmp_path / "personas.sqlite")
    try:
        yield s
    finally:
        await s.close()


async def test_create_round_trip(store) -> None:
    now = int(time.time() * 1000)
    p = Persona(
        id="kawaii",
        display_name="Kawaii Cat",
        short_summary="A friendly catgirl",
        system_prompt="You are a friendly catgirl, nya~",
        is_builtin=False,
        created_at_ms=now,
        updated_at_ms=now,
    )
    saved = await store.create(p)
    assert saved.id == "kawaii"
    assert saved.is_builtin is False
    got = await store.get("kawaii")
    assert got is not None
    assert got.system_prompt == "You are a friendly catgirl, nya~"


async def test_create_refuses_duplicate(store) -> None:
    now = int(time.time() * 1000)
    p = Persona(
        id="dup", display_name="d", short_summary="",
        system_prompt="x", is_builtin=False,
        created_at_ms=now, updated_at_ms=now,
    )
    await store.create(p)
    with pytest.raises(PersonaExists):
        await store.create(p)


async def test_create_refuses_is_builtin_from_outside(store) -> None:
    """API-side callers cannot stamp is_builtin=True via create()."""
    now = int(time.time() * 1000)
    p = Persona(
        id="fakebuiltin", display_name="x", short_summary="",
        system_prompt="x", is_builtin=True,   # caller-supplied
        created_at_ms=now, updated_at_ms=now,
    )
    with pytest.raises(PersonaProtected):
        await store.create(p)


async def test_update_preserves_builtin_flag(store) -> None:
    await seed_builtin_personas(store)
    updated = await store.update(
        DEFAULT_GRANTLEY_ID,
        display_name="格兰特利 v2",
    )
    assert updated is not None
    assert updated.display_name == "格兰特利 v2"
    assert updated.is_builtin is True  # preserved


async def test_delete_refuses_builtin(store) -> None:
    await seed_builtin_personas(store)
    with pytest.raises(PersonaProtected):
        await store.delete(DEFAULT_GRANTLEY_ID)


async def test_delete_custom_persona(store) -> None:
    now = int(time.time() * 1000)
    await store.create(Persona(
        id="custom", display_name="c", short_summary="",
        system_prompt="x", is_builtin=False,
        created_at_ms=now, updated_at_ms=now,
    ))
    removed = await store.delete("custom")
    assert removed is True
    assert await store.get("custom") is None


async def test_list_builtins_first(store) -> None:
    await seed_builtin_personas(store)
    now = int(time.time() * 1000)
    await store.create(Persona(
        id="c1", display_name="c1", short_summary="",
        system_prompt="x", is_builtin=False,
        created_at_ms=now, updated_at_ms=now,
    ))
    rows = await store.list()
    assert len(rows) >= 2
    assert rows[0].id == DEFAULT_GRANTLEY_ID  # builtin first
    assert rows[0].is_builtin is True


async def test_seed_idempotent(store) -> None:
    await seed_builtin_personas(store)
    await seed_builtin_personas(store)  # second call no-op
    rows = await store.list()
    builtin_ids = [r.id for r in rows if r.is_builtin]
    assert builtin_ids.count(DEFAULT_GRANTLEY_ID) == 1


async def test_grantley_seed_has_substantial_body(store) -> None:
    await seed_builtin_personas(store)
    g = await store.get(DEFAULT_GRANTLEY_ID)
    assert g is not None
    # The lifted SKILL.md body should be ≥2k chars (was 7948 at lift time);
    # the test stays loose so future edits don't trip on exact length.
    assert len(g.system_prompt) > 2000
    assert g.display_name  # non-empty


async def test_update_missing_persona_raises_persona_error(store) -> None:
    with pytest.raises(PersonaError):
        await store.update("does-not-exist", display_name="x")
