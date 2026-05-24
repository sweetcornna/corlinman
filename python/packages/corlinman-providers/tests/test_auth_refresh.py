"""Reactive 401 auth-refresh tests.

Covers the cross-provider helper :func:`with_401_recovery` plus per-provider
integration tests that monkeypatch each vendor SDK / HTTP transport to
return 401 on the first attempt and 200 on the second. The shared
assertion: after the env var (or AWS triple) is rotated mid-stream, the
second attempt picks up the new key and the chat completes successfully.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from corlinman_providers import (
    AzureProvider,
    BedrockProvider,
    GoogleProvider,
    OpenAICompatibleProvider,
    OpenAIProvider,
    ProviderKind,
    ProviderSpec,
    with_401_recovery,
)
from corlinman_providers.failover import AuthError

# ---------------------------------------------------------------------------
# with_401_recovery — the shared helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_401_recovery_no_error_returns_value() -> None:
    """When ``call()`` succeeds first try, ``refresh`` is never invoked."""
    refresh_called = {"n": 0}

    async def _call() -> str:
        return "ok"

    async def _refresh() -> bool:
        refresh_called["n"] += 1
        return True

    result = await with_401_recovery(_call, refresh=_refresh)
    assert result == "ok"
    assert refresh_called["n"] == 0


@pytest.mark.asyncio
async def test_with_401_recovery_retries_after_rotation() -> None:
    """A first-attempt :class:`AuthError` + ``refresh→True`` retries once."""
    attempts = {"n": 0}

    async def _call() -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise AuthError("401 first time", status_code=401)
        return "second-attempt-ok"

    async def _refresh() -> bool:
        return True

    result = await with_401_recovery(_call, refresh=_refresh)
    assert result == "second-attempt-ok"
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_with_401_recovery_reraises_when_refresh_says_no_rotation() -> None:
    """If ``refresh()`` returns False the original 401 propagates verbatim."""
    attempts = {"n": 0}

    async def _call() -> str:
        attempts["n"] += 1
        raise AuthError("401", status_code=401)

    async def _refresh() -> bool:
        return False

    with pytest.raises(AuthError):
        await with_401_recovery(_call, refresh=_refresh)
    assert attempts["n"] == 1  # never retried


@pytest.mark.asyncio
async def test_with_401_recovery_reraises_on_second_401() -> None:
    """A second 401 (after rotation) is fatal — re-raised, no third try."""
    attempts = {"n": 0}

    async def _call() -> str:
        attempts["n"] += 1
        raise AuthError(f"401 attempt {attempts['n']}", status_code=401)

    async def _refresh() -> bool:
        return True

    with pytest.raises(AuthError, match="attempt 2"):
        await with_401_recovery(_call, refresh=_refresh)
    assert attempts["n"] == 2


@pytest.mark.asyncio
async def test_with_401_recovery_refresh_exception_reraises_original() -> None:
    """An exception inside ``refresh`` doesn't kill the request flow — the
    original :class:`AuthError` is raised with the refresh error chained."""
    async def _call() -> str:
        raise AuthError("401", status_code=401)

    async def _refresh() -> bool:
        raise RuntimeError("refresh blew up")

    with pytest.raises(AuthError) as info:
        await with_401_recovery(_call, refresh=_refresh)
    assert isinstance(info.value.__cause__, RuntimeError)


# ---------------------------------------------------------------------------
# OpenAI provider — env-var rotation
# ---------------------------------------------------------------------------


def _make_401_then_200_openai_factory(
    captured_keys: list[str],
    chunks_after_recover: list[Any],
) -> Any:
    """Build an ``AsyncOpenAI`` factory that fails the first create() with a
    401 and succeeds the second time with ``chunks_after_recover``.

    Records the ``api_key`` each call was constructed with so the test can
    assert the second attempt picked up the rotated env var.
    """
    import openai  # type: ignore[import-not-found]

    authentication_error_cls = openai.AuthenticationError  # type: ignore[attr-defined]

    state = {"n": 0}

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
        async def create(self, **kwargs: Any) -> Any:
            state["n"] += 1
            if state["n"] == 1:
                # First attempt: 401. Construct a real ``AuthenticationError``
                # so the provider's ``_map_openai_error`` maps it to AuthError.
                err = authentication_error_cls.__new__(authentication_error_cls)
                Exception.__init__(err, "401 Unauthorized")
                err.status_code = 401
                err.response = SimpleNamespace(status_code=401)
                raise err
            return _FakeAsyncIter(chunks_after_recover)

    class _FakeChat:
        completions = _FakeCompletions()

    class _FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured_keys.append(kwargs.get("api_key", ""))
            self.chat = _FakeChat()

    return _FakeOpenAI


@pytest.mark.asyncio
async def test_openai_provider_reactive_401_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAI provider sees 401, env-var was rotated → retry succeeds."""
    import openai  # type: ignore[import-not-found]

    monkeypatch.setenv("OPENAI_API_KEY", "old-key")
    prov = OpenAIProvider()
    assert prov._api_key == "old-key"

    captured_keys: list[str] = []
    finish_chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=None, tool_calls=None),
                finish_reason="stop",
            )
        ]
    )
    text_chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content="hi", tool_calls=None),
                finish_reason=None,
            )
        ]
    )
    factory = _make_401_then_200_openai_factory(
        captured_keys, [text_chunk, finish_chunk]
    )
    monkeypatch.setattr(openai, "AsyncOpenAI", factory)

    # Rotate the env var BEFORE the provider sees the 401 — this
    # simulates the operator updating their secret between adapter
    # construction and the next chat turn.
    monkeypatch.setenv("OPENAI_API_KEY", "rotated-key")

    chunks: list[Any] = []
    async for c in prov.chat_stream(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "x"}],
    ):
        chunks.append(c)

    # Two client constructions: first with the stale key, second with
    # the rotated one.
    assert captured_keys == ["old-key", "rotated-key"]
    assert prov._api_key == "rotated-key"
    # The stream produced its content.
    texts = [c.text for c in chunks if c.kind == "token"]
    assert texts == ["hi"]
    assert chunks[-1].kind == "done"
    assert chunks[-1].finish_reason == "stop"


@pytest.mark.asyncio
async def test_openai_provider_401_no_rotation_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without an env rotation, the 401 propagates — no infinite retries."""
    import openai  # type: ignore[import-not-found]

    monkeypatch.setenv("OPENAI_API_KEY", "stale-key")
    prov = OpenAIProvider()

    captured_keys: list[str] = []
    factory = _make_401_then_200_openai_factory(captured_keys, [])
    monkeypatch.setattr(openai, "AsyncOpenAI", factory)

    # Env var unchanged — refresh should return False, no retry happens.
    with pytest.raises(AuthError):
        async for _ in prov.chat_stream(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "x"}],
        ):
            pass

    # Exactly one client construction — no retry.
    assert captured_keys == ["stale-key"]


# ---------------------------------------------------------------------------
# OpenAI-compatible provider — env-var rotation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_compatible_reactive_401_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAICompatibleProvider re-reads OPENAI_API_KEY on 401."""
    import openai  # type: ignore[import-not-found]

    monkeypatch.setenv("OPENAI_API_KEY", "compat-old")
    prov = OpenAICompatibleProvider(
        base_url="https://vllm.example.com/v1",
        api_key=None,
    )
    assert prov._api_key == "compat-old"

    captured_keys: list[str] = []
    text_chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content="hi-vllm", tool_calls=None),
                finish_reason=None,
            )
        ]
    )
    finish_chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=None, tool_calls=None),
                finish_reason="stop",
            )
        ]
    )
    factory = _make_401_then_200_openai_factory(
        captured_keys, [text_chunk, finish_chunk]
    )
    monkeypatch.setattr(openai, "AsyncOpenAI", factory)

    monkeypatch.setenv("OPENAI_API_KEY", "compat-rotated")
    chunks: list[Any] = []
    async for c in prov.chat_stream(
        model="vllm-llama-3",
        messages=[{"role": "user", "content": "x"}],
    ):
        chunks.append(c)

    assert captured_keys == ["compat-old", "compat-rotated"]
    assert prov._api_key == "compat-rotated"
    assert any(c.kind == "token" and c.text == "hi-vllm" for c in chunks)


# ---------------------------------------------------------------------------
# Azure provider — AZURE_OPENAI_API_KEY env rotation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_azure_provider_reactive_401_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Azure provider re-reads AZURE_OPENAI_API_KEY on 401."""
    import openai  # type: ignore[import-not-found]

    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-old")
    prov = AzureProvider(
        base_url="https://resource.openai.azure.com",
    )
    assert prov._api_key == "azure-old"

    captured_keys: list[str] = []
    text_chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content="azure-hi", tool_calls=None),
                finish_reason=None,
            )
        ]
    )
    finish_chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=None, tool_calls=None),
                finish_reason="stop",
            )
        ]
    )
    factory = _make_401_then_200_openai_factory(
        captured_keys, [text_chunk, finish_chunk]
    )
    # Azure uses AsyncAzureOpenAI rather than AsyncOpenAI.
    monkeypatch.setattr(openai, "AsyncAzureOpenAI", factory)

    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "azure-rotated")
    chunks: list[Any] = []
    async for c in prov.chat_stream(
        model="gpt-4o-deploy",
        messages=[{"role": "user", "content": "x"}],
    ):
        chunks.append(c)

    assert captured_keys == ["azure-old", "azure-rotated"]
    assert prov._api_key == "azure-rotated"
    assert any(c.kind == "token" and c.text == "azure-hi" for c in chunks)


# ---------------------------------------------------------------------------
# Google (Gemini) provider — GOOGLE_API_KEY env rotation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_google_provider_reactive_401_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Google provider re-reads GOOGLE_API_KEY on 401."""
    monkeypatch.setenv("GOOGLE_API_KEY", "google-old")
    prov = GoogleProvider()
    assert prov._api_key == "google-old"

    state = {"n": 0}
    captured_keys: list[str] = []

    class _FakeChunk:
        text = "google-hi"

    async def _stream_gen() -> AsyncIterator[Any]:
        yield _FakeChunk()

    class _FakeAioModels:
        async def generate_content_stream(self, **kwargs: Any) -> Any:
            state["n"] += 1
            if state["n"] == 1:
                # Build a 401-ish exception. Google SDK's ``ClientError``
                # carries ``code = 401``; we duck-type the smallest shape
                # the mapping needs to recognise it as AuthError.
                exc = RuntimeError("API key invalid (401 Unauthorized)")
                exc.code = 401  # type: ignore[attr-defined]
                raise exc
            return _stream_gen()

    class _FakeAio:
        models = _FakeAioModels()

    class _FakeClient:
        def __init__(self, *, api_key: str) -> None:
            captured_keys.append(api_key)
            self.aio = _FakeAio()

    import google.genai as genai  # type: ignore[import-not-found]

    monkeypatch.setattr(genai, "Client", _FakeClient)

    monkeypatch.setenv("GOOGLE_API_KEY", "google-rotated")
    chunks: list[Any] = []
    async for c in prov.chat_stream(
        model="gemini-1.5-pro",
        messages=[{"role": "user", "content": "x"}],
    ):
        chunks.append(c)

    assert captured_keys == ["google-old", "google-rotated"]
    assert prov._api_key == "google-rotated"
    texts = [c.text for c in chunks if c.kind == "token"]
    assert texts == ["google-hi"]


# ---------------------------------------------------------------------------
# Bedrock provider — AWS_* env rotation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bedrock_provider_reactive_401_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bedrock re-signs with rotated AWS_ACCESS_KEY_ID after a 401."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDOLD")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "sk-old")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    # Clear any session token left over from the host env so the
    # baseline + rotation are deterministic.
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)

    spec = ProviderSpec(
        name="bedrock",
        kind=ProviderKind.BEDROCK,
        params={"region": "us-east-1"},
    )
    prov = BedrockProvider.build(spec)
    assert prov._access_key_id == "AKIDOLD"

    from .test_aws_eventstream import encode_message

    def _ok_stream_body() -> bytes:
        chunk = json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}).encode()
        import base64

        payload = json.dumps({"bytes": base64.b64encode(chunk).decode()}).encode()
        return encode_message(
            {":event-type": "chunk", ":message-type": "event"}, payload
        )

    attempts = {"n": 0}
    captured_keys: list[str] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        # The SigV4 ``authorization`` header carries the access key id
        # in the ``Credential=...`` segment — pull it out so the test
        # can verify the second request signed with the rotated key.
        # Header shape: ``AWS4-HMAC-SHA256 Credential=<AK>/<date>/...,
        # SignedHeaders=..., Signature=...``.
        auth_hdr = request.headers.get("authorization", "")
        # Split off the algorithm prefix, then split the rest by ", ".
        if " " in auth_hdr:
            _, rest = auth_hdr.split(" ", 1)
        else:
            rest = auth_hdr
        for part in rest.split(", "):
            if part.startswith("Credential="):
                captured_keys.append(part.split("=", 1)[1].split("/", 1)[0])
                break
        if attempts["n"] == 1:
            return httpx.Response(401, text="invalid signature / access key")
        return httpx.Response(200, content=_ok_stream_body())

    transport = httpx.MockTransport(_handler)
    real_client = httpx.AsyncClient

    def _factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)

    # Rotate AWS credentials before the first 401 arrives.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDNEW")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "sk-new")

    chunks: list[Any] = []
    async for c in prov.chat_stream(
        model="anthropic.claude-3-5-sonnet-20241022-v2:0",
        messages=[{"role": "user", "content": "x"}],
    ):
        chunks.append(c)

    assert attempts["n"] == 2
    assert captured_keys == ["AKIDOLD", "AKIDNEW"]
    assert prov._access_key_id == "AKIDNEW"
    assert prov._secret_access_key == "sk-new"
    assert chunks[-1].kind == "done"


# ---------------------------------------------------------------------------
# Anthropic provider — already self-heals; non-regression check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anthropic_provider_oauth_self_heal_still_works(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sanity: Anthropic's pre-existing OAuth-file refresh path still fires.

    Spec says don't regress the Anthropic provider — verify the OAuth
    file resolution + same-thread refresh wiring is intact by
    constructing the adapter against a tmp ``data_dir`` and ensuring
    it picks up a manually-written credential.
    """
    from corlinman_providers import AnthropicProvider
    from corlinman_providers._anthropic_oauth import save_anthropic_credential

    oauth_dir = tmp_path / ".oauth"
    oauth_dir.mkdir()

    # We can't easily exercise the refresh round-trip without a
    # real OAuth server. The non-regression we need is: the provider
    # uses the OAuth bundle when present and the env vars are unset.
    import time as _time

    from corlinman_providers._anthropic_oauth import AnthropicOAuthCredential

    cred = AnthropicOAuthCredential(
        provider="anthropic",
        access_token="oauth-token-1",
        refresh_token="refresh-1",
        expires_at_ms=10_000_000_000_000,  # far future
        scope=None,
        obtained_at_ms=int(_time.time() * 1000),
    )
    save_anthropic_credential(tmp_path, cred)

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)

    prov = AnthropicProvider(data_dir=tmp_path)
    token, style = prov._credential_resolution()
    assert token == "oauth-token-1"
    assert style == "bearer"


# ---------------------------------------------------------------------------
# Codex provider — _ensure_fresh race + reactive 401 lock
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_codex_ensure_fresh_single_flight() -> None:
    """Five concurrent ``_ensure_fresh()`` → exactly one OAuth POST.

    Without the lock, every concurrent caller that saw ``is_expired()
    == True`` would race to POST ``/oauth/token`` with the same
    refresh token; the auth server rotates refresh on each, only one
    winner persists, and the other four hold dead refresh tokens.
    """
    import time

    from corlinman_providers._codex_oauth import (
        CodexOAuthCredential,
    )
    from corlinman_providers.codex_provider import CodexProvider

    expired_ms = int(time.time() * 1000) - 1
    cred = CodexOAuthCredential(
        access_token="old-token",
        refresh_token="rt-shared",
        expires_at_ms=expired_ms,
    )
    prov = CodexProvider(credential=cred)

    refresh_calls = {"n": 0}
    refresh_started_event = asyncio.Event()
    can_complete = asyncio.Event()

    fresh_ms = int(time.time() * 1000) + 3_600_000

    async def _slow_refresh(*, refresh_token: str) -> CodexOAuthCredential:
        # Hold the first refresh open while the other four pile up at
        # the lock. Without single-flight, each of the five callers
        # would have started its own POST before any of them finished.
        refresh_calls["n"] += 1
        refresh_started_event.set()
        await can_complete.wait()
        return CodexOAuthCredential(
            access_token="new-token",
            refresh_token="rt-shared-rotated",
            expires_at_ms=fresh_ms,
        )

    # Avoid touching the real ~/.codex/auth.json from this test.
    async def _no_persist(*_args: Any, **_kw: Any) -> None:
        return None

    import corlinman_providers.codex_provider as cp_mod

    orig_refresh = cp_mod.refresh_codex_token
    orig_persist = cp_mod.persist_codex_credential
    cp_mod.refresh_codex_token = _slow_refresh  # type: ignore[assignment]
    cp_mod.persist_codex_credential = lambda *a, **k: True  # type: ignore[assignment]
    try:
        # Kick off five concurrent calls.
        tasks = [asyncio.create_task(prov._ensure_fresh()) for _ in range(5)]
        await refresh_started_event.wait()
        # Now release the first refresh; the rest, queued on the
        # lock, must re-check is_expired() and short-circuit.
        can_complete.set()
        await asyncio.gather(*tasks)
    finally:
        cp_mod.refresh_codex_token = orig_refresh
        cp_mod.persist_codex_credential = orig_persist

    assert refresh_calls["n"] == 1
    assert prov._credential.access_token == "new-token"


@pytest.mark.asyncio
async def test_codex_token_recovery_single_flight() -> None:
    """Two racing ``_attempt_token_recovery`` paths → one POST.

    Models the case where two concurrent chat_streams both hit
    ``token_invalidated`` on the same access token. Both call
    ``_attempt_token_recovery``; the lock ensures only one HTTP
    refresh actually fires, the second sees the rotated access token
    on entry and returns True without re-POSTing.
    """
    import time

    from corlinman_providers._codex_oauth import (
        CodexOAuthCredential,
    )
    from corlinman_providers.codex_provider import CodexProvider

    cred = CodexOAuthCredential(
        access_token="invalidated",
        refresh_token="rt-shared",
        expires_at_ms=int(time.time() * 1000) + 3_600_000,
    )
    prov = CodexProvider(credential=cred)

    refresh_calls = {"n": 0}
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_refresh(*, refresh_token: str) -> CodexOAuthCredential:
        refresh_calls["n"] += 1
        started.set()
        await release.wait()
        return CodexOAuthCredential(
            access_token="recovered",
            refresh_token="rt-shared-rotated",
            expires_at_ms=int(time.time() * 1000) + 3_600_000,
        )

    import corlinman_providers.codex_provider as cp_mod

    orig_refresh = cp_mod.refresh_codex_token
    orig_persist = cp_mod.persist_codex_credential
    cp_mod.refresh_codex_token = _slow_refresh  # type: ignore[assignment]
    cp_mod.persist_codex_credential = lambda *a, **k: True  # type: ignore[assignment]
    try:
        t1 = asyncio.create_task(prov._attempt_token_recovery())
        await started.wait()
        # Second caller arrives while the first is still holding the
        # lock + waiting on the upstream OAuth server.
        t2 = asyncio.create_task(prov._attempt_token_recovery())
        # Both should ultimately succeed.
        release.set()
        r1, r2 = await asyncio.gather(t1, t2)
    finally:
        cp_mod.refresh_codex_token = orig_refresh
        cp_mod.persist_codex_credential = orig_persist

    assert r1 is True
    assert r2 is True
    # Exactly one POST hit the OAuth endpoint despite two racing callers.
    assert refresh_calls["n"] == 1
    assert prov._credential.access_token == "recovered"


# ---------------------------------------------------------------------------
# persist_codex_credential — fcntl file lock
# ---------------------------------------------------------------------------


def test_persist_codex_credential_serialises_concurrent_writers(tmp_path) -> None:
    """Two threads writing the same auth.json end up with one valid JSON.

    Without the flock, the read-modify-write windows interleave and
    the file can be left in a state where one process's freshly-
    rotated ``refresh_token`` is paired with the other's
    ``access_token``. With the lock, the file always reflects exactly
    one of the two writes (last-writer-wins is fine — the property is
    that the JSON is internally consistent).
    """
    import threading

    from corlinman_providers._codex_oauth import (
        CodexOAuthCredential,
        persist_codex_credential,
    )

    target = tmp_path / "auth.json"
    target.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "initial",
                    "refresh_token": "rt-initial",
                },
                "OPENAI_API_KEY": None,
            }
        ),
        encoding="utf-8",
    )

    cred_a = CodexOAuthCredential(
        access_token="from-A",
        refresh_token="rt-from-A",
        expires_at_ms=None,
    )
    cred_b = CodexOAuthCredential(
        access_token="from-B",
        refresh_token="rt-from-B",
        expires_at_ms=None,
    )

    barrier = threading.Barrier(2)
    results: list[bool] = []
    results_lock = threading.Lock()

    def _worker(cred: CodexOAuthCredential) -> None:
        barrier.wait()
        ok = persist_codex_credential(cred, path=target)
        with results_lock:
            results.append(ok)

    threads = [
        threading.Thread(target=_worker, args=(cred_a,)),
        threading.Thread(target=_worker, args=(cred_b,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Both writers reported success.
    assert results == [True, True] or results == [True, True]
    # File on disk is valid JSON (never garbled).
    data = json.loads(target.read_text())
    tokens = data["tokens"]
    # The result is one of the two writes in full — never a mix.
    assert (tokens["access_token"], tokens["refresh_token"]) in (
        ("from-A", "rt-from-A"),
        ("from-B", "rt-from-B"),
    )


def test_persist_codex_credential_lock_file_perms(tmp_path) -> None:
    """Lock file (``auth.json.lock``) is created with mode 0o600."""
    import sys

    if sys.platform == "win32":
        pytest.skip("flock + mode bits are POSIX-only")

    from corlinman_providers._codex_oauth import (
        CodexOAuthCredential,
        persist_codex_credential,
    )

    target = tmp_path / "auth.json"
    cred = CodexOAuthCredential(
        access_token="t", refresh_token="r", expires_at_ms=None
    )
    assert persist_codex_credential(cred, path=target) is True

    lock_path = target.with_suffix(target.suffix + ".lock")
    assert lock_path.exists()
    mode = lock_path.stat().st_mode & 0o777
    # umask may reduce the mode, but we asked for 0o600 — accept any
    # mode that doesn't grant group/other access.
    assert mode & 0o077 == 0, f"lock file too permissive: {oct(mode)}"


# ---------------------------------------------------------------------------
# Re-export sanity
# ---------------------------------------------------------------------------


def test_with_401_recovery_is_reexported_from_package() -> None:
    """Plugins can ``from corlinman_providers import with_401_recovery``."""
    from corlinman_providers import with_401_recovery as imported
    from corlinman_providers._auth_refresh import (
        with_401_recovery as direct,
    )

    assert imported is direct
