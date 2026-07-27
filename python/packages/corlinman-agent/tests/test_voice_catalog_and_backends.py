"""Voice backend registry, templating engine and GPT-Live negotiation.

The templating engine is the load-bearing piece: every vendor we support
(and every user-defined backend) is a row of data run through
``synthesize_http``, so these tests pin the substitution rules that keep
those rows honest — typed placeholders, dropped optionals, nested bodies,
auth placement, and base64-in-JSON extraction.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from corlinman_agent.voice import (
    SynthesisError,
    SynthesisRequest,
    all_backends,
    get_backend,
    normalize_backend,
    register_backends_from_config,
    reset_custom_backends,
    resolve_format,
    resolve_voice,
    synthesize,
)
from corlinman_agent.voice.gpt_live import (
    _answer_sdp,
    _classify_live_error,
    _negotiate,
    build_session,
)
from corlinman_agent.voice.synth import _live_base_url, resolve_credentials

_AUDIO = b"ID3\x04\x00FAKE" * 8


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_custom_backends()
    yield
    reset_custom_backends()


def _capture():
    """A MockTransport that records the last request it saw."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content) if request.content else None
        return httpx.Response(200, content=_AUDIO)

    return seen, httpx.MockTransport(handler)


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------


def test_builtin_backends_registered() -> None:
    ids = [b.id for b in all_backends()]
    assert ids[:6] == ["gpt_live", "openai", "fish", "elevenlabs", "gemini", "minimax"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("gpt-live", "gpt_live"),
        ("GPT_LIVE", "gpt_live"),
        ("realtime", "gpt_live"),
        ("fish-audio", "fish"),
        ("openai_compatible", "openai"),
        ("11labs", "elevenlabs"),
        ("", "openai"),
        (None, "openai"),
    ],
)
def test_backend_alias_folding(raw: str | None, expected: str) -> None:
    assert normalize_backend(raw) == expected


def test_unknown_backend_is_not_silently_coerced() -> None:
    # It must survive normalization so the caller can report it by name.
    assert normalize_backend("acme_tts") == "acme_tts"
    assert get_backend("acme_tts") is None


def test_openai_default_voice_is_universally_supported() -> None:
    # marin/cedar only exist on gpt-4o-mini-tts; defaulting to them would
    # 400 for anyone still pinned to tts-1.
    assert get_backend("openai").default_voice == "alloy"
    recommended = {v.id for v in get_backend("openai").voices if v.recommended}
    assert recommended == {"marin", "cedar"}


def test_gpt_live_catalog_matches_shipped_voice_set() -> None:
    voices = {v.id for v in get_backend("gpt_live").voices}
    assert voices == {
        "arbor", "breeze", "cove", "ember", "juniper",
        "maple", "sol", "spruce", "vale",
    }


def test_resolve_voice_coerces_unknown_but_keeps_new_ones() -> None:
    assert resolve_voice("openai", "marin") == "marin"
    assert resolve_voice("openai", "MARIN") == "marin"
    assert resolve_voice("openai", "not-a-voice") == "alloy"
    assert resolve_voice("openai", "") == "alloy"


def test_resolve_voice_passes_through_for_clone_backends() -> None:
    # Fish/ElevenLabs ids are user-created handles — never coerce them.
    assert resolve_voice("fish", "my-clone-abc") == "my-clone-abc"
    assert resolve_voice("elevenlabs", "VOICEID9") == "VOICEID9"


def test_resolve_format_clamps_to_backend_support() -> None:
    assert resolve_format("fish", "aac").id == "mp3"  # fish has no aac
    assert resolve_format("openai", "flac").id == "flac"
    assert resolve_format("openai", "nonsense").id == "mp3"


# --------------------------------------------------------------------------
# Templating engine
# --------------------------------------------------------------------------


async def test_openai_shape(tmp_path) -> None:
    seen, transport = _capture()
    await synthesize(
        SynthesisRequest(
            text="你好", backend="openai", voice="marin", fmt="mp3",
            params={"api_key": "sk-x"}, out_dir=tmp_path, transport=transport,
        )
    )
    assert seen["url"] == "https://api.openai.com/v1/audio/speech"
    assert seen["headers"]["authorization"] == "Bearer sk-x"
    assert seen["body"] == {
        "model": "gpt-4o-mini-tts",
        "voice": "marin",
        "input": "你好",
        "response_format": "mp3",
    }


async def test_unset_optionals_are_dropped_not_blanked(tmp_path) -> None:
    seen, transport = _capture()
    await synthesize(
        SynthesisRequest(
            text="hi", backend="openai", params={"api_key": "k"},
            out_dir=tmp_path, transport=transport,
        )
    )
    # instructions/speed are in the template but unset for this call.
    assert "instructions" not in seen["body"]
    assert "speed" not in seen["body"]


async def test_speed_placeholder_stays_numeric(tmp_path) -> None:
    seen, transport = _capture()
    await synthesize(
        SynthesisRequest(
            text="hi", backend="openai", speed=1.25,
            params={"api_key": "k"}, out_dir=tmp_path, transport=transport,
        )
    )
    assert seen["body"]["speed"] == 1.25
    assert isinstance(seen["body"]["speed"], float)


async def test_instructions_dropped_for_backend_without_support(tmp_path) -> None:
    seen, transport = _capture()
    await synthesize(
        SynthesisRequest(
            text="hi", backend="fish", voice="ref-1", instructions="慢一点",
            params={"api_key": "fk"}, out_dir=tmp_path, transport=transport,
        )
    )
    assert "instructions" not in seen["body"]


async def test_header_carried_model_and_freeform_voice(tmp_path) -> None:
    seen, transport = _capture()
    await synthesize(
        SynthesisRequest(
            text="hi", backend="fish", voice="ref-1",
            params={"api_key": "fk"}, out_dir=tmp_path, transport=transport,
        )
    )
    assert seen["url"] == "https://api.fish.audio/v1/tts"
    assert seen["headers"]["model"] == "s2-pro"
    assert seen["body"]["reference_id"] == "ref-1"


async def test_placeholder_inside_path(tmp_path) -> None:
    seen, transport = _capture()
    await synthesize(
        SynthesisRequest(
            text="hi", backend="elevenlabs", voice="VOICE7",
            params={"api_key": "xk"}, out_dir=tmp_path, transport=transport,
        )
    )
    assert seen["url"].endswith("/text-to-speech/VOICE7")
    assert seen["headers"]["xi-api-key"] == "xk"
    assert "authorization" not in seen["headers"]


async def test_query_auth_and_nested_body_and_b64_response(tmp_path) -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"inlineData": {
                        "data": base64.b64encode(b"PCM!").decode()
                    }}]}}
                ]
            },
        )

    result = await synthesize(
        SynthesisRequest(
            text="hi", backend="gemini", voice="Kore",
            params={"api_key": "gk"}, out_dir=tmp_path,
            transport=httpx.MockTransport(handler),
        )
    )
    assert "key=gk" in seen["url"]
    voice_cfg = seen["body"]["generationConfig"]["speechConfig"]["voiceConfig"]
    assert voice_cfg["prebuiltVoiceConfig"]["voiceName"] == "Kore"
    assert result.path.read_bytes() == b"PCM!"


async def test_bad_b64_path_reports_precisely(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candidates": []})

    with pytest.raises(SynthesisError) as excinfo:
        await synthesize(
            SynthesisRequest(
                text="hi", backend="gemini", params={"api_key": "gk"},
                out_dir=tmp_path, transport=httpx.MockTransport(handler),
            )
        )
    assert excinfo.value.code == "tts_bad_response"


async def test_http_status_carries_upstream_code(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    with pytest.raises(SynthesisError) as excinfo:
        await synthesize(
            SynthesisRequest(
                text="hi", backend="openai", params={"api_key": "k"},
                out_dir=tmp_path, transport=httpx.MockTransport(handler),
            )
        )
    assert excinfo.value.code == "tts_http_status"
    assert excinfo.value.status_code == 429


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


def test_provider_key_is_borrowed_for_bound_voice_provider(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("FISH_AUDIO_API_KEY", raising=False)
    provider = SimpleNamespace(_api_key="fish-key", _base_url="https://fish.example")
    key, base = resolve_credentials(get_backend("fish"), provider, {})
    assert key == "fish-key"
    assert base == "https://fish.example"


def test_openai_key_is_not_leaked_to_third_party_backend(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    provider = SimpleNamespace(_api_key="sk-openai")
    key, _ = resolve_credentials(get_backend("elevenlabs"), provider, {})
    assert key is None


def test_openai_key_is_used_for_openai_backend(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    provider = SimpleNamespace(_api_key="sk-openai")
    key, _ = resolve_credentials(get_backend("openai"), provider, {})
    assert key == "sk-openai"


def test_params_pin_beats_env(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    key, base = resolve_credentials(
        get_backend("openai"), None, {"api_key": "sk-pinned", "base_url": "https://r"}
    )
    assert key == "sk-pinned"
    assert base == "https://r"


# --------------------------------------------------------------------------
# Custom (UI-defined) backends
# --------------------------------------------------------------------------


async def test_custom_backend_from_config_is_fully_usable(tmp_path) -> None:
    registered = register_backends_from_config(
        {
            "acme": {
                "label": "Acme 语音",
                "base_url": "https://acme.test",
                "models": ["v2"],
                "voices": [{"id": "lin", "label": "小林", "tone": "轻快"}],
                "formats": ["wav"],
                "default_voice": "lin",
                "http": {
                    "path": "/say",
                    "auth": "header",
                    "auth_header": "X-Key",
                    "body": {"q": "{text}", "spk": "{voice}", "fmt": "{format}"},
                },
            }
        }
    )
    assert registered == ("acme",)
    seen, transport = _capture()
    result = await synthesize(
        SynthesisRequest(
            text="试听", backend="acme", params={"api_key": "tok"},
            out_dir=tmp_path, transport=transport,
        )
    )
    assert seen["url"] == "https://acme.test/say"
    assert seen["headers"]["x-key"] == "tok"
    assert seen["body"] == {"q": "试听", "spk": "lin", "fmt": "wav"}
    assert result.path.suffix == ".wav"


def test_config_can_extend_a_builtin_without_resetting_it() -> None:
    register_backends_from_config(
        {"openai": {"base_url": "https://relay.test/v1", "models": ["my-tts"]}}
    )
    backend = get_backend("openai")
    assert backend.base_url == "https://relay.test/v1"
    assert backend.default_model == "my-tts"
    # Voices were not overridden, so the built-in catalog survives.
    assert {v.id for v in backend.voices} >= {"alloy", "marin"}
    assert backend.http is not None


def test_disabled_and_malformed_blocks_are_skipped() -> None:
    registered = register_backends_from_config(
        {
            "off": {"http": {"path": "/x"}, "enabled": False},
            "nowire": {"label": "no http spec"},
            "notatable": "just a string",
        }
    )
    assert registered == ()


def test_reset_drops_custom_but_keeps_builtins() -> None:
    register_backends_from_config({"acme": {"http": {"path": "/x"}}})
    assert get_backend("acme") is not None
    reset_custom_backends()
    assert get_backend("acme") is None
    assert get_backend("openai") is not None


# --------------------------------------------------------------------------
# GPT-Live
# --------------------------------------------------------------------------


def test_live_base_url_strips_v1_suffix() -> None:
    # Chat providers are configured with a /v1 base, but the Live
    # handshake lives at the host root.
    assert _live_base_url("https://api.example.com/v1") == "https://api.example.com"
    assert _live_base_url("https://api.example.com/") == "https://api.example.com"


def test_session_requests_audio_only_and_pins_voice() -> None:
    session = build_session(model="gpt-live-1", voice="cove", instructions="慢一点")
    assert session["type"] == "realtime"
    assert session["model"] == "gpt-live-1"
    assert session["output_modalities"] == ["audio"]
    assert session["audio"]["output"]["voice"] == "cove"
    # No mic is attached, so server VAD must be off or it waits forever.
    assert session["audio"]["input"]["turn_detection"] is None
    assert "verbatim" in session["instructions"]
    assert "慢一点" in session["instructions"]


@pytest.mark.parametrize(
    ("status", "body", "code"),
    [
        (
            503,
            {"error": {"message": "Live attestation is unavailable: ..."}},
            "live_attestation_unavailable",
        ),
        (404, {"error": {"message": "404 page not found"}}, "live_endpoint_missing"),
        (401, {"error": {"message": "bad key"}}, "live_http_status"),
    ],
)
def test_live_error_classification(status: int, body: dict, code: str) -> None:
    response = httpx.Response(status, json=body)
    assert _classify_live_error(response).code == code


def test_answer_sdp_accepts_raw_and_json() -> None:
    raw = httpx.Response(200, text="v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\n")
    assert _answer_sdp(raw).startswith("v=0")
    wrapped = httpx.Response(200, json={"sdp": "v=0\r\nJSON\r\n"})
    assert "JSON" in _answer_sdp(wrapped)


async def test_negotiate_falls_through_404_to_codex_alias() -> None:
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(request.url.path)
        if request.url.path == "/v1/live":
            return httpx.Response(404, text="404 page not found")
        return httpx.Response(200, text="v=0\r\nANSWER\r\n")

    answer = await _negotiate(
        base_url="https://gw.test",
        api_key="k",
        offer_sdp="v=0\r\n",
        session={"type": "realtime"},
        timeout=5,
        transport=httpx.MockTransport(handler),
    )
    assert tried == ["/v1/live", "/backend-api/codex/realtime/calls"]
    assert "ANSWER" in answer


async def test_negotiate_reports_attestation_without_trying_alias() -> None:
    tried: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tried.append(request.url.path)
        return httpx.Response(
            503, json={"error": {"message": "Live attestation is unavailable: macOS only"}}
        )

    with pytest.raises(SynthesisError) as excinfo:
        await _negotiate(
            base_url="https://gw.test", api_key="k", offer_sdp="v=0\r\n",
            session={}, timeout=5, transport=httpx.MockTransport(handler),
        )
    assert excinfo.value.code == "live_attestation_unavailable"
    assert excinfo.value.status_code == 503
    # A non-404 is authoritative — no point retrying the other spelling.
    assert tried == ["/v1/live"]


async def test_negotiate_sends_sdp_and_session_object() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, text="v=0\r\nOK\r\n")

    await _negotiate(
        base_url="https://gw.test", api_key="k",
        offer_sdp="v=0\r\nOFFER\r\n",
        session=build_session(model="gpt-live-1", voice="cove", instructions=None),
        timeout=5, transport=httpx.MockTransport(handler),
    )
    assert "OFFER" in seen["body"]["sdp"]
    assert isinstance(seen["body"]["session"], dict)
    assert seen["auth"] == "Bearer k"


async def test_gpt_live_without_aiortc_degrades_precisely(tmp_path, monkeypatch) -> None:
    import corlinman_agent.voice.gpt_live as live

    def missing() -> tuple[object, object, object]:
        raise SynthesisError("gpt_live_dependency_missing", "aiortc 未安装")

    monkeypatch.setattr(live, "_import_webrtc", missing)
    with pytest.raises(SynthesisError) as excinfo:
        await synthesize(
            SynthesisRequest(
                text="hi", backend="gpt_live", params={"api_key": "k"},
                out_dir=tmp_path,
            )
        )
    assert excinfo.value.code == "gpt_live_dependency_missing"
