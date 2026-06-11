"""Legacy ``MODEL_PREFIX_DEFAULTS`` resolutions (P5 — broader model cover).

A specs-less :class:`ProviderRegistry` resolves raw model ids purely via
the prefix table, so these tests pin the vendor each family lands on:
Mistral ids (``mistral-`` / ``codestral-`` / ``ministral-``) → Mistral,
Moonshot ids (``kimi-`` / ``moonshot-``) → Moonshot, bare ``llama-`` →
Groq, and the DeepSeek reasoning family (``deepseek-r1``) → DeepSeek.

Also pins the vendor env-key isolation contract for every no-config
vendor ctor: with the vendor env var unset and ``OPENAI_API_KEY`` set,
the adapter must hold NO key (never inherit the OpenAI bearer — sending
it to a third-party host would leak the credential) and must fail with
an :class:`AuthError` naming the missing vendor env var at first call.
"""

from __future__ import annotations

import pytest
from corlinman_providers import MoonshotProvider, OpenAIProvider, ProviderRegistry
from corlinman_providers.china import DeepSeekProvider, GLMProvider, QwenProvider
from corlinman_providers.failover import AuthError
from corlinman_providers.market_providers import GroqProvider, MistralProvider


@pytest.fixture()
def registry() -> ProviderRegistry:
    return ProviderRegistry([])


@pytest.mark.parametrize(
    ("model", "cls"),
    [
        ("mistral-large-latest", MistralProvider),
        ("codestral-2501", MistralProvider),
        ("ministral-8b-latest", MistralProvider),
        ("kimi-k2-0905-preview", MoonshotProvider),
        ("moonshot-v1-32k", MoonshotProvider),
        ("llama-3.3-70b-versatile", GroqProvider),
        ("deepseek-r1", DeepSeekProvider),
        ("deepseek-reasoner", DeepSeekProvider),
        ("o4-mini", OpenAIProvider),
    ],
)
def test_prefix_resolution(
    registry: ProviderRegistry, model: str, cls: type[object]
) -> None:
    provider, upstream_model, params = registry.resolve(model)
    assert type(provider) is cls
    assert upstream_model == model
    assert params == {}


def test_moonshot_supports_kimi_and_moonshot_families() -> None:
    assert MoonshotProvider.supports("kimi-latest")
    assert MoonshotProvider.supports("moonshot-v1-8k")
    assert not MoonshotProvider.supports("gpt-4o")


def test_mistral_supports_family_prefixes() -> None:
    assert MistralProvider.supports("mistral-small-latest")
    assert MistralProvider.supports("codestral-latest")
    assert MistralProvider.supports("ministral-3b-latest")
    assert not MistralProvider.supports("llama-3.1-8b")


def test_groq_supports_bare_llama_family() -> None:
    """A configured ``kind = "groq"`` spec must win the configured-provider
    scan for raw ``llama-*`` ids (mirrors the MODEL_PREFIX_DEFAULTS row)."""
    assert GroqProvider.supports("llama-3.3-70b-versatile")
    assert GroqProvider.supports("llama-3.1-8b-instant")
    # Conservative: vendor-scoped / other-family ids are not claimed.
    assert not GroqProvider.supports("meta-llama/Llama-3.3-70B")
    assert not GroqProvider.supports("mistral-large-latest")
    assert not GroqProvider.supports("gpt-4o")


# ---------------------------------------------------------------------------
# Vendor env-key isolation — no-config ctors must NEVER inherit
# OPENAI_API_KEY (P1 regression: a raw ``mistral-*`` id with no
# MISTRAL_API_KEY used to bearer the user's OpenAI key to api.mistral.ai).
# ---------------------------------------------------------------------------

# (adapter class, vendor env var, representative raw model id)
_NO_CONFIG_VENDORS: list[tuple[type, str, str]] = [
    (MistralProvider, "MISTRAL_API_KEY", "mistral-large-latest"),
    (GroqProvider, "GROQ_API_KEY", "llama-3.3-70b-versatile"),
    (MoonshotProvider, "MOONSHOT_API_KEY", "kimi-k2-0905-preview"),
    (DeepSeekProvider, "DEEPSEEK_API_KEY", "deepseek-chat"),
    (QwenProvider, "DASHSCOPE_API_KEY", "qwen-max"),
    (GLMProvider, "ZHIPU_API_KEY", "glm-4-plus"),
]


@pytest.mark.parametrize(("cls", "env_var", "model"), _NO_CONFIG_VENDORS)
def test_no_config_ctor_never_inherits_openai_key(
    monkeypatch: pytest.MonkeyPatch, cls: type, env_var: str, model: str
) -> None:
    """Vendor key unset + OPENAI_API_KEY set → the adapter holds NO key,
    so no request could ever carry the OpenAI bearer to the vendor host."""
    monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret")
    prov = cls()
    assert prov._api_key is None


@pytest.mark.parametrize(("cls", "env_var", "model"), _NO_CONFIG_VENDORS)
def test_no_config_ctor_reads_vendor_env_key(
    monkeypatch: pytest.MonkeyPatch, cls: type, env_var: str, model: str
) -> None:
    """The vendor's own env var IS honoured when present."""
    monkeypatch.setenv(env_var, "sk-vendor-key")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret")
    prov = cls()
    assert prov._api_key == "sk-vendor-key"


@pytest.mark.asyncio
@pytest.mark.parametrize(("cls", "env_var", "model"), _NO_CONFIG_VENDORS)
async def test_no_config_ctor_missing_vendor_key_raises_auth_error(
    monkeypatch: pytest.MonkeyPatch, cls: type, env_var: str, model: str
) -> None:
    """First call fails with an AuthError naming the missing vendor env var
    — it never falls through to a request bearing OPENAI_API_KEY."""
    monkeypatch.delenv(env_var, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret")
    prov = cls()
    with pytest.raises(AuthError, match=env_var):
        async for _ in prov.chat_stream(
            model=model, messages=[{"role": "user", "content": "hi"}]
        ):
            pass


def test_market_build_without_api_key_reads_vendor_env_not_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configured ``kind = "mistral"`` / ``kind = "groq"`` build path has
    the same isolation contract as the no-config ctor."""
    from corlinman_providers import ProviderKind, ProviderSpec

    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-secret")

    mistral = MistralProvider.build(ProviderSpec(name="m", kind=ProviderKind.MISTRAL))
    groq = GroqProvider.build(ProviderSpec(name="g", kind=ProviderKind.GROQ))
    assert mistral._api_key is None
    assert groq._api_key is None

    monkeypatch.setenv("MISTRAL_API_KEY", "sk-mistral")
    assert MistralProvider.build(
        ProviderSpec(name="m", kind=ProviderKind.MISTRAL)
    )._api_key == "sk-mistral"
