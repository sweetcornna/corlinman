"""Azure OpenAI provider adapter tests — offline.

Two concerns:

1. **URL / auth shape** — a real :class:`openai.AsyncAzureOpenAI` client is
   constructed and we assert it routes to
   ``/openai/deployments/<deployment>/chat/completions`` with an
   ``api-version`` query param and an ``api-key`` header (not
   ``Authorization: Bearer``). This is the load-bearing Azure difference.

2. **Stream reuse** — the adapter inherits :meth:`OpenAIProvider.chat_stream`
   verbatim, so we patch ``AsyncAzureOpenAI`` with a fake that yields the
   OpenAI-shaped chunk objects and confirm tokens + tool calls + the
   terminal ``done`` chunk flow through unchanged.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_providers import AzureProvider, ProviderChunk, ProviderKind, ProviderSpec
from corlinman_providers.azure_provider import DEFAULT_API_VERSION
from corlinman_providers.failover import AuthError

# --------------------------------------------------------------------------
# OpenAI-shaped fake stream (mirrors test_openai_provider_tool_stream.py)
# --------------------------------------------------------------------------


def _text_chunk(text: str) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=text, tool_calls=None),
                finish_reason=None,
            )
        ]
    )


def _tool_chunk(
    *, index: int, tc_id: str | None = None, name: str | None = None, args: str | None = None
) -> Any:
    td = SimpleNamespace(
        index=index,
        id=tc_id,
        function=SimpleNamespace(name=name, arguments=args) if (name or args) else None,
    )
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=None, tool_calls=[td]),
                finish_reason=None,
            )
        ]
    )


def _finish_chunk(reason: str) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=None, tool_calls=None),
                finish_reason=reason,
            )
        ]
    )


class _FakeAsyncIter:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def __aiter__(self) -> AsyncIterator[Any]:
        items = self._items

        async def _gen() -> AsyncIterator[Any]:
            for it in items:
                yield it

        return _gen()


class _FakeCompletions:
    def __init__(self, chunks: list[Any], captured: dict[str, Any]) -> None:
        self._chunks = chunks
        self._captured = captured

    async def create(self, **kwargs: Any) -> _FakeAsyncIter:
        self._captured["create_kwargs"] = kwargs
        return _FakeAsyncIter(self._chunks)


class _FakeAzureOpenAI:
    def __init__(self, chunks: list[Any], captured: dict[str, Any], **init: Any) -> None:
        captured["init_kwargs"] = init
        self.chat = SimpleNamespace(
            completions=_FakeCompletions(chunks, captured)
        )


def _patch_azure(
    monkeypatch: pytest.MonkeyPatch,
    chunks: list[Any],
) -> dict[str, Any]:
    import openai  # type: ignore[import-not-found]

    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        openai,
        "AsyncAzureOpenAI",
        lambda **kw: _FakeAzureOpenAI(chunks, captured, **kw),
    )
    return captured


_SPEC = ProviderSpec(
    name="azure",
    kind=ProviderKind.AZURE,
    api_key="azure-resource-key",
    base_url="https://my-resource.openai.azure.com",
)


async def _collect(prov: AzureProvider, **kw: Any) -> list[ProviderChunk]:
    chunks: list[ProviderChunk] = []
    async for c in prov.chat_stream(**kw):
        chunks.append(c)
    return chunks


# --------------------------------------------------------------------------
# Build / config
# --------------------------------------------------------------------------


def test_build_requires_base_url() -> None:
    with pytest.raises(ValueError, match="base_url"):
        AzureProvider.build(
            ProviderSpec(name="azure", kind=ProviderKind.AZURE, api_key="k")
        )


def test_build_uses_default_api_version() -> None:
    prov = AzureProvider.build(_SPEC)
    assert prov._api_version == DEFAULT_API_VERSION


def test_build_honours_params_api_version() -> None:
    prov = AzureProvider.build(
        ProviderSpec(
            name="azure",
            kind=ProviderKind.AZURE,
            api_key="k",
            base_url="https://r.openai.azure.com",
            params={"api_version": "2025-01-01-preview"},
        )
    )
    assert prov._api_version == "2025-01-01-preview"


# --------------------------------------------------------------------------
# URL / auth shape — the load-bearing Azure difference
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_azure_client_builds_deployment_url_and_api_key_auth() -> None:
    """A real AsyncAzureOpenAI routes to the deployment path with api-key auth.

    This is the load-bearing Azure difference vs. plain OpenAI:
    deployment-id in the path, ``api-version`` query param, and the
    ``api-key`` header instead of ``Authorization: Bearer``.
    """
    from openai._models import FinalRequestOptions  # type: ignore[import-not-found]

    prov = AzureProvider.build(_SPEC)
    client = prov._make_client()
    try:
        # The openai SDK builds the final httpx request lazily; ask it to
        # build what a chat-completion against deployment "gpt-4o" sends.
        opts = FinalRequestOptions.construct(
            method="post",
            url="/chat/completions",
            json_data={"model": "gpt-4o", "messages": []},
        )
        req = client._build_request(opts)
        url = str(req.url)
        assert "/openai/deployments/gpt-4o/chat/completions" in url
        assert f"api-version={DEFAULT_API_VERSION}" in url
        # Azure auth header is ``api-key`` — never ``Authorization: Bearer``.
        assert req.headers.get("api-key") == "azure-resource-key"
        assert "authorization" not in {k.lower() for k in req.headers}
    finally:
        await client.close()


# --------------------------------------------------------------------------
# Stream translation reuse
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_api_key_raises_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing key fails as an AuthError naming the Azure env var."""
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    prov = AzureProvider(base_url="https://r.openai.azure.com")
    with pytest.raises(AuthError, match=r"API key missing.*AZURE_OPENAI_API_KEY"):
        await _collect(prov, model="gpt-4o", messages=[])


@pytest.mark.asyncio
async def test_text_stream_flows_through(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_azure(
        monkeypatch,
        [_text_chunk("hi "), _text_chunk("azure"), _finish_chunk("stop")],
    )
    prov = AzureProvider.build(_SPEC)
    chunks = await _collect(
        prov, model="gpt-4o-deploy", messages=[{"role": "user", "content": "x"}]
    )

    texts = [c.text for c in chunks if c.kind == "token"]
    assert texts == ["hi ", "azure"]
    assert chunks[-1].kind == "done"
    assert chunks[-1].finish_reason == "stop"

    # The client was built with Azure-shaped init kwargs.
    init = captured["init_kwargs"]
    assert init["api_key"] == "azure-resource-key"
    assert init["azure_endpoint"] == "https://my-resource.openai.azure.com"
    assert init["api_version"] == DEFAULT_API_VERSION
    # The deployment id is passed through as ``model``.
    assert captured["create_kwargs"]["model"] == "gpt-4o-deploy"
    assert captured["create_kwargs"]["stream"] is True


@pytest.mark.asyncio
async def test_tool_call_stream_flows_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_azure(
        monkeypatch,
        [
            _tool_chunk(index=0, tc_id="call_x", name="FooPlugin", args=""),
            _tool_chunk(index=0, args='{"a":1}'),
            _finish_chunk("tool_calls"),
        ],
    )
    prov = AzureProvider.build(_SPEC)
    chunks = await _collect(
        prov, model="gpt-4o-deploy", messages=[{"role": "user", "content": "go"}]
    )

    assert chunks[0].kind == "tool_call_start"
    assert chunks[0].tool_call_id == "call_x"
    assert chunks[0].tool_name == "FooPlugin"
    deltas = [c.arguments_delta for c in chunks if c.kind == "tool_call_delta"]
    assert deltas == ['{"a":1}']
    assert any(c.kind == "tool_call_end" for c in chunks)
    assert chunks[-1].finish_reason == "tool_calls"


def test_supports_never_claims_via_prefix() -> None:
    # Azure deployments are operator-named — alias-addressed only.
    assert not AzureProvider.supports("gpt-4o")
