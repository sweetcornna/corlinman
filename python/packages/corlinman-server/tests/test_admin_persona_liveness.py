"""Integration tests for the R3 persona-liveness admin routes.

Pins the SHARED API CONTRACT surface added on top of the W1 Persona
Studio:

* ``GET/PATCH /admin/personas/{id}/life-state``
* ``GET       /admin/personas/{id}/diary``
* ``GET/PUT   /admin/personas/{id}/life-seeds``
* ``POST      /admin/personas/{id}/reset-to-default``
* ``POST      /admin/personas/{id}/decay``
* ``PATCH     /admin/personas/{id}/assets/{aid}`` (rename label)
* ``avatar_url`` on the persona list/get response model.

The life-STATE routes open ``agent_state.sqlite`` lazily off
``data_dir`` (the same path c2_wiring / the ``persona.decay`` builtin
use), so the fixture just points ``data_dir`` at ``tmp_path`` — no extra
store handle is wired onto ``AdminState``.

Uses the same FastAPI TestClient pattern as
``test_admin_persona_assets.py``.
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from corlinman_server.gateway.routes_admin_a import (
    AdminState,
    build_router,
    set_admin_state,
)
from corlinman_server.gateway.routes_admin_a._session_store import (
    AdminSessionStore,
)
from corlinman_server.gateway.routes_admin_a.auth import hash_password
from corlinman_server.persona import (
    Persona,
    PersonaAssetStore,
    PersonaStore,
)
from corlinman_server.persona.default_grantley import (
    DEFAULT_GRANTLEY_ID,
    load_default_grantley_body,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

_PNG = bytes.fromhex(
    "89504E470D0A1A0A0000000D49484452000000010000000108020000009077"
    "53DE"
)


def _basic_auth_header() -> str:
    token = base64.b64encode(b"admin:rootroot").decode("ascii")
    return f"Basic {token}"


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    persona_store, asset_store = asyncio.run(_open_stores(tmp_path))
    state = AdminState(
        data_dir=tmp_path,
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
        persona_store=persona_store,
        persona_asset_store=asset_store,
    )
    set_admin_state(state)
    app = FastAPI()
    app.include_router(build_router())

    # One custom persona target + a built-in grantley row (the only built-in,
    # so reset-to-default has something to re-seed).
    now = int(time.time() * 1000)
    asyncio.run(
        persona_store.create(
            Persona(
                id="kawaii",
                display_name="Kawaii",
                short_summary="",
                system_prompt="be kawaii",
                is_builtin=False,
                created_at_ms=now,
                updated_at_ms=now,
            )
        )
    )
    # Seed the built-in grantley row through the same insert path the boot
    # seeder uses (``create`` refuses is_builtin=True from public callers).
    asyncio.run(
        persona_store._insert(  # type: ignore[attr-defined]
            Persona(
                id=DEFAULT_GRANTLEY_ID,
                display_name="overwritten",
                short_summary="overwritten",
                system_prompt="overwritten body",
                is_builtin=True,
                created_at_ms=now,
                updated_at_ms=now,
            ),
            builtin=True,
        )
    )

    with TestClient(app, headers={"Authorization": _basic_auth_header()}) as c:
        yield c

    asyncio.run(persona_store.close())
    asyncio.run(asset_store.close())
    set_admin_state(None)


async def _open_stores(tmp_path: Path):
    ps = await PersonaStore.open(tmp_path / "personas.sqlite")
    pas = await PersonaAssetStore.open(
        tmp_path / "persona_assets.sqlite",
        tmp_path / "personas",
        max_bytes_per_asset=1024 * 1024,
        max_bytes_per_persona=4 * 1024 * 1024,
    )
    return ps, pas


# ---------------------------------------------------------------------------
# life-state GET / PATCH
# ---------------------------------------------------------------------------


def test_life_state_get_defaults_when_no_row(client: TestClient) -> None:
    resp = client.get("/admin/personas/kawaii/life-state")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "mood": "neutral",
        "fatigue": 0.0,
        "recent_topics": [],
        "state_json": {},
        "updated_at_ms": 0,
    }


def test_life_state_get_404_for_missing_persona(client: TestClient) -> None:
    resp = client.get("/admin/personas/no-such/life-state")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "persona_not_found"


def test_life_state_patch_upserts_and_roundtrips(client: TestClient) -> None:
    resp = client.patch(
        "/admin/personas/kawaii/life-state",
        json={"mood": "嘚瑟", "fatigue": 0.42, "recent_topics": ["篮球", "天气"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mood"] == "嘚瑟"
    assert body["fatigue"] == 0.42
    assert body["recent_topics"] == ["篮球", "天气"]
    assert body["updated_at_ms"] > 0

    # GET reads back the upserted row.
    got = client.get("/admin/personas/kawaii/life-state").json()
    assert got["mood"] == "嘚瑟"
    assert got["fatigue"] == 0.42


def test_life_state_patch_partial_preserves_other_fields(
    client: TestClient,
) -> None:
    client.patch(
        "/admin/personas/kawaii/life-state",
        json={"mood": "tired", "fatigue": 0.9, "recent_topics": ["a"]},
    )
    # Patch only mood — fatigue + topics must survive.
    resp = client.patch(
        "/admin/personas/kawaii/life-state", json={"mood": "neutral"}
    )
    body = resp.json()
    assert body["mood"] == "neutral"
    assert body["fatigue"] == 0.9
    assert body["recent_topics"] == ["a"]


def test_life_state_patch_rejects_out_of_range_fatigue(
    client: TestClient,
) -> None:
    resp = client.patch(
        "/admin/personas/kawaii/life-state", json={"fatigue": 1.5}
    )
    assert resp.status_code == 422  # pydantic le=1.0 validation


def test_life_state_patch_404_for_missing_persona(client: TestClient) -> None:
    resp = client.patch(
        "/admin/personas/no-such/life-state", json={"mood": "x"}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "persona_not_found"


# ---------------------------------------------------------------------------
# diary GET
# ---------------------------------------------------------------------------


def _seed_diary_row(client: TestClient) -> None:
    """Seed a state row carrying a ``diary`` blob via the life-state PATCH —
    the PATCH preserves ``state_json`` from any existing row, so we plant
    the diary directly through the runtime store the route reads."""
    from corlinman_persona.state import PersonaState
    from corlinman_persona.store import PersonaStore as StateStore

    async def _go(db: Path) -> None:
        async with StateStore(db) as store:
            await store.upsert(
                PersonaState(
                    agent_id="kawaii",
                    state_json={
                        "diary": [
                            {"ts": "2026-06-01T08:00:00+00:00", "entry": "晨练"},
                            {"ts": "2026-06-02T20:00:00+00:00", "entry": "夜跑"},
                            {"ts": 1717000000000, "text": "operator note"},
                        ]
                    },
                )
            )

    from corlinman_server.gateway.routes_admin_a.state import get_admin_state

    db = get_admin_state().data_dir / "agent_state.sqlite"
    asyncio.run(_go(db))


def test_diary_get_empty_when_no_row(client: TestClient) -> None:
    resp = client.get("/admin/personas/kawaii/diary")
    assert resp.status_code == 200
    assert resp.json() == {"entries": []}


def test_diary_get_normalises_entries(client: TestClient) -> None:
    _seed_diary_row(client)
    resp = client.get("/admin/personas/kawaii/diary")
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    # Newest last, normalised to {ts:int, text:str}.
    assert [e["text"] for e in entries] == ["晨练", "夜跑", "operator note"]
    assert all(isinstance(e["ts"], int) for e in entries)
    # ISO ts parsed to epoch-ms (>0); the int ts is passed through.
    assert entries[0]["ts"] > 0
    assert entries[2]["ts"] == 1717000000000


def test_diary_get_respects_limit(client: TestClient) -> None:
    _seed_diary_row(client)
    resp = client.get("/admin/personas/kawaii/diary", params={"limit": 1})
    entries = resp.json()["entries"]
    assert len(entries) == 1
    # Tail keeps the newest.
    assert entries[0]["text"] == "operator note"


def test_diary_get_404_for_missing_persona(client: TestClient) -> None:
    resp = client.get("/admin/personas/no-such/diary")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "persona_not_found"


# ---------------------------------------------------------------------------
# life-seeds GET / PUT
# ---------------------------------------------------------------------------


def test_life_seeds_get_generic_for_plain_persona(client: TestClient) -> None:
    resp = client.get("/admin/personas/kawaii/life-seeds")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "generic"
    assert "mission_scenario" in body["yaml"]


def test_life_seeds_get_bundled_for_grantley(client: TestClient) -> None:
    resp = client.get(f"/admin/personas/{DEFAULT_GRANTLEY_ID}/life-seeds")
    assert resp.status_code == 200
    assert resp.json()["source"] == "bundled"


def test_life_seeds_put_then_get_override(client: TestClient) -> None:
    yaml_body = "companion:\n  - 小明\n  - 小红\nmission_scenario:\n  - 调查\n"
    put = client.put(
        "/admin/personas/kawaii/life-seeds", json={"yaml": yaml_body}
    )
    assert put.status_code == 200, put.text
    assert put.json() == {"ok": True}

    got = client.get("/admin/personas/kawaii/life-seeds").json()
    assert got["source"] == "override"
    assert "小明" in got["yaml"]


def test_life_seeds_put_rejects_invalid_yaml(client: TestClient) -> None:
    resp = client.put(
        "/admin/personas/kawaii/life-seeds",
        json={"yaml": "companion: [unterminated\n  - bad"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_yaml"


def test_life_seeds_put_rejects_non_mapping(client: TestClient) -> None:
    resp = client.put(
        "/admin/personas/kawaii/life-seeds", json={"yaml": "just a string"}
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_yaml"


def test_life_seeds_put_404_for_missing_persona(client: TestClient) -> None:
    resp = client.put(
        "/admin/personas/no-such/life-seeds", json={"yaml": "a:\n  - b\n"}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "persona_not_found"


# ---------------------------------------------------------------------------
# reset-to-default POST
# ---------------------------------------------------------------------------


def test_reset_to_default_reseeds_builtin(client: TestClient) -> None:
    # The fixture seeded grantley with an "overwritten" body; reset restores it.
    before = client.get(f"/admin/personas/{DEFAULT_GRANTLEY_ID}").json()
    assert before["system_prompt"] == "overwritten body"

    resp = client.post(f"/admin/personas/{DEFAULT_GRANTLEY_ID}/reset-to-default")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}

    after = client.get(f"/admin/personas/{DEFAULT_GRANTLEY_ID}").json()
    assert after["system_prompt"] == load_default_grantley_body()
    assert after["system_prompt"] != "overwritten body"


def test_reset_to_default_400_for_custom_persona(client: TestClient) -> None:
    resp = client.post("/admin/personas/kawaii/reset-to-default")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "not_builtin"


def test_reset_to_default_404_for_missing_persona(client: TestClient) -> None:
    resp = client.post("/admin/personas/no-such/reset-to-default")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "persona_not_found"


# ---------------------------------------------------------------------------
# decay POST
# ---------------------------------------------------------------------------


def test_decay_changes_aged_row(client: TestClient) -> None:
    from corlinman_persona.state import PersonaState
    from corlinman_persona.store import PersonaStore as StateStore
    from corlinman_server.gateway.routes_admin_a.state import get_admin_state

    db = get_admin_state().data_dir / "agent_state.sqlite"

    async def _seed_aged() -> None:
        async with StateStore(db) as store:
            # Stamp a 48h-old timestamp so fatigue recovery actually moves.
            old = int(time.time() * 1000) - 48 * 3_600_000
            await store.upsert(
                PersonaState(
                    agent_id="kawaii",
                    mood="tired",
                    fatigue=0.8,
                    recent_topics=["a", "b"],
                    updated_at_ms=old,
                )
            )

    asyncio.run(_seed_aged())
    resp = client.post("/admin/personas/kawaii/decay")
    assert resp.status_code == 200, resp.text
    assert resp.json()["rows_changed"] == 1

    # Fatigue recovered below the tired→neutral threshold.
    after = client.get("/admin/personas/kawaii/life-state").json()
    assert after["fatigue"] < 0.8
    assert after["mood"] == "neutral"


def test_decay_no_row_reports_zero(client: TestClient) -> None:
    resp = client.post("/admin/personas/kawaii/decay")
    assert resp.status_code == 200
    assert resp.json()["rows_changed"] == 0


def test_decay_404_for_missing_persona(client: TestClient) -> None:
    resp = client.post("/admin/personas/no-such/decay")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "persona_not_found"


# ---------------------------------------------------------------------------
# asset label PATCH (rename)
# ---------------------------------------------------------------------------


def _upload(client: TestClient, kind: str, label: str, extra: bytes = b"") -> str:
    resp = client.post(
        "/admin/personas/kawaii/assets",
        data={"kind": kind, "label": label},
        files={"file": (f"{label}.png", _PNG + extra, "image/png")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_asset_relabel_happy_path(client: TestClient) -> None:
    asset_id = _upload(client, "emoji", "happy")
    resp = client.patch(
        f"/admin/personas/kawaii/assets/{asset_id}", json={"label": "joyful"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["label"] == "joyful"
    # The new label is what listing surfaces.
    labels = {
        a["label"] for a in client.get("/admin/personas/kawaii/assets").json()["assets"]
    }
    assert labels == {"joyful"}


def test_asset_relabel_409_on_collision(client: TestClient) -> None:
    _upload(client, "emoji", "happy")
    other = _upload(client, "emoji", "sad", extra=b"\x01")
    resp = client.patch(
        f"/admin/personas/kawaii/assets/{other}", json={"label": "happy"}
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "duplicate_label"


def test_asset_relabel_400_on_invalid_label(client: TestClient) -> None:
    asset_id = _upload(client, "emoji", "happy")
    resp = client.patch(
        f"/admin/personas/kawaii/assets/{asset_id}",
        json={"label": "WITH SPACES!"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_label"


def test_asset_relabel_404_for_unknown_asset(client: TestClient) -> None:
    resp = client.patch(
        "/admin/personas/kawaii/assets/bogus-id", json={"label": "joyful"}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "asset_not_found"


def test_asset_relabel_404_for_wrong_persona_owner(client: TestClient) -> None:
    asset_id = _upload(client, "emoji", "happy")
    # Same id, wrong persona path segment → path-confusion guard 404s.
    resp = client.patch(
        f"/admin/personas/grantley/assets/{asset_id}", json={"label": "joyful"}
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "asset_not_found"


# ---------------------------------------------------------------------------
# avatar_url on PersonaOut
# ---------------------------------------------------------------------------


def test_avatar_url_null_without_assets(client: TestClient) -> None:
    got = client.get("/admin/personas/kawaii").json()
    assert got["avatar_url"] is None


def test_avatar_url_prefers_emoji_then_reference(client: TestClient) -> None:
    # Reference-only first → avatar falls back to the reference asset.
    ref_id = _upload(client, "reference", "front", extra=b"\x02")
    got = client.get("/admin/personas/kawaii").json()
    assert got["avatar_url"] == f"/admin/personas/kawaii/assets/{ref_id}"

    # Adding an emoji makes it win over the reference.
    emoji_id = _upload(client, "emoji", "happy")
    got2 = client.get("/admin/personas/kawaii").json()
    assert got2["avatar_url"] == f"/admin/personas/kawaii/assets/{emoji_id}"


def test_avatar_url_in_list(client: TestClient) -> None:
    emoji_id = _upload(client, "emoji", "happy")
    rows = client.get("/admin/personas").json()["personas"]
    kawaii = next(p for p in rows if p["id"] == "kawaii")
    assert kawaii["avatar_url"] == f"/admin/personas/kawaii/assets/{emoji_id}"
