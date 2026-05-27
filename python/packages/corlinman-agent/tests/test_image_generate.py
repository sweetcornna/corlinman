"""Tests for the ``image_generate`` builtin tool — plain generation.

Sibling of :mod:`tests.test_image_with_refs`. Validates that the
plain dispatcher writes a PNG to the workspace without ever touching
a persona store or asset store, and that the request body sent to
OpenAI carries no ``input_image`` parts (proves the isolation from
``image_with_refs`` is structural).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from corlinman_agent.image import (
    IMAGE_GENERATE_TOOL,
    dispatch_image_generate,
    image_generate_tool_schema,
)


_GENERATED_PNG = b"\x89PNG\r\n\x1a\n" + b"PLAINIMG" * 8


def _ok_handler():
    """Return a mock OpenAI Responses handler that asserts no refs
    are sent and returns ``_GENERATED_PNG`` base64-encoded."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/responses" in str(request.url)
        body = json.loads(request.content.decode())
        # Single user message, single content part, no input_image.
        assert isinstance(body.get("input"), list) and len(body["input"]) == 1
        content = body["input"][0]["content"]
        assert isinstance(content, list) and len(content) == 1
        assert content[0]["type"] == "input_text"
        for part in content:
            assert part["type"] != "input_image", (
                "image_generate must not send reference images"
            )
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "image_generation_call",
                        "result": base64.b64encode(_GENERATED_PNG).decode(
                            "ascii"
                        ),
                    }
                ]
            },
        )

    return handler


def _fake_provider():
    return SimpleNamespace(_api_key="sk-test", _base_url=None, name="openai")


# ---------------------------------------------------------------------------
# Schema + wire stability
# ---------------------------------------------------------------------------


def test_tool_name_wire_stable() -> None:
    assert IMAGE_GENERATE_TOOL == "image_generate"


def test_schema_openai_shape() -> None:
    schema = image_generate_tool_schema()
    assert schema["type"] == "function"
    assert schema["function"]["name"] == "image_generate"
    props = schema["function"]["parameters"]["properties"]
    assert "prompt" in props
    assert "aspect_ratio" in props
    # Isolation guard: the plain tool MUST NOT advertise persona /
    # reference fields, or the model will pass them and bypass the
    # dedicated image_with_refs surface.
    assert "characters" not in props
    assert "persona_id" not in props
    assert schema["function"]["parameters"]["required"] == ["prompt"]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_happy_path_writes_workspace_png(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    transport = httpx.MockTransport(_ok_handler())
    out = json.loads(
        await dispatch_image_generate(
            args_json=json.dumps(
                {
                    "prompt": "a watercolour cat napping on a windowsill",
                    "aspect_ratio": "portrait",
                }
            ).encode(),
            provider=_fake_provider(),
            transport=transport,
        )
    )
    assert out["ok"] is True
    assert out["mime"] == "image/png"
    assert out["aspect_ratio"] == "portrait"
    out_path = Path(out["path"])
    assert out_path.is_file()
    assert "workspace/generated" in str(out_path)
    assert out_path.suffix == ".png"
    assert out_path.read_bytes() == _GENERATED_PNG
    assert out["size_bytes"] == len(_GENERATED_PNG)


async def test_aspect_ratio_defaults_to_square(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    transport = httpx.MockTransport(_ok_handler())
    out = json.loads(
        await dispatch_image_generate(
            args_json=json.dumps({"prompt": "anything"}).encode(),
            provider=_fake_provider(),
            transport=transport,
        )
    )
    assert out["ok"] is True
    assert out["aspect_ratio"] == "square"


async def test_no_persona_wiring_required(tmp_path, monkeypatch) -> None:
    """Isolation guard: dispatch_image_generate has no
    persona_store / asset_store parameters. The call signature itself
    proves the plain path cannot reach the persona surface."""
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    transport = httpx.MockTransport(_ok_handler())
    # Note: kwargs explicitly enumerated to lock the signature.
    out = json.loads(
        await dispatch_image_generate(
            args_json=json.dumps({"prompt": "no persona here"}).encode(),
            provider=_fake_provider(),
            transport=transport,
        )
    )
    assert out["ok"] is True


# ---------------------------------------------------------------------------
# Error envelopes
# ---------------------------------------------------------------------------


async def test_missing_prompt() -> None:
    out = json.loads(
        await dispatch_image_generate(
            args_json=json.dumps({}).encode(),
            provider=_fake_provider(),
        )
    )
    assert out["ok"] is False
    assert out["error"] == "invalid_args"


async def test_empty_prompt() -> None:
    out = json.loads(
        await dispatch_image_generate(
            args_json=json.dumps({"prompt": "  "}).encode(),
            provider=_fake_provider(),
        )
    )
    assert out["ok"] is False
    assert out["error"] == "invalid_args"


async def test_invalid_aspect_ratio() -> None:
    out = json.loads(
        await dispatch_image_generate(
            args_json=json.dumps(
                {"prompt": "hi", "aspect_ratio": "panorama"}
            ).encode(),
            provider=_fake_provider(),
        )
    )
    assert out["ok"] is False
    assert out["error"] == "invalid_args"


async def test_malformed_args_json() -> None:
    out = json.loads(
        await dispatch_image_generate(
            args_json=b"{not valid json",
            provider=_fake_provider(),
        )
    )
    assert out["ok"] is False
    assert out["error"] == "invalid_args"


async def test_provider_no_api_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    keyless = SimpleNamespace(_api_key=None, _base_url=None)
    out = json.loads(
        await dispatch_image_generate(
            args_json=json.dumps({"prompt": "hi"}).encode(),
            provider=keyless,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "provider_unavailable"


async def test_openai_http_500(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    transport = httpx.MockTransport(handler)
    out = json.loads(
        await dispatch_image_generate(
            args_json=json.dumps({"prompt": "hi"}).encode(),
            provider=_fake_provider(),
            transport=transport,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "image_generation_failed"


async def test_response_missing_image(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output": []})

    transport = httpx.MockTransport(handler)
    out = json.loads(
        await dispatch_image_generate(
            args_json=json.dumps({"prompt": "hi"}).encode(),
            provider=_fake_provider(),
            transport=transport,
        )
    )
    assert out["ok"] is False
    assert out["error"] == "image_generation_failed"
