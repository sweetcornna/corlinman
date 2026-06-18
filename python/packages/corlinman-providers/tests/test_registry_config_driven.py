"""Tests for Feature C's config-driven ``ProviderRegistry``.

Coverage:
  (a) every :class:`ProviderKind` resolves to the right adapter when
      declared as a spec;
  (b) params merge order is ``provider.params`` < ``alias.params``
      (request-level overrides happen in the reasoning loop, not here);
  (c) raw model ids that don't appear in ``aliases`` still resolve via the
      legacy prefix fallback;
  (d) ``openai_compatible`` requires ``base_url``.
"""

from __future__ import annotations

import pytest
from corlinman_providers import (
    AliasEntry,
    AnthropicProvider,
    DeepSeekProvider,
    GLMProvider,
    GoogleProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
    ProviderKind,
    ProviderRegistry,
    ProviderSpec,
    QwenProvider,
)


def _spec(
    name: str,
    kind: ProviderKind,
    *,
    api_key: str | None = "sk-test",
    base_url: str | None = None,
    params: dict | None = None,
) -> ProviderSpec:
    return ProviderSpec(
        name=name,
        kind=kind,
        api_key=api_key,
        base_url=base_url,
        enabled=True,
        params=params or {},
    )


@pytest.mark.parametrize(
    "kind, expected_cls, base_url",
    [
        (ProviderKind.ANTHROPIC, AnthropicProvider, None),
        (ProviderKind.OPENAI, OpenAIProvider, None),
        (ProviderKind.GOOGLE, GoogleProvider, None),
        (ProviderKind.DEEPSEEK, DeepSeekProvider, None),
        (ProviderKind.QWEN, QwenProvider, None),
        (ProviderKind.GLM, GLMProvider, None),
        (ProviderKind.OPENAI_COMPATIBLE, OpenAICompatibleProvider, "http://localhost:8000/v1"),
    ],
)
def test_registry_builds_each_kind(
    kind: ProviderKind, expected_cls: type, base_url: str | None
) -> None:
    """Every enum variant must yield a built provider of the right class."""
    name = f"test-{kind.value}"
    spec = _spec(name, kind, base_url=base_url)
    reg = ProviderRegistry([spec])

    provider = reg.get(name)
    assert provider is not None
    assert isinstance(provider, expected_cls)


def test_registry_skips_disabled_specs() -> None:
    """Disabled specs are retained for listing but no provider is built."""
    spec = _spec("disabled", ProviderKind.OPENAI)
    spec.enabled = False
    reg = ProviderRegistry([spec])

    assert reg.get("disabled") is None
    assert [s.name for s in reg.list_specs()] == ["disabled"]


def test_resolve_via_alias_returns_merged_params() -> None:
    """Alias params override provider params (alias wins)."""
    spec = _spec(
        "openai-main",
        ProviderKind.OPENAI,
        params={"temperature": 0.2, "timeout_ms": 30_000},
    )
    reg = ProviderRegistry([spec])
    aliases = {
        "fast": AliasEntry(
            provider="openai-main",
            model="gpt-4o-mini",
            params={"temperature": 0.9, "top_p": 0.95},
        )
    }

    provider, model, merged = reg.resolve(alias_or_model="fast", aliases=aliases)

    assert isinstance(provider, OpenAIProvider)
    assert model == "gpt-4o-mini"
    # alias.temperature (0.9) wins over provider.temperature (0.2)
    assert merged["temperature"] == pytest.approx(0.9)
    # provider-level key flows through when alias doesn't override it
    assert merged["timeout_ms"] == 30_000
    # alias-only keys are present
    assert merged["top_p"] == pytest.approx(0.95)


def test_provider_spec_accepts_api_key_value_shape() -> None:
    """Admin-authored ``api_key = { value = ... }`` builds at runtime."""
    spec = ProviderSpec.model_validate(
        {
            "name": "custom",
            "kind": "openai_compatible",
            "api_key": {"value": "sk-test"},
            "base_url": "https://relay.example/v1",
        }
    )

    assert spec.api_key == "sk-test"


def test_provider_spec_accepts_api_key_env_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env-shaped API keys are resolved before adapter construction."""
    monkeypatch.setenv("CORLINMAN_TEST_PROVIDER_KEY", "sk-from-env")

    spec = ProviderSpec.model_validate(
        {
            "name": "custom",
            "kind": "openai_compatible",
            "api_key": {"env": "CORLINMAN_TEST_PROVIDER_KEY"},
            "base_url": "https://relay.example/v1",
        }
    )

    assert spec.api_key == "sk-from-env"


def test_resolve_filters_custom_metadata_from_runtime_params() -> None:
    """``params.custom`` is an admin marker, not an upstream request arg."""
    spec = _spec(
        "relay",
        ProviderKind.OPENAI_COMPATIBLE,
        base_url="https://relay.example/v1",
        params={"custom": True, "temperature": 0.2},
    )
    reg = ProviderRegistry([spec])
    aliases = {
        "chat-default": AliasEntry(
            provider="relay",
            model="gpt-5.2-chat-latest",
            params={"custom": True, "top_p": 0.95},
        )
    }

    _, _, merged = reg.resolve(alias_or_model="chat-default", aliases=aliases)

    assert merged == {"temperature": 0.2, "top_p": 0.95}


def test_resolve_legacy_prefix_fallback() -> None:
    """Raw model id not in aliases matches the legacy prefix table."""
    reg = ProviderRegistry([])  # no specs!
    provider, model, merged = reg.resolve(
        alias_or_model="claude-sonnet-4-5", aliases={}
    )
    assert isinstance(provider, AnthropicProvider)
    assert model == "claude-sonnet-4-5"
    assert merged == {}


def test_resolve_raw_model_prefers_configured_provider() -> None:
    """Configured providers must handle matching raw model ids before legacy fallback."""
    spec = _spec(
        "openai-main",
        ProviderKind.OPENAI,
        base_url="https://gateway.example/v1",
        params={"timeout_ms": 60_000},
    )
    reg = ProviderRegistry([spec])

    provider, model, merged = reg.resolve(alias_or_model="gpt-5.5", aliases={})

    assert provider is reg.get("openai-main")
    assert model == "gpt-5.5"
    assert merged["timeout_ms"] == 60_000


def test_resolve_raw_openai_id_prefers_configured_compat_relay() -> None:
    """A configured generic ``openai_compatible`` relay must capture a raw
    OpenAI-family id (gpt-*/o*-*) instead of falling through to the public
    OpenAI default.

    Regression for the "custom provider configured, but the chat hits
    OpenAI" bug: ``OpenAICompatibleProvider.supports()`` returns ``False``
    so the configured-provider scan skips the relay; without the legacy
    fallback preferring it, ``gpt-*`` would resolve to a fresh
    ``OpenAIProvider()`` pointed at ``api.openai.com`` (the relay's
    ``base_url`` silently lost).
    """
    relay = _spec(
        "byo-relay",
        ProviderKind.OPENAI_COMPATIBLE,
        base_url="https://relay.example/v1",
        params={"timeout_ms": 45_000},
    )
    reg = ProviderRegistry([relay])

    provider, model, merged = reg.resolve(alias_or_model="gpt-5.5", aliases={})

    assert provider is reg.get("byo-relay")
    assert provider._base_url == "https://relay.example/v1"
    assert model == "gpt-5.5"
    assert merged["timeout_ms"] == 45_000


def test_resolve_compat_relay_does_not_capture_non_openai_family_id() -> None:
    """The relay rescue is scoped to OpenAI-family ids. A ``claude-*`` id
    with only an ``openai_compatible`` relay configured must still fall to
    the Anthropic legacy default, not the relay — the relay only speaks the
    OpenAI wire and never declared it serves Anthropic ids."""
    relay = _spec(
        "byo-relay",
        ProviderKind.OPENAI_COMPATIBLE,
        base_url="https://relay.example/v1",
    )
    reg = ProviderRegistry([relay])

    provider, model, _ = reg.resolve(
        alias_or_model="claude-sonnet-4-5", aliases={}
    )

    assert isinstance(provider, AnthropicProvider)
    assert provider is not reg.get("byo-relay")
    assert model == "claude-sonnet-4-5"


def test_resolve_raises_on_unknown_raw_id() -> None:
    # NOTE: ``llama-*`` moved out of "unknown" when MODEL_PREFIX_DEFAULTS
    # grew the Groq mapping (P5 broader-model-cover), so this uses a
    # prefix no adapter claims.
    reg = ProviderRegistry([])
    with pytest.raises(KeyError):
        reg.resolve(alias_or_model="frontier-never-registered", aliases={})


def test_resolve_alias_pointing_to_disabled_provider_raises() -> None:
    spec = _spec("ghost", ProviderKind.OPENAI)
    spec.enabled = False
    reg = ProviderRegistry([spec])

    aliases = {"broken": AliasEntry(provider="ghost", model="gpt-4o")}
    with pytest.raises(KeyError, match="disabled provider"):
        reg.resolve(alias_or_model="broken", aliases=aliases)


def test_openai_compatible_requires_base_url() -> None:
    """``openai_compatible`` specs without a base_url must fail to build."""
    spec = _spec("local-vllm", ProviderKind.OPENAI_COMPATIBLE, base_url=None)
    # Build runs inside __init__; the failure is caught + logged; provider
    # is absent from the registry.
    reg = ProviderRegistry([spec])
    assert reg.get("local-vllm") is None


def test_openai_compatible_honours_user_chosen_name() -> None:
    """The ``name`` instance attribute reflects the user-given spec name."""
    spec = _spec(
        "my-local-gateway",
        ProviderKind.OPENAI_COMPATIBLE,
        base_url="http://localhost:8000/v1",
    )
    reg = ProviderRegistry([spec])
    provider = reg.get("my-local-gateway")
    assert provider is not None
    assert provider.name == "my-local-gateway"


def test_params_schema_per_provider_has_required_common_keys() -> None:
    """Every provider declares the ``temperature`` / ``max_tokens`` keys."""
    for cls in (
        AnthropicProvider,
        OpenAIProvider,
        GoogleProvider,
        DeepSeekProvider,
        QwenProvider,
        GLMProvider,
        OpenAICompatibleProvider,
    ):
        schema = cls.params_schema()
        assert schema["type"] == "object"
        props = schema["properties"]
        assert "temperature" in props
        assert "max_tokens" in props
        assert "timeout_ms" in props


def test_legacy_module_level_resolve_still_works() -> None:
    """Back-compat: ``corlinman_providers.resolve(model)`` returns a provider."""
    from corlinman_providers import resolve

    assert isinstance(resolve("claude-sonnet-4-5"), AnthropicProvider)
    assert isinstance(resolve("gpt-4o-mini"), OpenAIProvider)
    assert isinstance(resolve("gemini-2.0-flash"), GoogleProvider)


# --------------------------------------------------------------------- #
# W-D1: ``provider_hint`` kwarg                                          #
# --------------------------------------------------------------------- #


def test_resolve_provider_hint_prefers_named_provider() -> None:
    """When an agent card pins ``provider:``, that provider should win
    over the generic configured-provider scan order."""
    # Two configured providers that could both plausibly claim a raw
    # model id; the hint picks the explicit one.
    openai_spec = _spec(
        "openai-main",
        ProviderKind.OPENAI,
        params={"timeout_ms": 30_000},
    )
    compat_spec = _spec(
        "extra-compat",
        ProviderKind.OPENAI_COMPATIBLE,
        base_url="https://compat.example/v1",
        params={"timeout_ms": 60_000},
    )
    reg = ProviderRegistry([openai_spec, compat_spec])

    # Without the hint, the scan-order winner is returned.
    no_hint_provider, _, _ = reg.resolve(alias_or_model="gpt-4o", aliases={})

    # With the hint, the hinted provider wins regardless of scan order
    # and its own params are surfaced.
    hinted_provider, model, merged = reg.resolve(
        alias_or_model="gpt-4o",
        aliases={},
        provider_hint="extra-compat",
    )
    assert hinted_provider is reg.get("extra-compat")
    assert hinted_provider is not no_hint_provider
    assert model == "gpt-4o"
    assert merged["timeout_ms"] == 60_000


def test_resolve_provider_hint_numeric_provider_id_routes_raw_gpt_model() -> None:
    """Persona bindings may store custom provider ids such as ``"2"``.

    A raw ``gpt-*`` model plus an explicit provider must use that
    provider's base URL/key, not the legacy OpenAI prefix fallback.
    """
    relay_spec = _spec(
        "2",
        ProviderKind.OPENAI_COMPATIBLE,
        base_url="https://relay.example/v1",
        params={"timeout_ms": 45_000},
    )
    reg = ProviderRegistry([relay_spec])

    provider, model, merged = reg.resolve(
        alias_or_model="gpt-5.5",
        aliases={},
        provider_hint="2",
    )

    assert provider is reg.get("2")
    assert model == "gpt-5.5"
    assert merged["timeout_ms"] == 45_000


def test_resolve_provider_hint_unknown_fails_instead_of_openai_fallback() -> None:
    """An explicit provider hint is a routing contract, not a preference.

    Persona Studio stores the user's selected provider separately from
    the model id. If that provider is missing or disabled at runtime we
    must fail clearly instead of falling through to the legacy OpenAI
    prefix resolver and surfacing "API key missing for provider openai".
    """
    reg = ProviderRegistry([])  # no specs at all
    with pytest.raises(KeyError, match="provider-that-does-not-exist"):
        reg.resolve(
            alias_or_model="gpt-5.5",
            aliases={},
            provider_hint="provider-that-does-not-exist",
        )


def test_resolve_provider_hint_default_is_back_compat() -> None:
    """Callers that don't pass ``provider_hint`` keep working — it must
    default to ``None`` so existing call sites are unchanged."""
    spec = _spec(
        "openai-main",
        ProviderKind.OPENAI,
        params={"timeout_ms": 30_000},
    )
    reg = ProviderRegistry([spec])
    # Same call as ``test_resolve_raw_model_prefers_configured_provider``,
    # asserting back-compat after the new kwarg landed.
    provider, model, merged = reg.resolve(alias_or_model="gpt-5.5", aliases={})
    assert provider is reg.get("openai-main")
    assert model == "gpt-5.5"
    assert merged["timeout_ms"] == 30_000


# --------------------------------------------------------------------- #
# Legacy ``kind = "newapi"`` silent migration                            #
# --------------------------------------------------------------------- #


def test_legacy_newapi_kind_migrates_to_openai_compatible(capsys) -> None:
    """A deployed VPS that still carries ``[providers.<x>] kind = "newapi"``
    must keep booting after the newapi adapter is removed. The model
    validator rewrites the kind to ``openai_compatible`` BEFORE pydantic
    parses the enum, and a structlog WARNING fires per migrated slot.

    The named kind no longer exists on :class:`ProviderKind` — this is
    the contract that lets the deprecation be silent at the wire level
    while still loud in the logs.
    """
    # Clear the per-process dedupe so this test is order-independent.
    from corlinman_providers import specs as _specs_mod

    _specs_mod._NEWAPI_WARNED.clear()

    raw_entry = {
        "name": "legacy-pool",
        "kind": "newapi",
        "api_key": "sk-legacy",
        "base_url": "http://localhost:3000/v1",
        "enabled": True,
        "params": {},
    }

    spec = ProviderSpec.model_validate(raw_entry)

    # The kind was rewritten on the way in.
    assert spec.kind is ProviderKind.OPENAI_COMPATIBLE
    assert spec.name == "legacy-pool"
    assert spec.base_url == "http://localhost:3000/v1"

    # And the deprecation event was logged at least once for this slot.
    # structlog ships configured with the dev ConsoleRenderer in this
    # codebase, which writes through ``sys.stdout`` — capsys is the
    # surface that surfaces it deterministically.
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "provider.newapi.deprecated" in combined, combined
    assert "legacy-pool" in combined

    # Registry must build the migrated spec via OpenAICompatibleProvider.
    reg = ProviderRegistry([spec])
    provider = reg.get("legacy-pool")
    assert provider is not None
    assert isinstance(provider, OpenAICompatibleProvider)


def test_legacy_newapi_kind_dedupes_warning_per_slot() -> None:
    """The deprecation warning fires once per unique slot name, not per
    spec reload — config snapshots rebuild the registry on every change
    and we don't want log spam during normal operation."""
    from corlinman_providers import specs as _specs_mod

    _specs_mod._NEWAPI_WARNED.clear()

    entry = {
        "name": "pool-a",
        "kind": "newapi",
        "base_url": "http://x/v1",
    }
    ProviderSpec.model_validate(entry)
    ProviderSpec.model_validate(entry)
    # Distinct slot — should warn separately.
    ProviderSpec.model_validate({**entry, "name": "pool-b"})

    assert _specs_mod._NEWAPI_WARNED == {"pool-a", "pool-b"}
