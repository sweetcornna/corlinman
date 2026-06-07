"""gap-fill lane-new-tools — ``text_to_speech`` dispatch.

Covers gap ``text-to-speech-tool``: an agent-callable TTS tool that
synthesises audio via the OpenAI ``/audio/speech`` fallback and writes
the result under the workspace ``generated`` dir, degrading gracefully
when no credential is reachable.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from corlinman_agent.image.tts import (
    TEXT_TO_SPEECH_TOOL,
    dispatch_text_to_speech,
    text_to_speech_tool_schema,
)

_AUDIO = b"ID3\x04\x00FAKEMP3PAYLOAD" * 4


def _ok_handler(*, expected_model: str | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/audio/speech" in str(request.url)
        body = json.loads(request.content.decode())
        if expected_model is not None:
            assert body["model"] == expected_model
        assert body["input"]
        assert body["voice"] in (
            "alloy",
            "echo",
            "fable",
            "onyx",
            "nova",
            "shimmer",
        )
        return httpx.Response(200, content=_AUDIO)

    return handler


def _fish_handler(*, expected_model: str = "s2-pro"):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.fish.audio/v1/tts"
        assert request.headers["authorization"] == "Bearer fish-key"
        assert request.headers["model"] == expected_model
        body = json.loads(request.content.decode())
        assert body == {
            "text": "hello fish",
            "reference_id": "voice-123",
            "format": "mp3",
        }
        return httpx.Response(200, content=_AUDIO)

    return handler


def _provider():
    return SimpleNamespace(_api_key="sk-test", _base_url=None, name="openai")


def _fish_provider():
    return SimpleNamespace(
        _api_key="fish-key",
        _base_url="https://api.fish.audio",
        name="fish",
    )


def test_tool_name_wire_stable() -> None:
    assert TEXT_TO_SPEECH_TOOL == "text_to_speech"


def test_schema_shape() -> None:
    schema = text_to_speech_tool_schema()
    assert schema["function"]["name"] == "text_to_speech"
    props = schema["function"]["parameters"]["properties"]
    assert "text" in props
    assert schema["function"]["parameters"]["required"] == ["text"]


async def test_happy_path_writes_audio(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    transport = httpx.MockTransport(_ok_handler())
    out = await dispatch_text_to_speech(
        args_json=json.dumps({"text": "hello world", "voice": "nova"}).encode(),
        provider=_provider(),
        transport=transport,
    )
    env = json.loads(out)
    assert env["ok"] is True
    assert env["kind"] == "audio"
    assert env["mime"] == "audio/mpeg"
    assert env["backend"] == "openai"
    path = Path(env["path"])
    assert path.exists()
    assert path.read_bytes() == _AUDIO
    # Lives under the shared workspace/generated dir send_attachment walks.
    assert path.parent == tmp_path / "workspace" / "generated"


async def test_format_override_changes_extension(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    transport = httpx.MockTransport(_ok_handler())
    out = await dispatch_text_to_speech(
        args_json=json.dumps({"text": "hi", "format": "wav"}).encode(),
        provider=_provider(),
        transport=transport,
    )
    env = json.loads(out)
    assert env["ok"] is True
    assert env["mime"] == "audio/wav"
    assert Path(env["path"]).suffix == ".wav"


async def test_model_override_reaches_speech_request_body(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    transport = httpx.MockTransport(
        _ok_handler(expected_model="persona-voice-model")
    )
    out = await dispatch_text_to_speech(
        args_json=json.dumps({"text": "hi"}).encode(),
        provider=_provider(),
        model_override="persona-voice-model",
        transport=transport,
    )
    env = json.loads(out)
    assert env["ok"] is True


async def test_fish_backend_posts_text_to_v1_tts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    transport = httpx.MockTransport(_fish_handler())

    out = await dispatch_text_to_speech(
        args_json=json.dumps({"text": "hello fish"}).encode(),
        provider=_fish_provider(),
        model_override="s2-pro",
        provider_params={
            "tts_backend": "fish",
            "reference_id": "voice-123",
            "format": "mp3",
        },
        transport=transport,
    )

    env = json.loads(out)
    assert env["ok"] is True
    assert env["backend"] == "fish"
    assert env["reference_id"] == "voice-123"
    assert env["voice"] == "voice-123"
    assert env["mime"] == "audio/mpeg"
    assert Path(env["path"]).read_bytes() == _AUDIO


async def test_fish_backend_prefers_fish_env_over_openai_fallback_key(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "fish-env-key")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer fish-env-key"
        return httpx.Response(200, content=_AUDIO)

    out = await dispatch_text_to_speech(
        args_json=json.dumps({"text": "hello fish"}).encode(),
        provider=SimpleNamespace(
            _api_key="openai-secret",
            _base_url="https://api.fish.audio",
            name="fish",
        ),
        model_override="s2-pro",
        provider_params={
            "tts_backend": "fish",
            "reference_id": "voice-123",
        },
        transport=httpx.MockTransport(handler),
    )

    env = json.loads(out)
    assert env["ok"] is True
    assert env["backend"] == "fish"


async def test_fish_backend_clamps_unsupported_global_format(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        assert body["format"] == "mp3"
        return httpx.Response(200, content=_AUDIO)

    out = await dispatch_text_to_speech(
        args_json=json.dumps({"text": "hello fish", "format": "aac"}).encode(),
        provider=_fish_provider(),
        model_override="s2-pro",
        provider_params={
            "tts_backend": "fish",
            "reference_id": "voice-123",
        },
        transport=httpx.MockTransport(handler),
    )

    env = json.loads(out)
    assert env["ok"] is True
    assert env["mime"] == "audio/mpeg"
    assert Path(env["path"]).suffix == ".mp3"


async def test_fish_backend_requires_reference_id(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CORLINMAN_TTS_REFERENCE_ID", raising=False)
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))

    out = await dispatch_text_to_speech(
        args_json=json.dumps({"text": "hello"}).encode(),
        provider=_fish_provider(),
        provider_params={"tts_backend": "fish"},
    )

    env = json.loads(out)
    assert env["ok"] is False
    assert env["error"] == "tts_unavailable"
    assert "reference_id" in env["message"]


async def test_missing_text() -> None:
    out = await dispatch_text_to_speech(
        args_json=b"{}", provider=_provider()
    )
    env = json.loads(out)
    assert env["ok"] is False
    assert env["error"] == "invalid_args"


async def test_unavailable_without_key(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    out = await dispatch_text_to_speech(
        args_json=json.dumps({"text": "hello"}).encode(),
        provider=SimpleNamespace(),  # no api_key anywhere
    )
    env = json.loads(out)
    assert env["ok"] is False
    assert env["error"] == "tts_unavailable"


async def test_http_500_graceful(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))

    def boom(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream exploded")

    out = await dispatch_text_to_speech(
        args_json=json.dumps({"text": "hello"}).encode(),
        provider=_provider(),
        transport=httpx.MockTransport(boom),
    )
    env = json.loads(out)
    assert env["ok"] is False
    # RuntimeError raised on >=400 → tts_unavailable envelope.
    assert env["error"] == "tts_unavailable"
