"""Provider client lifecycle — every chat_stream path must close the SDK client.

The OpenAI / Anthropic adapters construct an ``AsyncOpenAI`` /
``AsyncAnthropic`` per ``chat_stream`` call. The underlying ``httpx``
connection pool only releases its file descriptors + TLS sessions when
``client.close()`` (or the client's ``async with``) runs. If the adapter
drops the client without closing it — on success, on a mid-stream error,
or on a cancellation between chunks — every chat call leaks a pool entry
and the process eventually exhausts ulimit.

This module pins the lifecycle contract: a tracking double records every
``close()`` call. We exercise three paths per provider and assert exactly
one close per chat.

The 401-recovery path (OpenAI) also gets a check: when the first attempt
hits AuthError and the wrapper retries with a fresh client, BOTH clients
must close — the retry must not leak the abandoned first client.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from corlinman_providers import AnthropicProvider, OpenAIProvider

# ---------------------------------------------------------------------------
# OpenAI doubles
# ---------------------------------------------------------------------------


class _FakeOpenAIIter:
    def __init__(self, items: list[Any], *, raise_at: int | None = None) -> None:
        self._items = items
        self._raise_at = raise_at

    def __aiter__(self) -> AsyncIterator[Any]:
        items = self._items
        raise_at = self._raise_at

        async def _gen() -> AsyncIterator[Any]:
            for i, it in enumerate(items):
                if raise_at is not None and i == raise_at:
                    raise RuntimeError("mid-stream upstream blowup")
                yield it

        return _gen()


class _TrackingOpenAI:
    """``AsyncOpenAI`` double that records ``close()`` invocations.

    All instances share the same ``closes`` counter via a class-level
    list so a test can patch ``AsyncOpenAI`` and observe lifecycle
    across multiple constructions (e.g. the 401-retry path that builds
    two clients in succession).
    """

    closes: ClassVar[list[str]] = []
    instances: ClassVar[list[_TrackingOpenAI]] = []

    def __init__(
        self,
        *,
        chunks: list[Any],
        raise_at: int | None = None,
        first_create_raises: Exception | None = None,
        label: str = "openai",
        **_: Any,
    ) -> None:
        self._chunks = chunks
        self._raise_at = raise_at
        self._first_create_raises = first_create_raises
        self._label = label
        self._closed = False
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create),
        )
        type(self).instances.append(self)

    async def _create(self, **_: Any) -> _FakeOpenAIIter:
        if self._first_create_raises is not None:
            exc = self._first_create_raises
            self._first_create_raises = None
            raise exc
        return _FakeOpenAIIter(self._chunks, raise_at=self._raise_at)

    async def close(self) -> None:
        self._closed = True
        type(self).closes.append(self._label)


def _reset_openai_tracker() -> None:
    _TrackingOpenAI.closes.clear()
    _TrackingOpenAI.instances.clear()


def _patch_openai_factory(monkeypatch: pytest.MonkeyPatch, factory: Any) -> None:
    import openai  # type: ignore[import-not-found]

    monkeypatch.setattr(openai, "AsyncOpenAI", factory)


def _openai_text_chunk(text: str) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=text, tool_calls=None),
                finish_reason=None,
            )
        ]
    )


def _openai_finish_chunk(reason: str = "stop") -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=None, tool_calls=None),
                finish_reason=reason,
            )
        ]
    )


# ---------------------------------------------------------------------------
# OpenAI — success / error / cancel / 401-retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_closes_client_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full chat_stream completion must close the OpenAI client exactly once."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    _reset_openai_tracker()

    def _factory(**kwargs: Any) -> _TrackingOpenAI:
        return _TrackingOpenAI(
            chunks=[_openai_text_chunk("hi"), _openai_finish_chunk("stop")],
            **kwargs,
        )

    _patch_openai_factory(monkeypatch, _factory)

    prov = OpenAIProvider()
    async for _ in prov.chat_stream(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "x"}],
    ):
        pass

    assert len(_TrackingOpenAI.instances) == 1
    assert _TrackingOpenAI.closes == ["openai"], (
        f"expected exactly one close on success path, got {_TrackingOpenAI.closes}"
    )


@pytest.mark.asyncio
async def test_openai_closes_client_on_midstream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mid-stream exception must still close the OpenAI client."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    _reset_openai_tracker()

    def _factory(**kwargs: Any) -> _TrackingOpenAI:
        return _TrackingOpenAI(
            chunks=[
                _openai_text_chunk("partial"),
                _openai_text_chunk("never-arrives"),
            ],
            raise_at=1,
            **kwargs,
        )

    _patch_openai_factory(monkeypatch, _factory)

    prov = OpenAIProvider()
    with pytest.raises(Exception):  # noqa: B017 — mapped to CorlinmanError
        async for _ in prov.chat_stream(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "x"}],
        ):
            pass

    assert _TrackingOpenAI.closes == ["openai"], (
        f"expected close after mid-stream error, got {_TrackingOpenAI.closes}"
    )


@pytest.mark.asyncio
async def test_openai_closes_client_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling the consumer mid-stream must still close the client.

    The async generator's ``aclose()`` (triggered when the caller stops
    iterating or the surrounding task is cancelled) flows control into
    the generator's outstanding ``finally`` blocks — that's where the
    client close lives.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    _reset_openai_tracker()

    def _factory(**kwargs: Any) -> _TrackingOpenAI:
        return _TrackingOpenAI(
            chunks=[
                _openai_text_chunk("a"),
                _openai_text_chunk("b"),
                _openai_text_chunk("c"),
                _openai_finish_chunk("stop"),
            ],
            **kwargs,
        )

    _patch_openai_factory(monkeypatch, _factory)

    prov = OpenAIProvider()
    gen = prov.chat_stream(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "x"}],
    )

    # Consume one chunk then explicitly close the generator — simulates
    # a client disconnect (FastAPI/Starlette aclose-on-disconnect) or a
    # caller doing ``break`` partway through.
    received = 0
    async for _ in gen:
        received += 1
        if received >= 1:
            break
    await gen.aclose()

    assert _TrackingOpenAI.closes == ["openai"], (
        f"expected close after cancellation, got {_TrackingOpenAI.closes}"
    )


@pytest.mark.asyncio
async def test_openai_closes_both_clients_on_401_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When 401-recovery rebuilds the client, the stale client must close too.

    Otherwise the first ``AsyncOpenAI`` (constructed with the rotated-
    out key) leaks its httpx pool while the second client takes over.
    """
    import openai  # type: ignore[import-not-found]

    monkeypatch.setenv("OPENAI_API_KEY", "old-key")
    _reset_openai_tracker()

    state = {"n": 0}

    def _factory(**kwargs: Any) -> _TrackingOpenAI:
        state["n"] += 1
        if state["n"] == 1:
            # First client: ``create()`` immediately raises auth error.
            err = openai.AuthenticationError.__new__(openai.AuthenticationError)
            Exception.__init__(err, "401 Unauthorized")
            err.status_code = 401
            err.response = SimpleNamespace(status_code=401)
            return _TrackingOpenAI(
                chunks=[],
                first_create_raises=err,
                label="first",
                **kwargs,
            )
        # Second client (post-rotation): stream completes successfully.
        return _TrackingOpenAI(
            chunks=[_openai_text_chunk("ok"), _openai_finish_chunk("stop")],
            label="second",
            **kwargs,
        )

    _patch_openai_factory(monkeypatch, _factory)

    # Construct adapter with the OLD key, then rotate the env — this
    # is the canonical "operator rotated mid-flight" sequence the
    # recovery wrapper exists to handle.
    prov = OpenAIProvider()
    assert prov._api_key == "old-key"
    monkeypatch.setenv("OPENAI_API_KEY", "rotated-key")

    chunks: list[Any] = []
    async for c in prov.chat_stream(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "x"}],
    ):
        chunks.append(c)

    # Both clients must close — the stale one before retry, the
    # successful one after the stream finishes.
    assert sorted(_TrackingOpenAI.closes) == ["first", "second"], (
        f"both clients must close across 401 retry; got {_TrackingOpenAI.closes}"
    )
    # Sanity: stream produced its content.
    assert any(c.kind == "token" and c.text == "ok" for c in chunks)


# ---------------------------------------------------------------------------
# Anthropic doubles
# ---------------------------------------------------------------------------


class _FakeAnthropicStream:
    def __init__(
        self,
        events: list[Any],
        *,
        stop_reason: str = "end_turn",
        raise_at: int | None = None,
    ) -> None:
        self._events = events
        self._stop_reason = stop_reason
        self._raise_at = raise_at

    async def __aenter__(self) -> _FakeAnthropicStream:
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    def __aiter__(self) -> AsyncIterator[Any]:
        events = self._events
        raise_at = self._raise_at

        async def _gen() -> AsyncIterator[Any]:
            for i, e in enumerate(events):
                if raise_at is not None and i == raise_at:
                    raise RuntimeError("mid-stream upstream blowup")
                yield e

        return _gen()

    async def get_final_message(self) -> Any:
        return SimpleNamespace(stop_reason=self._stop_reason)


class _TrackingAnthropic:
    closes: ClassVar[list[str]] = []
    instances: ClassVar[list[_TrackingAnthropic]] = []

    def __init__(
        self,
        *,
        events: list[Any],
        stop_reason: str = "end_turn",
        raise_at: int | None = None,
        label: str = "anthropic",
        **_: Any,
    ) -> None:
        stream = _FakeAnthropicStream(
            events, stop_reason=stop_reason, raise_at=raise_at
        )
        self.messages = SimpleNamespace(stream=lambda **__: stream)
        self._label = label
        self._closed = False
        type(self).instances.append(self)

    async def close(self) -> None:
        self._closed = True
        type(self).closes.append(self._label)


def _reset_anthropic_tracker() -> None:
    _TrackingAnthropic.closes.clear()
    _TrackingAnthropic.instances.clear()


def _patch_anthropic_factory(monkeypatch: pytest.MonkeyPatch, factory: Any) -> None:
    import anthropic  # type: ignore[import-not-found]

    monkeypatch.setattr(anthropic, "AsyncAnthropic", factory)


def _anthropic_text_event(text: str) -> Any:
    return SimpleNamespace(
        type="content_block_delta",
        index=0,
        delta=SimpleNamespace(type="text_delta", text=text),
    )


def _anthropic_block_stop_event(index: int = 0) -> Any:
    return SimpleNamespace(type="content_block_stop", index=index)


# ---------------------------------------------------------------------------
# Anthropic — success / error / cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_closes_client_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _reset_anthropic_tracker()

    def _factory(**kwargs: Any) -> _TrackingAnthropic:
        return _TrackingAnthropic(
            events=[_anthropic_text_event("hi"), _anthropic_block_stop_event(0)],
            **kwargs,
        )

    _patch_anthropic_factory(monkeypatch, _factory)

    prov = AnthropicProvider()
    async for _ in prov.chat_stream(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "x"}],
    ):
        pass

    assert _TrackingAnthropic.closes == ["anthropic"], (
        f"expected exactly one close on success, got {_TrackingAnthropic.closes}"
    )


@pytest.mark.asyncio
async def test_anthropic_closes_client_on_midstream_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _reset_anthropic_tracker()

    def _factory(**kwargs: Any) -> _TrackingAnthropic:
        return _TrackingAnthropic(
            events=[
                _anthropic_text_event("partial"),
                _anthropic_text_event("never-arrives"),
            ],
            raise_at=1,
            **kwargs,
        )

    _patch_anthropic_factory(monkeypatch, _factory)

    prov = AnthropicProvider()
    with pytest.raises(Exception):  # noqa: B017 — mapped to CorlinmanError
        async for _ in prov.chat_stream(
            model="claude-sonnet-4-5",
            messages=[{"role": "user", "content": "x"}],
        ):
            pass

    assert _TrackingAnthropic.closes == ["anthropic"], (
        f"expected close after mid-stream error, got {_TrackingAnthropic.closes}"
    )


@pytest.mark.asyncio
async def test_anthropic_closes_client_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _reset_anthropic_tracker()

    def _factory(**kwargs: Any) -> _TrackingAnthropic:
        return _TrackingAnthropic(
            events=[
                _anthropic_text_event("a"),
                _anthropic_text_event("b"),
                _anthropic_text_event("c"),
                _anthropic_block_stop_event(0),
            ],
            **kwargs,
        )

    _patch_anthropic_factory(monkeypatch, _factory)

    prov = AnthropicProvider()
    gen = prov.chat_stream(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "x"}],
    )

    received = 0
    async for _ in gen:
        received += 1
        if received >= 1:
            break
    await gen.aclose()

    assert _TrackingAnthropic.closes == ["anthropic"], (
        f"expected close after cancellation, got {_TrackingAnthropic.closes}"
    )


# ---------------------------------------------------------------------------
# Sanity: AsyncIterator cancellation also via task.cancel() route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_closes_client_on_task_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling the surrounding task (not just .aclose()) still closes."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    _reset_openai_tracker()

    def _factory(**kwargs: Any) -> _TrackingOpenAI:
        return _TrackingOpenAI(
            chunks=[
                _openai_text_chunk("a"),
                _openai_text_chunk("b"),
                _openai_text_chunk("c"),
                _openai_finish_chunk("stop"),
            ],
            **kwargs,
        )

    _patch_openai_factory(monkeypatch, _factory)

    prov = OpenAIProvider()

    async def _consume_partial() -> None:
        gen = prov.chat_stream(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "x"}],
        )
        try:
            async for _ in gen:
                # Yield control so the cancel can land between chunks.
                await asyncio.sleep(0)
        finally:
            await gen.aclose()

    task = asyncio.create_task(_consume_partial())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _TrackingOpenAI.closes == ["openai"], (
        f"expected close after task cancel, got {_TrackingOpenAI.closes}"
    )
