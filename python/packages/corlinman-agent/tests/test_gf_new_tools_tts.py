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


def _ok_handler():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/audio/speech" in str(request.url)
        body = json.loads(request.content.decode())
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


def _provider():
    return SimpleNamespace(_api_key="sk-test", _base_url=None, name="openai")


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
