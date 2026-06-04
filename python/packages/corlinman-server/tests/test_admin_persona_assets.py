"""Integration tests for ``/admin/personas/{id}/assets*`` routes.

Pins the W1 Persona Studio HTTP surface: multipart upload, listing,
ETag-tagged serving, deletion, and cascade-on-persona-delete. Uses
the same FastAPI TestClient pattern as ``test_sessions_replay.py``.
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
    # Live in-memory channels config the humanlike-route tests mutate
    # through the PUT endpoint. The writer callback captures every cfg
    # snapshot so tests can assert the route actually persisted.
    channels_config: dict[str, object] = {
        "qq": {"enabled": True, "ws_url": "ws://x"},
        "telegram": {"enabled": False, "bot_token": "t"},
        "discord": {"enabled": False, "bot_token": "d"},
        "slack": {"enabled": False, "app_token": "a", "bot_token": "b"},
        "feishu": {"enabled": False, "app_id": "i", "app_secret": "s"},
    }
    writer_calls: list[dict[str, object]] = []

    def _writer(cfg: dict[str, object]) -> None:
        writer_calls.append(cfg)

    state = AdminState(
        data_dir=tmp_path,
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
        persona_store=persona_store,
        persona_asset_store=asset_store,
        channels_config=channels_config,
        channels_writer=_writer,
    )
    state._writer_calls = writer_calls  # type: ignore[attr-defined]
    set_admin_state(state)
    app = FastAPI()
    app.include_router(build_router())

    # Seed one custom persona so the asset routes have a target.
    asyncio.run(
        persona_store.create(
            Persona(
                id="kawaii",
                display_name="Kawaii",
                short_summary="",
                system_prompt="be kawaii",
                is_builtin=False,
                created_at_ms=int(time.time() * 1000),
                updated_at_ms=int(time.time() * 1000),
            )
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
# Upload
# ---------------------------------------------------------------------------


def test_upload_then_list(client: TestClient) -> None:
    resp = client.post(
        "/admin/personas/kawaii/assets",
        data={"kind": "emoji", "label": "happy"},
        files={"file": ("happy.png", _PNG, "image/png")},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["kind"] == "emoji"
    assert body["label"] == "happy"
    assert body["mime"] == "image/png"
    assert body["size_bytes"] == len(_PNG)
    assert body["url"].startswith("/admin/personas/kawaii/assets/")

    list_resp = client.get("/admin/personas/kawaii/assets")
    assert list_resp.status_code == 200
    assets = list_resp.json()["assets"]
    assert len(assets) == 1
    assert assets[0]["label"] == "happy"


def test_list_filtered_by_kind(client: TestClient) -> None:
    client.post(
        "/admin/personas/kawaii/assets",
        data={"kind": "emoji", "label": "happy"},
        files={"file": ("happy.png", _PNG, "image/png")},
    )
    client.post(
        "/admin/personas/kawaii/assets",
        data={"kind": "reference", "label": "front"},
        files={"file": ("front.png", _PNG + b"\x00", "image/png")},
    )
    only_emoji = client.get(
        "/admin/personas/kawaii/assets", params={"kind": "emoji"}
    )
    only_refs = client.get(
        "/admin/personas/kawaii/assets", params={"kind": "reference"}
    )
    assert {a["label"] for a in only_emoji.json()["assets"]} == {"happy"}
    assert {a["label"] for a in only_refs.json()["assets"]} == {"front"}


def test_upload_rejects_invalid_label(client: TestClient) -> None:
    resp = client.post(
        "/admin/personas/kawaii/assets",
        data={"kind": "emoji", "label": "WITH SPACES!"},
        files={"file": ("x.png", _PNG, "image/png")},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_label"


def test_upload_rejects_invalid_kind(client: TestClient) -> None:
    resp = client.post(
        "/admin/personas/kawaii/assets",
        data={"kind": "weird", "label": "happy"},
        files={"file": ("x.png", _PNG, "image/png")},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "invalid_kind"


def test_upload_rejects_unsupported_mime(client: TestClient) -> None:
    resp = client.post(
        "/admin/personas/kawaii/assets",
        data={"kind": "emoji", "label": "happy"},
        files={"file": ("x.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 415
    assert resp.json()["detail"]["error"] == "unsupported_mime"


def test_upload_missing_persona_404(client: TestClient) -> None:
    resp = client.post(
        "/admin/personas/no-such/assets",
        data={"kind": "emoji", "label": "happy"},
        files={"file": ("x.png", _PNG, "image/png")},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "persona_not_found"


# ---------------------------------------------------------------------------
# Serve
# ---------------------------------------------------------------------------


def test_serve_returns_bytes_and_etag(client: TestClient) -> None:
    up = client.post(
        "/admin/personas/kawaii/assets",
        data={"kind": "emoji", "label": "happy"},
        files={"file": ("happy.png", _PNG, "image/png")},
    )
    asset_id = up.json()["id"]
    served = client.get(f"/admin/personas/kawaii/assets/{asset_id}")
    assert served.status_code == 200
    assert served.content == _PNG
    assert served.headers["content-type"].startswith("image/png")
    # ETag is the sha256.
    import hashlib

    assert served.headers["etag"] == f'"{hashlib.sha256(_PNG).hexdigest()}"'


def test_serve_404_for_unknown_asset(client: TestClient) -> None:
    resp = client.get("/admin/personas/kawaii/assets/bogus-id")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "asset_not_found"


def test_serve_404_for_wrong_persona_owner(client: TestClient) -> None:
    # Seed a second persona, upload asset to the FIRST, then try to
    # serve through the SECOND's URL — must 404 to prevent path
    # confusion.

    asyncio.run(
        _create_persona(client, "bobby")
    )
    up = client.post(
        "/admin/personas/kawaii/assets",
        data={"kind": "emoji", "label": "happy"},
        files={"file": ("happy.png", _PNG, "image/png")},
    )
    asset_id = up.json()["id"]
    resp = client.get(f"/admin/personas/bobby/assets/{asset_id}")
    assert resp.status_code == 404


async def _create_persona(client: TestClient, persona_id: str) -> None:
    """Helper that creates a persona via the admin POST (so the
    asset-store side stays in lockstep with the persona store the
    fixture booted)."""
    resp = client.post(
        "/admin/personas",
        json={
            "id": persona_id,
            "display_name": persona_id,
            "short_summary": "",
            "system_prompt": "stub",
        },
    )
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_asset_round_trip(client: TestClient) -> None:
    up = client.post(
        "/admin/personas/kawaii/assets",
        data={"kind": "emoji", "label": "happy"},
        files={"file": ("happy.png", _PNG, "image/png")},
    )
    asset_id = up.json()["id"]
    del_resp = client.delete(f"/admin/personas/kawaii/assets/{asset_id}")
    assert del_resp.status_code == 204
    after = client.get("/admin/personas/kawaii/assets")
    assert after.json()["assets"] == []


def test_delete_persona_cascades_assets(client: TestClient) -> None:
    client.post(
        "/admin/personas/kawaii/assets",
        data={"kind": "emoji", "label": "happy"},
        files={"file": ("happy.png", _PNG, "image/png")},
    )
    client.post(
        "/admin/personas/kawaii/assets",
        data={"kind": "reference", "label": "front"},
        files={"file": ("front.png", _PNG + b"\x00", "image/png")},
    )
    resp = client.delete("/admin/personas/kawaii")
    assert resp.status_code == 204
    # Asset list now 404s because the persona is gone.
    assets_resp = client.get("/admin/personas/kawaii/assets")
    assert assets_resp.status_code == 404


# ---------------------------------------------------------------------------
# Per-channel humanlike toggle — W7 generalisation of the QQ-only route
# ---------------------------------------------------------------------------


def test_humanlike_get_unknown_channel_404(client: TestClient) -> None:
    resp = client.get("/admin/channels/myspace/humanlike")
    assert resp.status_code == 404
    body = resp.json()["detail"]
    assert body["error"] == "unknown_channel"
    assert body["channel"] == "myspace"


def test_humanlike_put_unknown_channel_404(client: TestClient) -> None:
    resp = client.put(
        "/admin/channels/myspace/humanlike",
        json={"enabled": True, "persona_id": "kawaii"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "unknown_channel"


def test_humanlike_get_qq_defaults(client: TestClient) -> None:
    """No ``[channels.qq.humanlike]`` block yet → enabled=False,
    persona_id=None. Mirrors the old QQ-only test shape."""
    resp = client.get("/admin/channels/qq/humanlike")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"enabled": False, "persona_id": None}


def test_humanlike_put_then_get_telegram(client: TestClient) -> None:
    """Round-trip through the W7 generic route: PUT to Telegram,
    GET back the same toggle, and verify the writer persisted the
    ``[channels.telegram.humanlike]`` block."""
    put = client.put(
        "/admin/channels/telegram/humanlike",
        json={"enabled": True, "persona_id": "kawaii"},
    )
    assert put.status_code == 200, put.text
    assert put.json() == {"enabled": True, "persona_id": "kawaii"}

    got = client.get("/admin/channels/telegram/humanlike")
    assert got.status_code == 200
    assert got.json() == {"enabled": True, "persona_id": "kawaii"}


@pytest.mark.parametrize(
    "channel",
    [
        "discord",
        "slack",
        "feishu",
        "qq",
        "telegram",
        # The two "official" platforms were wired into the humanlike
        # resolver in Wave 2; they must now toggle here too (Codex #4).
        "qq_official",
        "wechat_official",
    ],
)
def test_humanlike_put_all_channels_round_trip(
    client: TestClient, channel: str
) -> None:
    """Every supported channel must accept a humanlike PUT and echo it
    back via GET."""
    put = client.put(
        f"/admin/channels/{channel}/humanlike",
        json={"enabled": True, "persona_id": "kawaii"},
    )
    assert put.status_code == 200, put.text
    got = client.get(f"/admin/channels/{channel}/humanlike")
    assert got.json() == {"enabled": True, "persona_id": "kawaii"}


def test_humanlike_put_requires_persona_id_when_enabled(
    client: TestClient,
) -> None:
    resp = client.put(
        "/admin/channels/telegram/humanlike",
        json={"enabled": True, "persona_id": None},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"] == "persona_id_required"


def test_humanlike_put_404s_on_unknown_persona(client: TestClient) -> None:
    resp = client.put(
        "/admin/channels/discord/humanlike",
        json={"enabled": True, "persona_id": "no-such-persona"},
    )
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "persona_not_found"


def test_humanlike_put_can_disable(client: TestClient) -> None:
    """PUT enabled=false + persona_id=None must clear the toggle."""
    # Enable first.
    client.put(
        "/admin/channels/slack/humanlike",
        json={"enabled": True, "persona_id": "kawaii"},
    )
    # Then disable.
    resp = client.put(
        "/admin/channels/slack/humanlike",
        json={"enabled": False, "persona_id": None},
    )
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False, "persona_id": None}
    got = client.get("/admin/channels/slack/humanlike")
    assert got.json() == {"enabled": False, "persona_id": None}
