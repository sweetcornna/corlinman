"""Admin persona model-binding contract.

Persona Studio lets operators bind separate text / image / voice model choices
to a persona. The CRUD API must round-trip those choices without touching
private runtime data.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from corlinman_server.gateway.routes_admin_a import (  # noqa: E402
    AdminState,
    build_router,
    set_admin_state,
)
from corlinman_server.gateway.routes_admin_a._session_store import (  # noqa: E402
    AdminSessionStore,
)
from corlinman_server.gateway.routes_admin_a.auth import hash_password  # noqa: E402
from corlinman_server.persona import PersonaStore  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _basic_auth_header() -> str:
    token = base64.b64encode(b"admin:rootroot").decode("ascii")
    return f"Basic {token}"


@pytest.fixture()
def client(tmp_path: Path) -> Iterator[TestClient]:
    persona_store = asyncio.run(PersonaStore.open(tmp_path / "personas.sqlite"))
    state = AdminState(
        data_dir=tmp_path,
        admin_username="admin",
        admin_password_hash=hash_password("rootroot"),
        session_store=AdminSessionStore(86_400),
        persona_store=persona_store,
    )
    set_admin_state(state)
    app = FastAPI()
    app.include_router(build_router())
    with TestClient(app, headers={"Authorization": _basic_auth_header()}) as c:
        yield c
    asyncio.run(persona_store.close())
    set_admin_state(None)


def test_create_get_and_list_round_trip_model_bindings(client: TestClient) -> None:
    create = client.post(
        "/admin/personas",
        json={
            "id": "senahydrangea",
            "display_name": "濑名紫阳花",
            "short_summary": "gentle persona",
            "system_prompt": "be gentle",
            "model_bindings": {
                "text": {"provider": "relay", "model": "gpt-5.5"},
                "image": {"provider": "draw", "model": "flux-pro"},
                "voice": {"provider": "voice", "model": "tts-large"},
            },
        },
    )
    assert create.status_code == 201, create.text
    body = create.json()
    assert body["model_bindings"]["text"] == {
        "provider": "relay",
        "model": "gpt-5.5",
    }

    got = client.get("/admin/personas/senahydrangea")
    assert got.status_code == 200
    assert got.json()["model_bindings"]["image"] == {
        "provider": "draw",
        "model": "flux-pro",
    }

    listed = client.get("/admin/personas")
    assert listed.status_code == 200
    row = next(
        p
        for p in listed.json()["personas"]
        if p["id"] == "senahydrangea"
    )
    assert row["model_bindings"]["voice"] == {
        "provider": "voice",
        "model": "tts-large",
    }


def test_patch_model_bindings_preserves_other_persona_fields(
    client: TestClient,
) -> None:
    create = client.post(
        "/admin/personas",
        json={
            "id": "hydrangea",
            "display_name": "Hydrangea",
            "short_summary": "original summary",
            "system_prompt": "original prompt",
        },
    )
    assert create.status_code == 201, create.text
    assert create.json()["model_bindings"] == {
        "text": {"provider": None, "model": None},
        "image": {"provider": None, "model": None},
        "voice": {"provider": None, "model": None},
    }

    patch = client.patch(
        "/admin/personas/hydrangea",
        json={
            "model_bindings": {
                "text": {"provider": "relay", "model": "gpt-5.5"},
                "image": {"provider": None, "model": None},
                "voice": {"provider": "voice", "model": "tts-large"},
            }
        },
    )
    assert patch.status_code == 200, patch.text
    body = patch.json()
    assert body["display_name"] == "Hydrangea"
    assert body["short_summary"] == "original summary"
    assert body["system_prompt"] == "original prompt"
    assert body["model_bindings"] == {
        "text": {"provider": "relay", "model": "gpt-5.5"},
        "image": {"provider": None, "model": None},
        "voice": {"provider": "voice", "model": "tts-large"},
    }


def test_patch_model_bindings_refreshes_sidecar_provider_registry(
    client: TestClient,
    tmp_path: Path,
) -> None:
    """Saving persona bindings should refresh the Python provider drop."""
    from corlinman_server.gateway.routes_admin_b.state import (
        AdminState as AdminBState,
    )
    from corlinman_server.gateway.routes_admin_b.state import (
        set_admin_state as set_admin_b_state,
    )

    py_config_path = tmp_path / "py-config.json"
    cfg = {
        "providers": {
            "voice-relay": {
                "kind": "openai_compatible",
                "enabled": True,
                "base_url": "https://relay.example/v1",
                "api_key": {"value": "sk-voice"},
            }
        }
    }
    set_admin_b_state(
        AdminBState(config_loader=lambda: cfg, py_config_path=py_config_path)
    )
    try:
        create = client.post(
            "/admin/personas",
            json={
                "id": "hydrangea-sidecar",
                "display_name": "Hydrangea",
                "short_summary": "summary",
                "system_prompt": "prompt",
            },
        )
        assert create.status_code == 201, create.text

        patch = client.patch(
            "/admin/personas/hydrangea-sidecar",
            json={
                "model_bindings": {
                    "voice": {"provider": "voice-relay", "model": "s2-pro"}
                }
            },
        )
    finally:
        set_admin_b_state(None)

    assert patch.status_code == 200, patch.text
    py_cfg = json.loads(py_config_path.read_text(encoding="utf-8"))
    providers = {p["name"]: p for p in py_cfg["providers"]}
    assert providers["voice-relay"]["base_url"] == "https://relay.example/v1"
