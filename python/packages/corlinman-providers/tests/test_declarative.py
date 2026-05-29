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
    DeclarativeProvider,
    DeclarativeProviderSpec,
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
