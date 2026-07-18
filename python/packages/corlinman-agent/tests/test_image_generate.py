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


def _ok_handler(*, expected_model: str | None = None):
    """Return a mock OpenAI Responses handler that asserts no refs
    are sent and returns ``_GENERATED_PNG`` base64-encoded."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/responses" in str(request.url)
        body = json.loads(request.content.decode())
        if expected_model is not None:
            assert body["model"] == expected_model
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
    assert out_path.parent == tmp_path / "workspace" / "generated"
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


async def test_model_override_reaches_openai_request_body(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    transport = httpx.MockTransport(
        _ok_handler(expected_model="persona-image-model")
    )
    out = json.loads(
        await dispatch_image_generate(
            args_json=json.dumps({"prompt": "anything"}).encode(),
            provider=_fake_provider(),
            model_override="persona-image-model",
            transport=transport,
        )
    )
    assert out["ok"] is True


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


# ---------------------------------------------------------------------------
# Fast-fail contract: unrecoverable errors + endpoint cooldown breaker.
# ---------------------------------------------------------------------------


def _provider_at(base_url: str) -> SimpleNamespace:
    return SimpleNamespace(_api_key="sk-test", _base_url=base_url, name="openai")


async def test_auth_reject_is_unrecoverable_and_trips_breaker(
    tmp_path, monkeypatch
) -> None:
    from corlinman_agent.image import generate as gen

    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(gen, "_UNAVAILABLE_UNTIL", {})
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": "bad key"})

    transport = httpx.MockTransport(handler)
    provider = _provider_at("https://img-a.test/v1")
    out = json.loads(
        await dispatch_image_generate(
            args_json=json.dumps({"prompt": "hi"}).encode(),
            provider=provider,
            transport=transport,
        )
    )
    assert out["ok"] is False
    assert "image_generation_unavailable" in out["message"]
    assert "不要重试" in out["message"]
    assert calls["n"] == 1

    # Second call fails instantly from the breaker — no HTTP hit.
    out2 = json.loads(
        await dispatch_image_generate(
            args_json=json.dumps({"prompt": "hi"}).encode(),
            provider=provider,
            transport=transport,
        )
    )
    assert out2["ok"] is False
    assert "image_generation_unavailable" in out2["message"]
    assert calls["n"] == 1


async def test_connect_error_is_unrecoverable(tmp_path, monkeypatch) -> None:
    from corlinman_agent.image import generate as gen

    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(gen, "_UNAVAILABLE_UNTIL", {})

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    transport = httpx.MockTransport(handler)
    out = json.loads(
        await dispatch_image_generate(
            args_json=json.dumps({"prompt": "hi"}).encode(),
            provider=_provider_at("https://img-b.test/v1"),
            transport=transport,
        )
    )
    assert out["ok"] is False
    assert "image_generation_unavailable" in out["message"]
    assert "不要重试" in out["message"]


async def test_http_500_stays_retryable(tmp_path, monkeypatch) -> None:
    """5xx is transient — must NOT trip the breaker or carry the hint."""
    from corlinman_agent.image import generate as gen

    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(gen, "_UNAVAILABLE_UNTIL", {})
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, json={"error": "boom"})

    transport = httpx.MockTransport(handler)
    provider = _provider_at("https://img-c.test/v1")
    for _ in range(2):
        out = json.loads(
            await dispatch_image_generate(
                args_json=json.dumps({"prompt": "hi"}).encode(),
                provider=provider,
                transport=transport,
            )
        )
        assert out["ok"] is False
        assert "不要重试" not in out["message"]
    assert calls["n"] == 2


async def test_success_clears_breaker_state(tmp_path, monkeypatch) -> None:
    from corlinman_agent.image import generate as gen

    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    root = "https://img-d.test/v1"
    monkeypatch.setattr(
        gen, "_UNAVAILABLE_UNTIL", {root: (float("inf"), "stale")}
    )
    # Expired/stale entries block until TTL — simulate expiry instead.
    gen._UNAVAILABLE_UNTIL[root] = (0.0, "stale")
    transport = httpx.MockTransport(_ok_handler())
    out = json.loads(
        await dispatch_image_generate(
            args_json=json.dumps({"prompt": "hi"}).encode(),
            provider=_provider_at(root),
            transport=transport,
        )
    )
    assert out["ok"] is True
    assert root not in gen._UNAVAILABLE_UNTIL


def test_default_timeout_is_five_minutes() -> None:
    from corlinman_agent.image import generate as gen

    assert gen._DEFAULT_TIMEOUT_SECS == 300.0
