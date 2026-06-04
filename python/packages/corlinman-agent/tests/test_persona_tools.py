"""Tests for the ``persona.*`` builtin tool dispatchers (W3).

Each of the seven persona.* tools is exercised on the happy path plus
at least one error envelope (missing persona, duplicate slug, quota
exceeded, invalid args). Uses the real ``PersonaStore`` +
``PersonaAssetStore`` from corlinman-server — those are the stable
contracts the dispatchers wrap.
"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from corlinman_agent.persona import (
    PERSONA_ATTACH_ASSET_FROM_ATTACHMENT_TOOL,
    PERSONA_ATTACH_ASSET_FROM_DATA_TOOL,
    PERSONA_ATTACH_ASSET_FROM_URL_TOOL,
    PERSONA_CREATE_TOOL,
    PERSONA_DELETE_TOOL,
    PERSONA_GET_TOOL,
    PERSONA_LIST_ASSETS_TOOL,
    PERSONA_LIST_TOOL,
    PERSONA_TOOLS,
    PERSONA_UPDATE_TOOL,
    dispatch_persona_attach_asset_from_attachment,
    dispatch_persona_attach_asset_from_data,
    dispatch_persona_attach_asset_from_url,
    dispatch_persona_create,
    dispatch_persona_delete,
    dispatch_persona_get,
    dispatch_persona_list,
    dispatch_persona_list_assets,
    dispatch_persona_update,
    persona_attach_asset_from_attachment_tool_schema,
    persona_attach_asset_from_data_tool_schema,
    persona_attach_asset_from_url_tool_schema,
    persona_create_tool_schema,
    persona_delete_tool_schema,
    persona_get_tool_schema,
    persona_list_assets_tool_schema,
    persona_list_tool_schema,
    persona_tool_schemas,
    persona_update_tool_schema,
)
from corlinman_agent.reasoning_loop import Attachment
from corlinman_server.persona import (
    Persona,
    PersonaAssetStore,
    PersonaStore,
)

# Minimal valid PNG header so MIME sniffing + the asset store accept the
# upload. Same fixture shape as test_persona_asset_store.
_PNG_MAGIC = bytes.fromhex(
    "89504E470D0A1A0A0000000D49484452000000010000000108020000009077"
    "53DE"
)


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


@pytest.fixture
async def persona_store(tmp_path):
    s = await PersonaStore.open(tmp_path / "personas.sqlite")
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
async def asset_store(tmp_path):
    # Per-asset cap intentionally larger than per-persona so the quota
    # test below fires the AssetQuotaExceeded branch (not AssetTooLarge).
    s = await PersonaAssetStore.open(
        tmp_path / "persona_assets.sqlite",
        tmp_path / "personas",
        max_bytes_per_asset=3 * 1024 * 1024,  # 3 MiB
        max_bytes_per_persona=2 * 1024 * 1024,  # 2 MiB
    )
    try:
        yield s
    finally:
        await s.close()


async def _seed(persona_store: PersonaStore, *, pid: str = "kawaii") -> Persona:
    now = _now_ms()
    p = Persona(
        id=pid,
        display_name="Kawaii Cat",
        short_summary="A friendly catgirl",
        system_prompt="You are a friendly catgirl, nya~",
        is_builtin=False,
        created_at_ms=now,
        updated_at_ms=now,
    )
    return await persona_store.create(p)


# ---------------------------------------------------------------------------
# Schemas + wire-stable names
# ---------------------------------------------------------------------------


def test_tool_names_are_wire_stable() -> None:
    assert PERSONA_LIST_TOOL == "persona_list"
    assert PERSONA_GET_TOOL == "persona_get"
    assert PERSONA_CREATE_TOOL == "persona_create"
    assert PERSONA_UPDATE_TOOL == "persona_update"
    assert PERSONA_DELETE_TOOL == "persona_delete"
    assert PERSONA_LIST_ASSETS_TOOL == "persona_list_assets"
    assert PERSONA_ATTACH_ASSET_FROM_URL_TOOL == "persona_attach_asset_from_url"
    assert (
        PERSONA_ATTACH_ASSET_FROM_DATA_TOOL == "persona_attach_asset_from_data"
    )
    assert (
        PERSONA_ATTACH_ASSET_FROM_ATTACHMENT_TOOL
        == "persona_attach_asset_from_attachment"
    )
    assert PERSONA_TOOLS == frozenset(
        {
            "persona_list",
            "persona_get",
            "persona_create",
            "persona_update",
            "persona_delete",
            "persona_list_assets",
            "persona_attach_asset_from_url",
            "persona_attach_asset_from_data",
            "persona_attach_asset_from_attachment",
        }
    )


@pytest.mark.parametrize(
    ("schema_fn", "name"),
    [
        (persona_list_tool_schema, "persona_list"),
        (persona_get_tool_schema, "persona_get"),
        (persona_create_tool_schema, "persona_create"),
        (persona_update_tool_schema, "persona_update"),
        (persona_delete_tool_schema, "persona_delete"),
        (persona_list_assets_tool_schema, "persona_list_assets"),
        (persona_attach_asset_from_url_tool_schema, "persona_attach_asset_from_url"),
        (
            persona_attach_asset_from_data_tool_schema,
            "persona_attach_asset_from_data",
        ),
        (
            persona_attach_asset_from_attachment_tool_schema,
            "persona_attach_asset_from_attachment",
        ),
    ],
)
def test_schemas_are_openai_shaped(schema_fn, name) -> None:  # type: ignore[no-untyped-def]
    schema = schema_fn()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == name
    assert "parameters" in schema["function"]
    assert schema["function"]["parameters"]["type"] == "object"


def test_persona_tool_schemas_returns_all() -> None:
    schemas = persona_tool_schemas()
    names = {s["function"]["name"] for s in schemas}
    assert names == PERSONA_TOOLS
    # No duplicate schema entries.
    assert len(schemas) == len(PERSONA_TOOLS)


# ---------------------------------------------------------------------------
# persona_list
# ---------------------------------------------------------------------------


async def test_list_empty(persona_store, asset_store) -> None:
    out = json.loads(
        await dispatch_persona_list(
            args_json=b"{}",
            persona_store=persona_store,
            asset_store=asset_store,
        )
    )
    assert out["ok"] is True
    assert out["personas"] == []


async def test_list_returns_summaries(persona_store, asset_store) -> None:
    await _seed(persona_store, pid="kawaii")
    out = json.loads(
        await dispatch_persona_list(
            args_json=b"{}",
            persona_store=persona_store,
            asset_store=asset_store,
        )
    )
    assert out["ok"] is True
    assert len(out["personas"]) == 1
    p = out["personas"][0]
    assert p["id"] == "kawaii"
    assert p["display_name"] == "Kawaii Cat"
    assert p["short_summary"] == "A friendly catgirl"
    assert p["is_builtin"] is False
    # Summary view never carries the body.
    assert "system_prompt" not in p


async def test_list_store_unavailable() -> None:
    out = json.loads(
        await dispatch_persona_list(
            args_json=b"{}",
            persona_store=None,
            asset_store=None,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "persona_store_unavailable"


# ---------------------------------------------------------------------------
# persona_get
# ---------------------------------------------------------------------------


async def test_get_happy(persona_store, asset_store) -> None:
    await _seed(persona_store, pid="kawaii")
    out = json.loads(
        await dispatch_persona_get(
            args_json=json.dumps({"id": "kawaii"}).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
        )
    )
    assert out["ok"] is True
    assert out["persona"]["id"] == "kawaii"
    assert out["persona"]["system_prompt"].startswith(
        "You are a friendly catgirl"
    )
    assert out["persona"]["system_prompt_truncated"] is False


async def test_get_clips_long_body(persona_store, asset_store) -> None:
    now = _now_ms()
    long_body = "x" * 5000
    await persona_store.create(
        Persona(
            id="bigp",
            display_name="Big Body",
            short_summary="",
            system_prompt=long_body,
            is_builtin=False,
            created_at_ms=now,
            updated_at_ms=now,
        )
    )
    out = json.loads(
        await dispatch_persona_get(
            args_json=json.dumps({"id": "bigp"}).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
        )
    )
    assert out["ok"] is True
    assert out["persona"]["system_prompt_truncated"] is True
    assert out["persona"]["system_prompt"].endswith("…truncated")


async def test_get_missing_persona(persona_store, asset_store) -> None:
    out = json.loads(
        await dispatch_persona_get(
            args_json=json.dumps({"id": "nope"}).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "persona_not_found"


async def test_get_missing_id_arg(persona_store, asset_store) -> None:
    out = json.loads(
        await dispatch_persona_get(
            args_json=b"{}",
            persona_store=persona_store,
            asset_store=asset_store,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "invalid_args"


# ---------------------------------------------------------------------------
# persona_create
# ---------------------------------------------------------------------------


async def test_create_happy(persona_store, asset_store) -> None:
    out = json.loads(
        await dispatch_persona_create(
            args_json=json.dumps(
                {
                    "id": "newp",
                    "display_name": "New",
                    "short_summary": "fresh",
                    "system_prompt": "Speak like a wizard.",
                }
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
        )
    )
    assert out["ok"] is True
    assert out["persona"]["id"] == "newp"
    # Full body returned on create (no clip).
    assert out["persona"]["system_prompt"] == "Speak like a wizard."
    assert out["persona"]["system_prompt_truncated"] is False
    # Confirm the row landed in the store.
    row = await persona_store.get("newp")
    assert row is not None
    assert row.display_name == "New"


async def test_create_duplicate_slug(persona_store, asset_store) -> None:
    await _seed(persona_store, pid="kawaii")
    out = json.loads(
        await dispatch_persona_create(
            args_json=json.dumps(
                {
                    "id": "kawaii",
                    "display_name": "Duplicate",
                    "system_prompt": "x",
                }
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "persona_exists"


async def test_create_missing_args(persona_store, asset_store) -> None:
    out = json.loads(
        await dispatch_persona_create(
            args_json=json.dumps({"id": "x"}).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "invalid_args"


# ---------------------------------------------------------------------------
# persona_update
# ---------------------------------------------------------------------------


async def test_update_happy(persona_store, asset_store) -> None:
    await _seed(persona_store, pid="kawaii")
    out = json.loads(
        await dispatch_persona_update(
            args_json=json.dumps(
                {"id": "kawaii", "display_name": "Kawaii v2"}
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
        )
    )
    assert out["ok"] is True
    assert out["persona"]["display_name"] == "Kawaii v2"


async def test_update_no_fields(persona_store, asset_store) -> None:
    await _seed(persona_store, pid="kawaii")
    out = json.loads(
        await dispatch_persona_update(
            args_json=json.dumps({"id": "kawaii"}).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "invalid_args"


async def test_update_missing_persona(persona_store, asset_store) -> None:
    out = json.loads(
        await dispatch_persona_update(
            args_json=json.dumps(
                {"id": "nope", "display_name": "x"}
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "persona_not_found"


# ---------------------------------------------------------------------------
# persona_delete
# ---------------------------------------------------------------------------


async def test_delete_happy(persona_store, asset_store) -> None:
    await _seed(persona_store, pid="kawaii")
    out = json.loads(
        await dispatch_persona_delete(
            args_json=json.dumps({"id": "kawaii"}).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
        )
    )
    assert out["ok"] is True
    assert out["removed"] is True
    assert await persona_store.get("kawaii") is None


async def test_delete_unknown_returns_removed_false(
    persona_store, asset_store
) -> None:
    out = json.loads(
        await dispatch_persona_delete(
            args_json=json.dumps({"id": "ghost"}).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
        )
    )
    assert out["ok"] is True
    # Store's delete() returns False for missing rows, matching admin
    # route's 404 semantics; the dispatcher surfaces that as removed=false.
    assert out["removed"] is False


async def test_delete_builtin_protected(persona_store, asset_store) -> None:
    from corlinman_server.persona import (
        DEFAULT_GRANTLEY_ID,
        seed_builtin_personas,
    )

    await seed_builtin_personas(persona_store)
    out = json.loads(
        await dispatch_persona_delete(
            args_json=json.dumps({"id": DEFAULT_GRANTLEY_ID}).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "persona_protected"


# ---------------------------------------------------------------------------
# persona_list_assets
# ---------------------------------------------------------------------------


async def test_list_assets_happy(persona_store, asset_store) -> None:
    await _seed(persona_store, pid="kawaii")
    await asset_store.put(
        "kawaii",
        "emoji",
        "happy",
        bytes_=_PNG_MAGIC,
        mime="image/png",
        file_name="happy.png",
    )
    await asset_store.put(
        "kawaii",
        "reference",
        "front",
        bytes_=_PNG_MAGIC + b"\x00",
        mime="image/png",
        file_name="front.png",
    )
    out = json.loads(
        await dispatch_persona_list_assets(
            args_json=json.dumps({"id": "kawaii"}).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
        )
    )
    assert out["ok"] is True
    assert len(out["assets"]) == 2
    labels = {a["label"] for a in out["assets"]}
    assert labels == {"happy", "front"}


async def test_list_assets_kind_filter(persona_store, asset_store) -> None:
    await _seed(persona_store, pid="kawaii")
    await asset_store.put(
        "kawaii",
        "emoji",
        "happy",
        bytes_=_PNG_MAGIC,
        mime="image/png",
        file_name="happy.png",
    )
    await asset_store.put(
        "kawaii",
        "reference",
        "front",
        bytes_=_PNG_MAGIC + b"\x00",
        mime="image/png",
        file_name="front.png",
    )
    out = json.loads(
        await dispatch_persona_list_assets(
            args_json=json.dumps({"id": "kawaii", "kind": "emoji"}).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
        )
    )
    assert out["ok"] is True
    assert {a["label"] for a in out["assets"]} == {"happy"}


async def test_list_assets_missing_persona(persona_store, asset_store) -> None:
    out = json.loads(
        await dispatch_persona_list_assets(
            args_json=json.dumps({"id": "nope"}).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "persona_not_found"


async def test_list_assets_invalid_kind(persona_store, asset_store) -> None:
    await _seed(persona_store, pid="kawaii")
    out = json.loads(
        await dispatch_persona_list_assets(
            args_json=json.dumps(
                {"id": "kawaii", "kind": "stickers"}
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "invalid_args"


# ---------------------------------------------------------------------------
# persona_attach_asset_from_url
# ---------------------------------------------------------------------------


def _png_handler(
    body: bytes = _PNG_MAGIC, content_type: str = "image/png"
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body, headers={"content-type": content_type}
        )

    return handler


async def test_attach_asset_happy(persona_store, asset_store) -> None:
    await _seed(persona_store, pid="kawaii")
    transport = httpx.MockTransport(_png_handler())
    out = json.loads(
        await dispatch_persona_attach_asset_from_url(
            args_json=json.dumps(
                {
                    "persona_id": "kawaii",
                    "kind": "emoji",
                    "label": "happy",
                    "url": "https://example.com/happy.png",
                }
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
            transport=transport,
        )
    )
    assert out["ok"] is True
    assert out["asset"]["label"] == "happy"
    assert out["asset"]["sha256"] == hashlib.sha256(_PNG_MAGIC).hexdigest()
    # Confirm the bytes landed on disk.
    record = await asset_store.get("kawaii", "emoji", "happy")
    assert record is not None
    assert asset_store.path_for(record).read_bytes() == _PNG_MAGIC


async def test_attach_asset_default_file_name_from_url(
    persona_store, asset_store
) -> None:
    await _seed(persona_store, pid="kawaii")
    transport = httpx.MockTransport(_png_handler())
    out = json.loads(
        await dispatch_persona_attach_asset_from_url(
            args_json=json.dumps(
                {
                    "persona_id": "kawaii",
                    "kind": "emoji",
                    "label": "h",
                    "url": "https://cdn.example.com/foo/bar.png",
                }
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
            transport=transport,
        )
    )
    assert out["ok"] is True
    assert out["asset"]["file_name"] == "bar.png"


async def test_attach_asset_missing_persona(
    persona_store, asset_store
) -> None:
    transport = httpx.MockTransport(_png_handler())
    out = json.loads(
        await dispatch_persona_attach_asset_from_url(
            args_json=json.dumps(
                {
                    "persona_id": "ghost",
                    "kind": "emoji",
                    "label": "x",
                    "url": "https://example.com/x.png",
                }
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
            transport=transport,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "persona_not_found"


async def test_attach_asset_unsupported_mime(
    persona_store, asset_store
) -> None:
    await _seed(persona_store, pid="kawaii")
    transport = httpx.MockTransport(
        _png_handler(body=b"not really svg", content_type="image/svg+xml")
    )
    out = json.loads(
        await dispatch_persona_attach_asset_from_url(
            args_json=json.dumps(
                {
                    "persona_id": "kawaii",
                    "kind": "emoji",
                    "label": "x",
                    "url": "https://example.com/x.svg",
                }
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
            transport=transport,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "unsupported_mime"


async def test_attach_asset_quota_exceeded(
    persona_store, asset_store
) -> None:
    """Per-persona cap is 2 MiB in the fixture; uploading 2x ~1 MiB
    blobs should hit the asset store's ``AssetQuotaExceeded``."""
    await _seed(persona_store, pid="kawaii")
    big_png = _PNG_MAGIC + (b"\x00" * (1024 * 1024 - len(_PNG_MAGIC)))
    transport = httpx.MockTransport(_png_handler(body=big_png))
    # First upload — fits.
    out1 = json.loads(
        await dispatch_persona_attach_asset_from_url(
            args_json=json.dumps(
                {
                    "persona_id": "kawaii",
                    "kind": "reference",
                    "label": "front",
                    "url": "https://example.com/front.png",
                }
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
            transport=transport,
        )
    )
    assert out1["ok"] is True
    # Second upload — different label, but combined size > 2 MiB cap.
    big_png2 = _PNG_MAGIC + (b"\x01" * (1024 * 1024 + 1024))
    transport2 = httpx.MockTransport(_png_handler(body=big_png2))
    out2 = json.loads(
        await dispatch_persona_attach_asset_from_url(
            args_json=json.dumps(
                {
                    "persona_id": "kawaii",
                    "kind": "reference",
                    "label": "side",
                    "url": "https://example.com/side.png",
                }
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
            transport=transport2,
        )
    )
    assert out2["ok"] is False
    assert out2["error"] == "quota_exceeded"


async def test_attach_asset_download_too_large(
    persona_store, asset_store
) -> None:
    """Synthesize a body larger than the 10 MiB download cap so the
    dispatcher's stream guard fires before the asset store ever sees
    the bytes."""
    await _seed(persona_store, pid="kawaii")
    # 11 MiB body — over the 10 MiB cap.
    huge = _PNG_MAGIC + (b"\x00" * (11 * 1024 * 1024))
    transport = httpx.MockTransport(_png_handler(body=huge))
    out = json.loads(
        await dispatch_persona_attach_asset_from_url(
            args_json=json.dumps(
                {
                    "persona_id": "kawaii",
                    "kind": "emoji",
                    "label": "fat",
                    "url": "https://example.com/fat.png",
                }
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
            transport=transport,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "download_too_large"


async def test_attach_asset_http_status_error(
    persona_store, asset_store
) -> None:
    await _seed(persona_store, pid="kawaii")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"not found")

    transport = httpx.MockTransport(handler)
    out = json.loads(
        await dispatch_persona_attach_asset_from_url(
            args_json=json.dumps(
                {
                    "persona_id": "kawaii",
                    "kind": "emoji",
                    "label": "x",
                    "url": "https://example.com/x.png",
                }
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
            transport=transport,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "download_failed"


async def test_attach_asset_invalid_args(persona_store, asset_store) -> None:
    out = json.loads(
        await dispatch_persona_attach_asset_from_url(
            args_json=json.dumps(
                {"persona_id": "kawaii", "kind": "emoji", "label": "x"}
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "invalid_args"


# ---------------------------------------------------------------------------
# persona-inference (bound_persona_id) — the "as grantley, save this as my
# 立绘" path where the model omits persona_id and the servicer supplies the
# bound persona from start.extra["persona_id"].
# ---------------------------------------------------------------------------


async def test_attach_from_url_infers_bound_persona(
    persona_store, asset_store
) -> None:
    await _seed(persona_store, pid="grantley")
    transport = httpx.MockTransport(_png_handler())
    out = json.loads(
        await dispatch_persona_attach_asset_from_url(
            # No persona_id in args — the model doesn't know its slug.
            args_json=json.dumps(
                {
                    "kind": "reference",
                    "label": "front",
                    "url": "https://example.com/front.png",
                }
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
            bound_persona_id="grantley",
            transport=transport,
        )
    )
    assert out["ok"] is True
    assert out["asset"]["persona_id"] == "grantley"
    assert out["asset"]["kind"] == "reference"
    assert out["asset"]["label"] == "front"


async def test_attach_from_url_explicit_persona_wins_over_bound(
    persona_store, asset_store
) -> None:
    # Wizard path: bound persona is "grantley" but the operator is
    # editing "vivian" explicitly — the explicit id must win.
    await _seed(persona_store, pid="vivian")
    transport = httpx.MockTransport(_png_handler())
    out = json.loads(
        await dispatch_persona_attach_asset_from_url(
            args_json=json.dumps(
                {
                    "persona_id": "vivian",
                    "kind": "reference",
                    "label": "front",
                    "url": "https://example.com/front.png",
                }
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
            bound_persona_id="grantley",
            transport=transport,
        )
    )
    assert out["ok"] is True
    assert out["asset"]["persona_id"] == "vivian"


async def test_attach_from_url_no_persona_unresolved(
    persona_store, asset_store
) -> None:
    transport = httpx.MockTransport(_png_handler())
    out = json.loads(
        await dispatch_persona_attach_asset_from_url(
            args_json=json.dumps(
                {
                    "kind": "reference",
                    "label": "front",
                    "url": "https://example.com/front.png",
                }
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
            bound_persona_id=None,
            transport=transport,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "persona_unresolved"


# ---------------------------------------------------------------------------
# persona_attach_asset_from_data
# ---------------------------------------------------------------------------


def _png_data_uri() -> str:
    import base64

    b64 = base64.b64encode(_PNG_MAGIC).decode("ascii")
    return f"data:image/png;base64,{b64}"


async def test_attach_from_data_data_uri_happy(
    persona_store, asset_store
) -> None:
    await _seed(persona_store, pid="grantley")
    out = json.loads(
        await dispatch_persona_attach_asset_from_data(
            args_json=json.dumps(
                {
                    "kind": "reference",
                    "label": "front",
                    "data": _png_data_uri(),
                }
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
            bound_persona_id="grantley",
        )
    )
    assert out["ok"] is True
    assert out["asset"]["persona_id"] == "grantley"
    assert out["asset"]["mime"] == "image/png"
    assert out["asset"]["sha256"] == hashlib.sha256(_PNG_MAGIC).hexdigest()
    # Bytes round-tripped to disk.
    record = await asset_store.get("grantley", "reference", "front")
    assert record is not None
    assert asset_store.path_for(record).read_bytes() == _PNG_MAGIC


async def test_attach_from_data_bare_base64_sniffs_mime(
    persona_store, asset_store
) -> None:
    import base64

    await _seed(persona_store, pid="grantley")
    bare = base64.b64encode(_PNG_MAGIC).decode("ascii")
    out = json.loads(
        await dispatch_persona_attach_asset_from_data(
            args_json=json.dumps(
                {"kind": "emoji", "label": "happy", "data": bare}
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
            bound_persona_id="grantley",
        )
    )
    assert out["ok"] is True
    # MIME sniffed from the PNG magic bytes (no data-URI media type).
    assert out["asset"]["mime"] == "image/png"


async def test_attach_from_data_bad_base64(
    persona_store, asset_store
) -> None:
    await _seed(persona_store, pid="grantley")
    out = json.loads(
        await dispatch_persona_attach_asset_from_data(
            args_json=json.dumps(
                {
                    "kind": "emoji",
                    "label": "happy",
                    "data": "not!!valid!!base64",
                }
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
            bound_persona_id="grantley",
        )
    )
    assert out["ok"] is False
    assert out["error"] == "invalid_args"


async def test_attach_from_data_no_persona_unresolved(
    persona_store, asset_store
) -> None:
    out = json.loads(
        await dispatch_persona_attach_asset_from_data(
            args_json=json.dumps(
                {
                    "kind": "emoji",
                    "label": "happy",
                    "data": _png_data_uri(),
                }
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
            bound_persona_id=None,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "persona_unresolved"


# ---------------------------------------------------------------------------
# persona_attach_asset_from_attachment
# ---------------------------------------------------------------------------


async def test_attach_from_attachment_happy(
    persona_store, asset_store
) -> None:
    await _seed(persona_store, pid="grantley")
    attachments = [
        Attachment(
            kind="image",
            bytes_=_PNG_MAGIC,
            mime="image/png",
            file_name="selfie.png",
        )
    ]
    out = json.loads(
        await dispatch_persona_attach_asset_from_attachment(
            args_json=json.dumps(
                {"kind": "reference", "label": "front"}
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
            attachments=attachments,
            bound_persona_id="grantley",
        )
    )
    assert out["ok"] is True
    assert out["asset"]["persona_id"] == "grantley"
    assert out["asset"]["file_name"] == "selfie.png"
    assert out["asset"]["mime"] == "image/png"


async def test_attach_from_attachment_index_selects_second_image(
    persona_store, asset_store
) -> None:
    await _seed(persona_store, pid="grantley")
    # A non-image attachment is skipped when indexing over images.
    second = bytes.fromhex("ffd8ffe000104a464946")  # jpeg magic
    attachments = [
        Attachment(kind="file", bytes_=b"doc", mime="text/plain"),
        Attachment(kind="image", bytes_=_PNG_MAGIC, mime="image/png"),
        Attachment(kind="image", bytes_=second, mime="image/jpeg"),
    ]
    out = json.loads(
        await dispatch_persona_attach_asset_from_attachment(
            args_json=json.dumps(
                {
                    "kind": "reference",
                    "label": "side",
                    "attachment_index": 1,
                }
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
            attachments=attachments,
            bound_persona_id="grantley",
        )
    )
    assert out["ok"] is True
    assert out["asset"]["sha256"] == hashlib.sha256(second).hexdigest()


async def test_attach_from_attachment_url_only_bounces(
    persona_store, asset_store
) -> None:
    # A channel that forwarded only a URL (no inline bytes) cannot be
    # ingested in-band — the tool tells the model to use the URL tool.
    await _seed(persona_store, pid="grantley")
    attachments = [
        Attachment(kind="image", url="https://cdn.example.com/pic.png")
    ]
    out = json.loads(
        await dispatch_persona_attach_asset_from_attachment(
            args_json=json.dumps(
                {"kind": "reference", "label": "front"}
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
            attachments=attachments,
            bound_persona_id="grantley",
        )
    )
    assert out["ok"] is False
    assert out["error"] == "attachment_not_ingestible"
    assert "cdn.example.com/pic.png" in out["message"]


async def test_attach_from_attachment_none_present(
    persona_store, asset_store
) -> None:
    await _seed(persona_store, pid="grantley")
    out = json.loads(
        await dispatch_persona_attach_asset_from_attachment(
            args_json=json.dumps(
                {"kind": "reference", "label": "front"}
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
            attachments=[],
            bound_persona_id="grantley",
        )
    )
    assert out["ok"] is False
    assert out["error"] == "no_attachment"


async def test_attach_from_attachment_index_out_of_range(
    persona_store, asset_store
) -> None:
    await _seed(persona_store, pid="grantley")
    attachments = [
        Attachment(kind="image", bytes_=_PNG_MAGIC, mime="image/png")
    ]
    out = json.loads(
        await dispatch_persona_attach_asset_from_attachment(
            args_json=json.dumps(
                {
                    "kind": "reference",
                    "label": "front",
                    "attachment_index": 5,
                }
            ).encode(),
            persona_store=persona_store,
            asset_store=asset_store,
            attachments=attachments,
            bound_persona_id="grantley",
        )
    )
    assert out["ok"] is False
    assert out["error"] == "attachment_index_out_of_range"
