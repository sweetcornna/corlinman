"""Codex provider client lifecycle — every chat_stream path must close the SDK client.

:class:`corlinman_providers.codex_provider.CodexProvider.chat_stream`
constructs an ``AsyncOpenAI`` (pointed at the Codex backend) per call via
``_make_client``. The underlying ``httpx`` connection pool only releases
its file descriptors + TLS sessions when ``client.close()`` runs — the
``stream(...)`` context manager only closes the *response*, not the
owning client's pool. If the adapter drops the client without closing it
— on success, on a mid-stream error, or on a cancellation between chunks
— every Codex chat turn leaks a pool entry.

This pins the same lifecycle contract R1-003 enforced for the OpenAI /
Anthropic providers (see ``test_provider_client_lifecycle.py``), which
Codex was missed by.

The token-recovery path (401 ``token_invalidated`` → refresh → rebuild
client → retry) gets its own check: BOTH the abandoned first client and
the second client must close. Otherwise the first ``AsyncOpenAI`` leaks
its httpx pool while the second takes over.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest
from corlinman_providers._codex_oauth import CodexOAuthCredential
from corlinman_providers.codex_provider import CodexProvider

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


def _fresh_cred() -> CodexOAuthCredential:
    """A credential far from expiry so ``_ensure_fresh`` is a no-op."""
    future_ms = int(time.time() * 1000) + 3_600_000
    return CodexOAuthCredential(
        access_token="good-token", refresh_token="rt-good", expires_at_ms=future_ms
    )


def _text_event(text: str) -> Any:
    return SimpleNamespace(type="response.output_text.delta", delta=text)


class _FakeStream:
    """Mimics ``client.responses.stream(**kwargs)`` — an async-with CM that
    yields a list of events, optionally raising mid-stream."""

    def __init__(self, events: list[Any], *, raise_at: int | None = None) -> None:
        self._events = events
        self._raise_at = raise_at

    async def __aenter__(self) -> _FakeStream:
        return self

    async def __aexit__(self, *_: Any) -> bool:
        return False

    def __aiter__(self) -> AsyncIterator[Any]:
        events = self._events
        raise_at = self._raise_at

        async def _gen() -> AsyncIterator[Any]:
            for i, e in enumerate(events):
                if raise_at is not None and i == raise_at:
                    raise RuntimeError("mid-stream upstream blowup")
                yield e

        return _gen()


class _TrackingCodexClient:
    """``AsyncOpenAI`` double that records ``close()`` invocations.

    Instances share a class-level ``closes`` list so a test patching
    ``_make_client`` can observe lifecycle across multiple constructions
    (the token-recovery path builds two clients in succession).
    """

    closes: ClassVar[list[str]] = []
    instances: ClassVar[list[_TrackingCodexClient]] = []

    def __init__(
        self,
        *,
        events: list[Any],
        raise_at: int | None = None,
        open_raises: Exception | None = None,
        label: str = "codex",
    ) -> None:
        self._events = events
        self._raise_at = raise_at
        self._open_raises = open_raises
        self._label = label
        self._closed = False
        self.responses = SimpleNamespace(stream=self._stream)
        type(self).instances.append(self)

    def _stream(self, **_kwargs: Any) -> Any:
        if self._open_raises is not None:
            exc = self._open_raises

            class _RaisingOpen:
                async def __aenter__(self) -> Any:
                    raise exc

                async def __aexit__(self, *_: Any) -> bool:
                    return False

                def __aiter__(self) -> Any:
                    return self

                async def __anext__(self) -> Any:
                    raise StopAsyncIteration

            return _RaisingOpen()
        return _FakeStream(self._events, raise_at=self._raise_at)

    async def close(self) -> None:
        self._closed = True
        type(self).closes.append(self._label)


def _reset_tracker() -> None:
    _TrackingCodexClient.closes.clear()
    _TrackingCodexClient.instances.clear()


def _fake_invalidated_401() -> Exception:
    """Exception that looks like a Codex ``token_invalidated`` 401."""

    class _AuthError(Exception):
        pass

    err = _AuthError(
        "Error code: 401 - {'error': {'code': 'token_invalidated', 'message': "
        "'Your authentication token has been invalidated.'}}"
    )
    err.status_code = 401  # type: ignore[attr-defined]
    resp = MagicMock()
    resp.status_code = 401
    resp.json.return_value = {
        "error": {
            "code": "token_invalidated",
            "message": "Your authentication token has been invalidated.",
        }
    }
    err.response = resp  # type: ignore[attr-defined]
    return err


# ---------------------------------------------------------------------------
# success / error / cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_codex_closes_client_on_success() -> None:
    """Full chat_stream completion must close the Codex client exactly once."""
    _reset_tracker()
    prov = CodexProvider(credential=_fresh_cred())

    client = _TrackingCodexClient(
        events=[
            _text_event("hi"),
            SimpleNamespace(type="response.completed", response=None),
        ]
    )

    with patch.object(prov, "_make_client", return_value=client):
        async for _ in prov.chat_stream(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "x"}],
        ):
            pass

    assert len(_TrackingCodexClient.instances) == 1
    assert _TrackingCodexClient.closes == ["codex"], (
        f"expected exactly one close on success path, got {_TrackingCodexClient.closes}"
    )


@pytest.mark.asyncio
async def test_codex_closes_client_on_midstream_error() -> None:
    """A mid-stream exception must still close the Codex client."""
    _reset_tracker()
    prov = CodexProvider(credential=_fresh_cred())

    client = _TrackingCodexClient(
        events=[_text_event("partial"), _text_event("never-arrives")],
        raise_at=1,
    )

    with patch.object(prov, "_make_client", return_value=client):
        # chat_stream maps a mid-stream error to a done/error chunk rather
        # than raising, so we just drain it.
        async for _ in prov.chat_stream(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "x"}],
        ):
            pass

    assert _TrackingCodexClient.closes == ["codex"], (
        f"expected close after mid-stream error, got {_TrackingCodexClient.closes}"
    )


@pytest.mark.asyncio
async def test_codex_closes_client_on_cancellation() -> None:
    """Cancelling the consumer mid-stream must still close the client.

    The async generator's ``aclose()`` (triggered when the caller stops
    iterating) flows control into the generator's outstanding ``finally``
    blocks — that's where the client close must live.
    """
    _reset_tracker()
    prov = CodexProvider(credential=_fresh_cred())

    client = _TrackingCodexClient(
        events=[
            _text_event("a"),
            _text_event("b"),
            _text_event("c"),
            SimpleNamespace(type="response.completed", response=None),
        ]
    )

    with patch.object(prov, "_make_client", return_value=client):
        gen = prov.chat_stream(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "x"}],
        )
        received = 0
        async for _ in gen:
            received += 1
            if received >= 1:
                break
        await gen.aclose()

    assert _TrackingCodexClient.closes == ["codex"], (
        f"expected close after cancellation, got {_TrackingCodexClient.closes}"
    )


@pytest.mark.asyncio
async def test_codex_closes_client_on_task_cancel() -> None:
    """Cancelling the surrounding task (not just .aclose()) still closes."""
    _reset_tracker()
    prov = CodexProvider(credential=_fresh_cred())

    client = _TrackingCodexClient(
        events=[
            _text_event("a"),
            _text_event("b"),
            _text_event("c"),
            SimpleNamespace(type="response.completed", response=None),
        ]
    )

    with patch.object(prov, "_make_client", return_value=client):

        async def _consume_partial() -> None:
            gen = prov.chat_stream(
                model="gpt-5.5",
                messages=[{"role": "user", "content": "x"}],
            )
            try:
                async for _ in gen:
                    await asyncio.sleep(0)
            finally:
                await gen.aclose()

        task = asyncio.create_task(_consume_partial())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert _TrackingCodexClient.closes == ["codex"], (
        f"expected close after task cancel, got {_TrackingCodexClient.closes}"
    )


# ---------------------------------------------------------------------------
# token-recovery double-leak
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_codex_closes_both_clients_on_token_recovery(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When 401-recovery rebuilds the client, the stale client must close too.

    Otherwise the first ``AsyncOpenAI`` (built with the invalidated token)
    leaks its httpx pool while the recovered second client takes over.
    """
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(
        '{"tokens": {"access_token": "stale-token", "refresh_token": "rt-good"}}',
        encoding="utf-8",
    )
    _reset_tracker()

    prov = CodexProvider(credential=_fresh_cred())

    fresh = CodexOAuthCredential(
        access_token="brand-new",
        refresh_token="rt-good",
        expires_at_ms=int(time.time() * 1000) + 3_600_000,
    )

    async def _fake_refresh(*, refresh_token: str) -> CodexOAuthCredential:
        return fresh

    # First _make_client → client whose stream open raises 401-invalidated.
    # Second _make_client (post-recovery) → client that streams a token.
    first = _TrackingCodexClient(
        events=[], open_raises=_fake_invalidated_401(), label="first"
    )
    second = _TrackingCodexClient(events=[_text_event("ok")], label="second")
    # The tracker's __init__ already appended both to instances; clear so
    # the side_effect-driven order is what the chat_stream actually pulls.
    _reset_tracker()
    clients = iter([first, second])

    with (
        patch(
            "corlinman_providers.codex_provider.refresh_codex_token", _fake_refresh
        ),
        patch.object(prov, "_make_client", side_effect=lambda: next(clients)),
    ):
        chunks: list[Any] = []
        async for c in prov.chat_stream(
            model="gpt-5.5",
            messages=[{"role": "user", "content": "x"}],
        ):
            chunks.append(c)

    # Both clients must close — the stale one before/at retry, the
    # successful one after the stream finishes.
    assert sorted(_TrackingCodexClient.closes) == ["first", "second"], (
        f"both clients must close across token recovery; got {_TrackingCodexClient.closes}"
    )
    # Sanity: recovery produced the real content.
    assert any(c.kind == "token" and c.text == "ok" for c in chunks)
