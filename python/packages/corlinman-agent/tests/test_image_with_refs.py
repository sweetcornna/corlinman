"""Tests for the ``image_with_refs`` builtin tool (W4).

Network is mocked via :class:`httpx.MockTransport` — the OpenAI
Responses endpoint returns a tiny canned ``image_generation_call`` body
with a base64-encoded PNG so the dispatcher can complete the full
pipeline (resolve persona → match refs → call provider → write file).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from corlinman_agent.image import (
    IMAGE_WITH_REFS_TOOL,
    dispatch_image_with_refs,
    image_with_refs_tool_schema,
)
from corlinman_server.persona import (
    Persona,
    PersonaAssetStore,
    PersonaStore,
)

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
    s = await PersonaAssetStore.open(
        tmp_path / "persona_assets.sqlite",
        tmp_path / "personas",
    )
    try:
        yield s
    finally:
        await s.close()


@pytest.fixture
def fake_provider():
    """``SimpleNamespace`` exposing the same private attributes
    :class:`OpenAIProvider` carries so :func:`generate_with_refs` reads
    the credential without needing a real provider adapter."""
    return SimpleNamespace(_api_key="sk-test", _base_url=None, name="openai")


# Canonical mock PNG body the OpenAI Responses endpoint returns; tests
# assert the dispatcher writes EXACTLY these bytes to workspace/generated.
_GENERATED_PNG = b"\x89PNG\r\n\x1a\n" + b"FAKEIMAGEDATA" * 8


def _responses_handler(body_payload: dict | None = None):
    """Build a handler that emulates the OpenAI Responses API shape."""
    if body_payload is None:
        body_payload = {
            "output": [
                {
                    "type": "image_generation_call",
                    "result": base64.b64encode(_GENERATED_PNG).decode(
                        "ascii"
                    ),
                }
            ]
        }

    def handler(request: httpx.Request) -> httpx.Response:
        # Sanity-check the endpoint shape — the dispatcher MUST hit the
        # ``/responses`` path with a JSON body carrying the prompt +
        # one ``input_image`` per reference.
        assert "/responses" in str(request.url)
        try:
            body = json.loads(request.content.decode())
        except Exception as exc:  # noqa: BLE001 — surface as test failure
            raise AssertionError(f"non-json request body: {exc}") from exc
        assert "input" in body
        return httpx.Response(200, json=body_payload)

    return handler


async def _seed_persona_with_refs(
    persona_store: PersonaStore,
    asset_store: PersonaAssetStore,
    *,
    pid: str = "kawaii",
    labels: tuple[str, ...] = ("front", "side"),
) -> None:
    now = _now_ms()
    await persona_store.create(
        Persona(
            id=pid,
            display_name="Kawaii Cat",
            short_summary="",
            system_prompt="cat",
            is_builtin=False,
            created_at_ms=now,
            updated_at_ms=now,
        )
    )
    for i, label in enumerate(labels):
        await asset_store.put(
            pid,
            "reference",
            label,
            bytes_=_PNG_MAGIC + bytes([i]),
            mime="image/png",
            file_name=f"{label}.png",
        )


# ---------------------------------------------------------------------------
# Schema + wire stability
# ---------------------------------------------------------------------------


def test_tool_name_wire_stable() -> None:
    assert IMAGE_WITH_REFS_TOOL == "image_with_refs"


def test_schema_openai_shape() -> None:
    schema = image_with_refs_tool_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "image_with_refs"
    assert "prompt" in schema["function"]["parameters"]["properties"]
    assert "characters" in schema["function"]["parameters"]["properties"]
    assert "aspect_ratio" in schema["function"]["parameters"]["properties"]
    assert "persona_id" in schema["function"]["parameters"]["properties"]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_happy_path_writes_workspace_png(
    persona_store, asset_store, fake_provider, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    await _seed_persona_with_refs(persona_store, asset_store)

    transport = httpx.MockTransport(_responses_handler())
    out = json.loads(
        await dispatch_image_with_refs(
            args_json=json.dumps(
                {
                    "prompt": "cat sipping tea, watercolour",
                    "characters": ["front", "side"],
                    "aspect_ratio": "square",
                    "persona_id": "kawaii",
                }
            ).encode(),
            provider=fake_provider,
            persona_store=persona_store,
            asset_store=asset_store,
            transport=transport,
        )
    )
    assert out["ok"] is True
    assert out["mime"] == "image/png"
    assert out["chars_used"] == ["front", "side"]
    assert out["chars_missing"] == []
    assert out["persona_id"] == "kawaii"
    out_path = Path(out["path"])
    assert out_path.is_file()
    # Lands inside workspace/generated under the data dir.
    assert "workspace/generated" in str(out_path)
    assert out_path.suffix == ".png"
    # Bytes match what the mock returned.
    assert out_path.read_bytes() == _GENERATED_PNG
    assert out["size_bytes"] == len(_GENERATED_PNG)


async def test_explicit_persona_id_wins_over_bound(
    persona_store, asset_store, fake_provider, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    await _seed_persona_with_refs(persona_store, asset_store, pid="kawaii")
    await _seed_persona_with_refs(
        persona_store, asset_store, pid="grumpy", labels=("front",)
    )
    transport = httpx.MockTransport(_responses_handler())
    out = json.loads(
        await dispatch_image_with_refs(
            args_json=json.dumps(
                {
                    "prompt": "hi",
                    "characters": ["front"],
                    "persona_id": "grumpy",
                }
            ).encode(),
            provider=fake_provider,
            persona_store=persona_store,
            asset_store=asset_store,
            bound_persona_id="kawaii",  # bound != explicit
            transport=transport,
        )
    )
    assert out["ok"] is True
    assert out["persona_id"] == "grumpy"


async def test_bound_persona_used_when_no_explicit_id(
    persona_store, asset_store, fake_provider, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    await _seed_persona_with_refs(persona_store, asset_store, pid="kawaii")
    transport = httpx.MockTransport(_responses_handler())
    out = json.loads(
        await dispatch_image_with_refs(
            args_json=json.dumps(
                {"prompt": "hi", "characters": ["front"]}
            ).encode(),
            provider=fake_provider,
            persona_store=persona_store,
            asset_store=asset_store,
            bound_persona_id="kawaii",
            transport=transport,
        )
    )
    assert out["ok"] is True
    assert out["persona_id"] == "kawaii"


# ---------------------------------------------------------------------------
# Error envelopes
# ---------------------------------------------------------------------------


async def test_persona_unresolved(
    persona_store, asset_store, fake_provider
) -> None:
    out = json.loads(
        await dispatch_image_with_refs(
            args_json=json.dumps(
                {"prompt": "hi", "characters": ["front"]}
            ).encode(),
            provider=fake_provider,
            persona_store=persona_store,
            asset_store=asset_store,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "persona_unresolved"


async def test_persona_not_found(
    persona_store, asset_store, fake_provider
) -> None:
    out = json.loads(
        await dispatch_image_with_refs(
            args_json=json.dumps(
                {
                    "prompt": "hi",
                    "characters": ["front"],
                    "persona_id": "ghost",
                }
            ).encode(),
            provider=fake_provider,
            persona_store=persona_store,
            asset_store=asset_store,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "persona_not_found"


async def test_no_refs_resolved(
    persona_store, asset_store, fake_provider, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    await _seed_persona_with_refs(persona_store, asset_store, labels=("front",))
    out = json.loads(
        await dispatch_image_with_refs(
            args_json=json.dumps(
                {
                    "prompt": "hi",
                    "characters": ["nonexistent"],
                    "persona_id": "kawaii",
                }
            ).encode(),
            provider=fake_provider,
            persona_store=persona_store,
            asset_store=asset_store,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "no_refs_resolved"


async def test_partial_match_succeeds(
    persona_store, asset_store, fake_provider, tmp_path, monkeypatch
) -> None:
    """When one of the requested labels matches and one doesn't, the
    dispatcher proceeds with the matching label and reports the missing
    one in ``chars_missing``."""
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    await _seed_persona_with_refs(persona_store, asset_store, labels=("front",))
    transport = httpx.MockTransport(_responses_handler())
    out = json.loads(
        await dispatch_image_with_refs(
            args_json=json.dumps(
                {
                    "prompt": "hi",
                    "characters": ["front", "side"],
                    "persona_id": "kawaii",
                }
            ).encode(),
            provider=fake_provider,
            persona_store=persona_store,
            asset_store=asset_store,
            transport=transport,
        )
    )
    assert out["ok"] is True
    assert out["chars_used"] == ["front"]
    assert out["chars_missing"] == ["side"]


async def test_invalid_prompt(persona_store, asset_store, fake_provider) -> None:
    out = json.loads(
        await dispatch_image_with_refs(
            args_json=json.dumps(
                {"prompt": "  ", "characters": ["x"]}
            ).encode(),
            provider=fake_provider,
            persona_store=persona_store,
            asset_store=asset_store,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "invalid_args"


async def test_invalid_aspect_ratio(
    persona_store, asset_store, fake_provider
) -> None:
    out = json.loads(
        await dispatch_image_with_refs(
            args_json=json.dumps(
                {
                    "prompt": "hi",
                    "characters": ["front"],
                    "persona_id": "kawaii",
                    "aspect_ratio": "panorama",
                }
            ).encode(),
            provider=fake_provider,
            persona_store=persona_store,
            asset_store=asset_store,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "invalid_args"


async def test_provider_no_api_key(
    persona_store, asset_store, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    await _seed_persona_with_refs(persona_store, asset_store, labels=("front",))
    keyless_provider = SimpleNamespace(_api_key=None, _base_url=None)
    transport = httpx.MockTransport(_responses_handler())
    out = json.loads(
        await dispatch_image_with_refs(
            args_json=json.dumps(
                {
                    "prompt": "hi",
                    "characters": ["front"],
                    "persona_id": "kawaii",
                }
            ).encode(),
            provider=keyless_provider,
            persona_store=persona_store,
            asset_store=asset_store,
            transport=transport,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "provider_unavailable"


async def test_openai_http_500(
    persona_store, asset_store, fake_provider, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    await _seed_persona_with_refs(persona_store, asset_store, labels=("front",))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    transport = httpx.MockTransport(handler)
    out = json.loads(
        await dispatch_image_with_refs(
            args_json=json.dumps(
                {
                    "prompt": "hi",
                    "characters": ["front"],
                    "persona_id": "kawaii",
                }
            ).encode(),
            provider=fake_provider,
            persona_store=persona_store,
            asset_store=asset_store,
            transport=transport,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "image_generation_failed"


async def test_response_missing_image(
    persona_store, asset_store, fake_provider, tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    await _seed_persona_with_refs(persona_store, asset_store, labels=("front",))
    transport = httpx.MockTransport(_responses_handler(body_payload={"output": []}))
    out = json.loads(
        await dispatch_image_with_refs(
            args_json=json.dumps(
                {
                    "prompt": "hi",
                    "characters": ["front"],
                    "persona_id": "kawaii",
                }
            ).encode(),
            provider=fake_provider,
            persona_store=persona_store,
            asset_store=asset_store,
            transport=transport,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "image_generation_failed"
