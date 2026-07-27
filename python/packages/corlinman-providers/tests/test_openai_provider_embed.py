"""G2 (a): ``OpenAIProvider.embed`` over a mocked embeddings endpoint.

Pins the real implementation that replaced the ``NotImplementedError``
stub: order-by-index alignment, batch splitting, input truncation, the
missing-credential :class:`AuthError`, and the SDK-error → CorlinmanError
mapping shared with ``chat_stream``.
"""

from __future__ import annotations

from typing import Any

import pytest
from corlinman_providers.failover import AuthError, CorlinmanError, FormatError
from corlinman_providers.openai_provider import OpenAIProvider


class _Item:
    def __init__(self, index: int, embedding: list[float]) -> None:
        self.index = index
        self.embedding = embedding


class _Response:
    def __init__(self, items: list[_Item]) -> None:
        self.data = items


class _FakeEmbeddings:
    """Echoes one deterministic vector per input; records every call."""

    def __init__(self, *, fail: Exception | None = None, drop_one: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail = fail
        self._drop_one = drop_one

    async def create(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        if self._fail is not None:
            raise self._fail
        batch = list(kwargs["input"])
        if self._drop_one:
            batch = batch[:-1]
        # Deliberately reversed index order to prove re-alignment happens.
        items = [
            _Item(index=i, embedding=[float(i), float(len(text))])
            for i, text in enumerate(batch)
        ]
        return _Response(list(reversed(items)))


class _FakeClient:
    def __init__(self, embeddings: _FakeEmbeddings) -> None:
        self.embeddings = embeddings
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _patch_client(
    monkeypatch: pytest.MonkeyPatch, provider: OpenAIProvider, embeddings: _FakeEmbeddings
) -> _FakeClient:
    client = _FakeClient(embeddings)
    monkeypatch.setattr(provider, "_make_client", lambda: client)
    return client


@pytest.mark.asyncio
async def test_embed_returns_vectors_aligned_to_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIProvider(api_key="sk-test")
    embeddings = _FakeEmbeddings()
    client = _patch_client(monkeypatch, provider, embeddings)

    vectors = await provider.embed(
        model="text-embedding-3-small", inputs=["ab", "cdef"]
    )

    # Response arrived index-reversed; output must still align 1:1.
    assert vectors == [[0.0, 2.0], [1.0, 4.0]]
    assert embeddings.calls[0]["model"] == "text-embedding-3-small"
    assert embeddings.calls[0]["input"] == ["ab", "cdef"]
    assert client.closed is True


@pytest.mark.asyncio
async def test_embed_splits_oversized_input_lists_into_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import corlinman_providers.openai_provider as mod

    monkeypatch.setattr(mod, "_EMBED_MAX_BATCH", 2)
    provider = OpenAIProvider(api_key="sk-test")
    embeddings = _FakeEmbeddings()
    _patch_client(monkeypatch, provider, embeddings)

    vectors = await provider.embed(model="emb", inputs=["a", "b", "c"])

    assert len(vectors) == 3
    assert [call["input"] for call in embeddings.calls] == [["a", "b"], ["c"]]


@pytest.mark.asyncio
async def test_embed_truncates_over_long_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import corlinman_providers.openai_provider as mod

    monkeypatch.setattr(mod, "_EMBED_MAX_INPUT_CHARS", 4)
    provider = OpenAIProvider(api_key="sk-test")
    embeddings = _FakeEmbeddings()
    _patch_client(monkeypatch, provider, embeddings)

    await provider.embed(model="emb", inputs=["abcdefgh", ""])

    # Over-long input truncated; empty input replaced by a single space
    # (the API rejects "" and alignment must be preserved).
    assert embeddings.calls[0]["input"] == ["abcd", " "]


@pytest.mark.asyncio
async def test_embed_without_credential_raises_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    provider = OpenAIProvider()
    with pytest.raises(AuthError, match=r"API key missing.*OPENAI_API_KEY"):
        await provider.embed(model="emb", inputs=["hello"])


@pytest.mark.asyncio
async def test_embed_empty_inputs_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIProvider(api_key="sk-test")

    def _boom() -> Any:  # pragma: no cover — must not be reached
        raise AssertionError("no client should be constructed for []")

    monkeypatch.setattr(provider, "_make_client", _boom)
    assert await provider.embed(model="emb", inputs=[]) == []


@pytest.mark.asyncio
async def test_embed_maps_sdk_errors_and_still_closes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIProvider(api_key="sk-test")
    embeddings = _FakeEmbeddings(fail=RuntimeError("boom"))
    client = _patch_client(monkeypatch, provider, embeddings)

    with pytest.raises(CorlinmanError, match="boom"):
        await provider.embed(model="emb", inputs=["x"])
    assert client.closed is True


@pytest.mark.asyncio
async def test_embed_vector_count_mismatch_is_a_format_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIProvider(api_key="sk-test")
    embeddings = _FakeEmbeddings(drop_one=True)
    _patch_client(monkeypatch, provider, embeddings)

    with pytest.raises(FormatError, match="1 vectors for 2 inputs"):
        await provider.embed(model="emb", inputs=["x", "y"])


@pytest.mark.asyncio
async def test_embed_passes_extra_params_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAIProvider(api_key="sk-test")
    embeddings = _FakeEmbeddings()
    _patch_client(monkeypatch, provider, embeddings)

    await provider.embed(model="emb", inputs=["x"], extra={"dimensions": 256})

    assert embeddings.calls[0]["dimensions"] == 256
