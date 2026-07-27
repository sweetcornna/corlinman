"""Operator config must reach the tool, not just the admin audition.

``text_to_speech`` runs in the agent process and never sees the gateway
config snapshot, so a ``[voice]`` block only takes effect if it is pushed
into this process explicitly. These tests pin that: what the operator
picks in the UI is what the ``text_to_speech`` tool sends, and it wins
over stale ``CORLINMAN_TTS_*`` exports on the host.

Without this wiring the settings page would be decorative — preview would
honour the choice while every channel kept using the built-in default.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest
from corlinman_agent.image.tts import dispatch_text_to_speech
from corlinman_agent.voice import (
    apply_voice_config,
    get_voice_defaults,
    reset_custom_backends,
    reset_voice_defaults,
    voice_defaults_from_config,
)

_AUDIO = b"ID3\x04\x00FAKE" * 8


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    reset_voice_defaults()
    reset_custom_backends()
    yield
    reset_voice_defaults()
    reset_custom_backends()


def _capture():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(200, content=_AUDIO)

    return seen, httpx.MockTransport(handler)


async def _speak(transport, tmp_path, monkeypatch, **params):
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    return json.loads(
        await dispatch_text_to_speech(
            args_json=json.dumps({"text": "你好"}).encode(),
            provider_params=params or None,
            transport=transport,
        )
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_config_block_parses_into_defaults() -> None:
    d = voice_defaults_from_config(
        {
            "enabled": True,
            "backend": "gemini",
            "voice": "Kore",
            "model": "gemini-2.5-pro-preview-tts",
            "format": "wav",
            "instructions": "慢一点",
            "speed": 1.25,
        }
    )
    assert (d.backend, d.voice, d.fmt, d.speed) == ("gemini", "Kore", "wav", 1.25)
    assert d.instructions == "慢一点"


def test_absent_or_malformed_block_is_inert() -> None:
    assert voice_defaults_from_config(None).backend == ""
    assert voice_defaults_from_config({"speed": "fast"}).speed is None


def test_apply_installs_custom_backends_and_defaults() -> None:
    defaults = apply_voice_config(
        {
            "backend": "acme",
            "voice": "lin",
            "backends": {
                "acme": {
                    "base_url": "https://acme.test",
                    "models": ["v2"],
                    "voices": [{"id": "lin", "label": "小林"}],
                    "http": {"path": "/say", "body": {"q": "{text}"}},
                }
            },
        }
    )
    assert defaults.backend == "acme"
    assert get_voice_defaults().voice == "lin"


# ---------------------------------------------------------------------------
# The tool actually honours them
# ---------------------------------------------------------------------------


async def test_configured_backend_and_voice_reach_the_tool(tmp_path, monkeypatch) -> None:
    apply_voice_config({"backend": "openai", "voice": "nova", "format": "wav"})
    seen, transport = _capture()
    env = await _speak(transport, tmp_path, monkeypatch, api_key="k")
    assert env["ok"] is True
    assert env["backend"] == "openai"
    assert seen["body"]["voice"] == "nova"
    assert seen["body"]["response_format"] == "wav"


async def test_configured_custom_backend_is_usable_by_the_tool(
    tmp_path, monkeypatch
) -> None:
    """A provider defined only in config must work for the model too."""
    apply_voice_config(
        {
            "backend": "acme",
            "voice": "lin",
            "backends": {
                "acme": {
                    "base_url": "https://acme.test",
                    "models": ["v2"],
                    "voices": [{"id": "lin", "label": "小林"}],
                    "formats": ["mp3"],
                    "http": {
                        "path": "/say",
                        "body": {"q": "{text}", "spk": "{voice}"},
                    },
                }
            },
        }
    )
    seen, transport = _capture()
    env = await _speak(transport, tmp_path, monkeypatch, api_key="tok")
    assert env["backend"] == "acme"
    assert seen["url"] == "https://acme.test/say"
    assert seen["body"] == {"q": "你好", "spk": "lin"}


async def test_config_beats_a_stale_env_var(tmp_path, monkeypatch) -> None:
    """The UI must not be silently overridden by an old host export."""
    monkeypatch.setenv("CORLINMAN_TTS_VOICE", "echo")
    monkeypatch.setenv("CORLINMAN_TTS_BACKEND", "fish")
    apply_voice_config({"backend": "openai", "voice": "shimmer"})
    seen, transport = _capture()
    env = await _speak(transport, tmp_path, monkeypatch, api_key="k")
    assert env["backend"] == "openai"
    assert seen["body"]["voice"] == "shimmer"


async def test_env_still_applies_when_config_is_silent(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CORLINMAN_TTS_VOICE", "echo")
    apply_voice_config({})
    seen, transport = _capture()
    await _speak(transport, tmp_path, monkeypatch, api_key="k")
    assert seen["body"]["voice"] == "echo"


async def test_persona_binding_still_outranks_global_config(
    tmp_path, monkeypatch
) -> None:
    """Per-persona bindings stay the most specific setting."""
    apply_voice_config({"backend": "openai", "voice": "shimmer"})
    seen, transport = _capture()
    env = await _speak(
        transport, tmp_path, monkeypatch, api_key="k", voice="onyx"
    )
    assert env["backend"] == "openai"
    assert seen["body"]["voice"] == "onyx"


async def test_explicit_tool_argument_outranks_everything(
    tmp_path, monkeypatch
) -> None:
    apply_voice_config({"backend": "openai", "voice": "shimmer"})
    seen, transport = _capture()
    out = await dispatch_text_to_speech(
        args_json=json.dumps({"text": "hi", "voice": "fable"}).encode(),
        provider_params={"api_key": "k"},
        transport=transport,
    )
    assert json.loads(out)["ok"] is True
    assert seen["body"]["voice"] == "fable"
