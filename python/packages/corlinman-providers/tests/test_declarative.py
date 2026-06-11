"""Tests for declarative (TOML-driven) providers.

Coverage matches the three acceptance criteria from the task:
  1. Load ``moonshot.toml`` → valid :class:`DeclarativeProviderSpec`.
  2. :class:`DeclarativeProvider` constructs and :meth:`list_models`
     surfaces every declared model.
  3. Conflict policy: class-based ``ProviderKind.OPENAI`` spec + TOML
     spec with ``id = "openai"`` → TOML dropped + WARNING logged, the
     class-based provider remains the one served by the registry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import structlog
from corlinman_providers import (
    AnthropicProvider,
    DeclarativeProvider,
    DeclarativeProviderSpec,
    GoogleProvider,
    OpenAIProvider,
    ProviderKind,
    ProviderRegistry,
    ProviderSpec,
    load_spec_from_toml,
)
from corlinman_providers.declarative import ModelSpec

SPEC_DIR = Path(__file__).resolve().parent.parent / "spec"


def test_load_moonshot_toml_yields_valid_spec() -> None:
    """moonshot.toml round-trips into a well-formed spec."""
    spec = load_spec_from_toml(SPEC_DIR / "moonshot.toml")

    assert isinstance(spec, DeclarativeProviderSpec)
    assert spec.id == "moonshot"
    assert spec.name == "Moonshot (月之暗面)"
    assert spec.base_url == "https://api.moonshot.cn/v1"
    assert spec.auth_kind == "bearer_api_key"
    assert spec.request_format == "openai_compatible"
    assert spec.auth_config["env_var"] == "MOONSHOT_API_KEY"
    # Three models declared in the TOML.
    assert set(spec.models.keys()) == {"default", "short", "long"}
    long_model = spec.models["long"]
    assert isinstance(long_model, ModelSpec)
    assert long_model.id == "moonshot-v1-128k"
    assert long_model.context_length == 131072
    assert long_model.supports_tools is True


def test_load_toml_supports_tools_is_tristate(tmp_path: Path) -> None:
    """The loader keeps the explicit-vs-unset distinction: a row that never
    declared ``supports_tools`` parses to ``None`` (inherits provider level),
    while explicit ``false`` / ``true`` parse to real bools."""
    toml_body = """
id = "tri"
name = "Tri-state Gateway"
base_url = "https://gateway.invalid/v1"
auth_kind = "bearer_api_key"
request_format = "openai_compatible"

[auth_config]
env_var = "TRI_API_KEY"

[models.unset]
id = "tri-unset"
context_length = 8192

[models.explicit_false]
id = "tri-false"
context_length = 8192
supports_tools = false

[models.explicit_true]
id = "tri-true"
context_length = 8192
supports_tools = true
"""
    path = tmp_path / "tri.toml"
    path.write_text(toml_body, encoding="utf-8")
    spec = load_spec_from_toml(path)

    assert spec.models["unset"].supports_tools is None
    assert spec.models["explicit_false"].supports_tools is False
    assert spec.models["explicit_true"].supports_tools is True

    # And the runtime adapter honours exactly the explicit false.
    provider = DeclarativeProvider(spec, api_key="sk-test")
    assert provider.supports_tools("tri-false") is False
    assert provider.supports_tools("tri-unset") is True
    assert provider.supports_tools("tri-true") is True


def test_declarative_provider_constructs_and_lists_models() -> None:
    """Given a mock api_key, DeclarativeProvider builds and lists all models."""
    spec = load_spec_from_toml(SPEC_DIR / "moonshot.toml")
    provider = DeclarativeProvider(spec, api_key="sk-test-mock")

    assert provider.name == "moonshot"
    # list_models returns every ModelSpec — order-insensitive.
    ids = {m.id for m in provider.list_models()}
    assert ids == {"moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"}
    # Inner adapter is an OpenAIProvider for an openai_compatible spec.
    assert isinstance(provider._inner, OpenAIProvider)


def _built_request_headers(client: Any) -> Any:
    """Build a dummy chat request through the real openai SDK client and
    return its final outgoing (case-insensitive ``httpx.Headers``) — the
    faithful view of what auth would actually hit the wire."""
    from openai._models import FinalRequestOptions

    opts = FinalRequestOptions.construct(
        method="post", url="/chat/completions", json_data={"model": "m"}
    )
    return client._build_request(opts).headers


def test_header_auth_sends_custom_header_not_bearer() -> None:
    """auth_kind="header" must put the api_key in the declared custom header
    (e.g. X-API-Key) and NOT leak the real secret as Authorization: Bearer.

    We build a real request through the openai SDK and inspect the final
    outgoing headers — the credential must appear under X-API-Key, and any
    Authorization header must not carry the real secret.
    """
    secret = "sk-secret-123"
    spec = DeclarativeProviderSpec(
        id="customhdr",
        name="Custom Header Gateway",
        base_url="https://gateway.invalid/v1",
        auth_kind="header",
        auth_config={"env_var": "CUSTOMHDR_API_KEY", "header_name": "X-API-Key"},
        request_format="openai_compatible",
        models={"default": ModelSpec(id="m", context_length=8192)},
    )
    provider = DeclarativeProvider(spec, api_key=secret)
    assert isinstance(provider._inner, OpenAIProvider)

    client = provider._inner._make_client()
    headers = _built_request_headers(client)

    assert headers.get("X-API-Key") == secret
    # The real secret must NOT leak as a bearer credential.
    assert secret not in (headers.get("Authorization") or "")


def test_header_auth_honours_value_prefix() -> None:
    """A declared ``value_prefix`` (e.g. "Token ") is prepended to the key."""
    spec = DeclarativeProviderSpec(
        id="prefixhdr",
        name="Prefixed Header Gateway",
        base_url="https://gateway.invalid/v1",
        auth_kind="header",
        auth_config={
            "env_var": "PREFIXHDR_API_KEY",
            "header_name": "X-Auth",
            "value_prefix": "Token ",
        },
        request_format="openai_compatible",
        models={"default": ModelSpec(id="m", context_length=8192)},
    )
    provider = DeclarativeProvider(spec, api_key="abc")
    client = provider._inner._make_client()
    headers = _built_request_headers(client)

    assert headers.get("X-Auth") == "Token abc"


def test_header_auth_missing_header_name_raises() -> None:
    """auth_kind='header' without a header_name is a misconfig → loud failure,
    not a silent unauthenticated request."""
    spec = DeclarativeProviderSpec(
        id="nohdr",
        name="No Header Name",
        base_url="https://gateway.invalid/v1",
        auth_kind="header",
        auth_config={"env_var": "NOHDR_API_KEY"},
        request_format="openai_compatible",
        models={"default": ModelSpec(id="m", context_length=8192)},
    )
    with pytest.raises(ValueError, match="header_name"):
        DeclarativeProvider(spec, api_key="abc")


def test_query_param_auth_raises_clear_error() -> None:
    """auth_kind="query_param" is not yet supported by the inner client — it
    must fail loudly at build time rather than silently bearer-authing."""
    spec = DeclarativeProviderSpec(
        id="qp",
        name="Query Param Gateway",
        base_url="https://gateway.invalid/v1",
        auth_kind="query_param",
        auth_config={"env_var": "QP_API_KEY", "param_name": "api_key"},
        request_format="openai_compatible",
        models={"default": ModelSpec(id="m", context_length=8192)},
    )
    with pytest.raises(ValueError, match="query_param auth not yet supported"):
        DeclarativeProvider(spec, api_key="sk-secret")


# --- anthropic_compatible / gemini_compatible custom-header auth (R7-B2) ----
#
# These mirror the openai_compatible header-auth tests above: a declarative
# spec with ``auth_kind="header"`` against the anthropic/gemini wire formats
# must send the credential in the DECLARED custom header (captured off the
# fake vendor client) instead of silently falling back to the default vendor
# auth (Anthropic ``x-api-key`` / Gemini ``x-goog-api-key``). ``query_param``
# must raise the same clear "not yet supported" error.


@pytest.fixture
def _capture_anthropic_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch ``anthropic.AsyncAnthropic`` to record its constructor kwargs.

    Returns a dict that ``chat_stream`` fills with the kwargs the inner
    :class:`AnthropicProvider` passed to the vendor SDK, so we can assert
    the credential rode the custom header (not the default ``x-api-key``).
    """
    import anthropic  # type: ignore[import-not-found]

    captured: dict[str, Any] = {}

    class _FakeStream:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        def __aiter__(self) -> Any:
            async def _gen() -> Any:
                if False:  # pragma: no cover — empty event stream
                    yield None

            return _gen()

        async def get_final_message(self) -> Any:
            from types import SimpleNamespace

            return SimpleNamespace(stop_reason="end_turn")

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            from types import SimpleNamespace

            self.messages = SimpleNamespace(stream=lambda **_: _FakeStream())

        async def close(self) -> None:
            return None

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _FakeClient)
    return captured


@pytest.mark.asyncio
async def test_declarative_anthropic_header_auth_uses_custom_header(
    _capture_anthropic_kwargs: dict[str, Any],
) -> None:
    """anthropic_compatible + auth_kind="header" sends the key in the declared
    header, NOT as the default Anthropic ``x-api-key`` credential."""
    secret = "sk-anthropic-secret-123"
    spec = DeclarativeProviderSpec(
        id="anthropic-gw",
        name="Anthropic-wire Gateway",
        base_url="",
        auth_kind="header",
        auth_config={"env_var": "ANTHROPIC_GW_KEY", "header_name": "X-Custom-Auth"},
        request_format="anthropic_compatible",
        models={"default": ModelSpec(id="claude-x", context_length=8192)},
    )
    provider = DeclarativeProvider(spec, api_key=secret)

    async for _ in provider.chat_stream(model="claude-x", messages=[]):
        pass

    # The vendor SDK must NOT receive the real secret as its native api_key —
    # that would auth via the default ``x-api-key`` header (wrong gateway key).
    assert _capture_anthropic_kwargs.get("api_key") != secret
    # The credential must ride the declared custom header.
    headers = _capture_anthropic_kwargs.get("default_headers") or {}
    assert headers.get("X-Custom-Auth") == secret


@pytest.mark.asyncio
async def test_declarative_anthropic_header_auth_value_prefix(
    _capture_anthropic_kwargs: dict[str, Any],
) -> None:
    """A declared ``value_prefix`` is prepended to the key in the header."""
    spec = DeclarativeProviderSpec(
        id="anthropic-prefix",
        name="Anthropic Prefixed Gateway",
        base_url="",
        auth_kind="header",
        auth_config={
            "env_var": "ANTHROPIC_PREFIX_KEY",
            "header_name": "X-Auth",
            "value_prefix": "Token ",
        },
        request_format="anthropic_compatible",
        models={"default": ModelSpec(id="claude-x", context_length=8192)},
    )
    provider = DeclarativeProvider(spec, api_key="abc")

    async for _ in provider.chat_stream(model="claude-x", messages=[]):
        pass

    headers = _capture_anthropic_kwargs.get("default_headers") or {}
    assert headers.get("X-Auth") == "Token abc"


def test_declarative_anthropic_header_auth_no_secret_on_default_xapikey() -> None:
    """End-to-end wire check (real anthropic SDK): with a custom header that
    is NOT ``x-api-key``, the secret rides the custom header and the default
    ``x-api-key`` carries only the harmless sentinel — never the secret."""
    from anthropic import AsyncAnthropic
    from anthropic._models import FinalRequestOptions
    from corlinman_providers.anthropic_provider import _HEADER_AUTH_SENTINEL

    secret = "sk-anthropic-secret-xyz"
    spec = DeclarativeProviderSpec(
        id="anthropic-wire",
        name="Anthropic Wire",
        base_url="",
        auth_kind="header",
        auth_config={"env_var": "ANTHROPIC_WIRE_KEY", "header_name": "X-Custom-Auth"},
        request_format="anthropic_compatible",
        models={"default": ModelSpec(id="claude-x", context_length=8192)},
    )
    provider = DeclarativeProvider(spec, api_key=secret)
    inner = provider._inner
    assert isinstance(inner, AnthropicProvider)

    # Build a real Anthropic SDK client the way chat_stream would.
    client = AsyncAnthropic(
        api_key=_HEADER_AUTH_SENTINEL,
        default_headers=dict(inner._default_headers or {}),
    )
    opts = FinalRequestOptions.construct(
        method="post", url="/v1/messages", json_data={"model": "claude-x"}
    )
    headers = client._build_request(opts).headers

    assert headers.get("x-custom-auth") == secret
    # The secret must NOT leak via the default vendor auth header.
    assert headers.get("x-api-key") != secret
    assert secret not in (headers.get("authorization") or "")


def test_declarative_anthropic_query_param_raises() -> None:
    """anthropic_compatible + query_param is not yet supported → loud raise."""
    spec = DeclarativeProviderSpec(
        id="anthropic-qp",
        name="Anthropic Query Param",
        base_url="",
        auth_kind="query_param",
        auth_config={"env_var": "ANTHROPIC_QP_KEY", "param_name": "api_key"},
        request_format="anthropic_compatible",
        models={"default": ModelSpec(id="claude-x", context_length=8192)},
    )
    with pytest.raises(ValueError, match="query_param auth not yet supported"):
        DeclarativeProvider(spec, api_key="sk-secret")


@pytest.fixture
def _capture_google_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch ``google.genai.Client`` to record its constructor kwargs."""
    import sys
    from types import ModuleType, SimpleNamespace

    from google.genai import types as real_types

    captured: dict[str, Any] = {}

    class _FakeAsyncIter:
        def __aiter__(self) -> Any:
            async def _gen() -> Any:
                if False:  # pragma: no cover — empty chunk stream
                    yield None

            return _gen()

    class _FakeModels:
        async def generate_content_stream(self, **_: Any) -> _FakeAsyncIter:
            return _FakeAsyncIter()

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            self.aio = SimpleNamespace(models=_FakeModels())

    google_mod = ModuleType("google")
    genai_mod = ModuleType("google.genai")
    genai_mod.Client = _FakeClient  # type: ignore[attr-defined]
    genai_mod.types = real_types  # type: ignore[attr-defined]
    google_mod.genai = genai_mod  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.genai", genai_mod)
    monkeypatch.setitem(sys.modules, "google.genai.types", real_types)
    return captured


@pytest.mark.asyncio
async def test_declarative_google_header_auth_uses_custom_header(
    _capture_google_kwargs: dict[str, Any],
) -> None:
    """gemini_compatible + auth_kind="header" sends the key in the declared
    header, NOT as the default Gemini ``x-goog-api-key`` credential."""
    secret = "sk-google-secret-123"
    spec = DeclarativeProviderSpec(
        id="gemini-gw",
        name="Gemini-wire Gateway",
        base_url="",
        auth_kind="header",
        auth_config={"env_var": "GEMINI_GW_KEY", "header_name": "X-Custom-Auth"},
        request_format="gemini_compatible",
        models={"default": ModelSpec(id="gemini-x", context_length=8192)},
    )
    provider = DeclarativeProvider(spec, api_key=secret)
    assert isinstance(provider._inner, GoogleProvider)

    async for _ in provider.chat_stream(model="gemini-x", messages=[]):
        pass

    # The vendor SDK must NOT receive the real secret as its native api_key.
    assert _capture_google_kwargs.get("api_key") != secret
    http_options = _capture_google_kwargs.get("http_options")
    headers = getattr(http_options, "headers", None) or {}
    assert headers.get("X-Custom-Auth") == secret


def test_declarative_google_query_param_raises() -> None:
    """gemini_compatible + query_param is not yet supported → loud raise."""
    spec = DeclarativeProviderSpec(
        id="gemini-qp",
        name="Gemini Query Param",
        base_url="",
        auth_kind="query_param",
        auth_config={"env_var": "GEMINI_QP_KEY", "param_name": "key"},
        request_format="gemini_compatible",
        models={"default": ModelSpec(id="gemini-x", context_length=8192)},
    )
    with pytest.raises(ValueError, match="query_param auth not yet supported"):
        DeclarativeProvider(spec, api_key="sk-secret")


def test_registry_conflict_prefers_classbased_and_warns() -> None:
    """class-based ``ProviderKind.OPENAI`` + TOML ``id="openai"`` → TOML loses.

    We feed the registry a declarative spec *by hand* (bypassing the
    directory scan) so the test doesn't depend on any file on disk and
    stays hermetic. ``structlog.testing.capture_logs`` collects structlog
    events without perturbing the global logging config.
    """
    class_spec = ProviderSpec(
        name="openai",
        kind=ProviderKind.OPENAI,
        api_key="sk-test-class",
    )
    declarative_spec = DeclarativeProviderSpec(
        id="openai",  # intentional collision
        name="Openai via TOML",
        base_url="https://example.invalid/v1",
        auth_kind="bearer_api_key",
        auth_config={"env_var": "OPENAI_API_KEY"},
        request_format="openai_compatible",
        models={
            "default": ModelSpec(id="example-model", context_length=8192),
        },
    )

    with structlog.testing.capture_logs() as captured:
        reg = ProviderRegistry(
            [class_spec],
            declarative_specs=[declarative_spec],
        )

    # class-based wins — the provider served under "openai" is the
    # class-based OpenAIProvider, NOT the DeclarativeProvider composite.
    provider = reg.get("openai")
    assert isinstance(provider, OpenAIProvider)
    assert not isinstance(provider, DeclarativeProvider)
    # The TOML spec did not make it into the declarative-specs listing.
    assert reg.list_declarative_specs() == []
    # A WARNING naming the conflict was emitted.
    conflicts = [
        ev
        for ev in captured
        if ev.get("event") == "provider.declarative_conflict"
        and ev.get("log_level") == "warning"
    ]
    assert conflicts, f"expected a provider.declarative_conflict WARNING; got {captured}"
    assert conflicts[0]["id"] == "openai"


def test_context_window_returns_declared_length_by_wire_id() -> None:
    """context_window(model) resolves the declared context_length by wire id,
    and returns None for an unknown model (→ loop falls back to its default)."""
    spec = DeclarativeProviderSpec(
        id="ctxprov",
        name="Ctx Gateway",
        base_url="https://gateway.invalid/v1",
        auth_kind="bearer_api_key",
        auth_config={"env_var": "CTXPROV_API_KEY"},
        request_format="openai_compatible",
        models={
            "default": ModelSpec(id="ctx-small", context_length=32_768),
            "long": ModelSpec(id="ctx-long", context_length=1_000_000),
        },
    )
    provider = DeclarativeProvider(spec, api_key="sk-test")
    assert provider.context_window("ctx-small") == 32_768
    assert provider.context_window("ctx-long") == 1_000_000
    assert provider.context_window("not-declared") is None
