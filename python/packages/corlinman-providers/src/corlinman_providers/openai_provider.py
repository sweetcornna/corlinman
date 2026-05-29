"""OpenAI provider adapter.

Wraps :class:`openai.AsyncOpenAI` behind
:class:`corlinman_providers.base.CorlinmanProvider`; also used as the base
implementation for OpenAI-compatible endpoints (DeepSeek, Qwen DashScope,
GLM) which just vary ``base_url`` and auth.

Tool-call handling (plan §14 R5): the OpenAI chat-completion stream emits
``choices[0].delta.tool_calls[]`` with one entry per new or in-progress
tool call. Each entry carries an ``index``; successive deltas for the same
index append to the same call's ``function.arguments`` buffer. We track
whether we've seen a call's ``id`` yet — the **first** chunk for a given
index carries the ``id`` + ``function.name``, and we emit
``tool_call_start`` the first time we see it. Argument fragments flow
through as ``tool_call_delta``. When the terminal chunk's
``finish_reason == "tool_calls"`` arrives, we emit ``tool_call_end`` for
every open call before the final ``done`` chunk.

Tested against ``openai==2.32``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar

import structlog

from corlinman_providers._auth_refresh import (
    refresh_env_key_if_rotated,
    with_401_recovery,
)
from corlinman_providers.base import ProviderChunk
from corlinman_providers.failover import (
    AuthError,
    AuthPermanentError,
    BillingError,
    ContextOverflowError,
    CorlinmanError,
    FormatError,
    ModelNotFoundError,
    OverloadedError,
    RateLimitError,
    TimeoutError,  # noqa: A004 — intentional shadowing; see failover.TimeoutError
)
from corlinman_providers.specs import ProviderKind, ProviderSpec

logger = structlog.get_logger(__name__)

# Placeholder ``api_key`` used only to satisfy the openai SDK constructor when
# the real credential travels in a custom auth header (``auth_kind="header"``).
# It is deliberately NOT a real secret so the SDK's mandatory bearer never
# leaks the credential — see :meth:`OpenAIProvider._make_client`.
_HEADER_AUTH_SENTINEL = "header-auth-no-bearer"


@dataclass(slots=True)
class _ToolCallState:
    """Per-index streaming state for one in-progress tool call.

    ``started`` flips ``True`` the moment we emit ``tool_call_start`` — which
    only happens once we hold a real ``id`` (or, as a last resort, at finish
    under the synthetic id). While ``started`` is ``False`` we buffer argument
    fragments in ``pending_args`` and the name in ``name`` so a late ``id`` can
    be promoted without splitting the call or losing its args (BUG-006).
    """

    call_id: str
    started: bool
    name: str = ""
    pending_args: list[str] = field(default_factory=list)


class OpenAIProvider:
    """OpenAI adapter (and base for OpenAI-compatible endpoints)."""

    name: ClassVar[str] = "openai"
    kind: ClassVar[ProviderKind] = ProviderKind.OPENAI

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        env_key: str = "OPENAI_API_KEY",
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self._api_key = api_key or os.environ.get(env_key) or None
        self._base_url = base_url
        # Static headers forwarded on every request. Used by declarative
        # providers whose ``auth_kind == "header"`` carry their credential in
        # a custom header (e.g. ``X-API-Key``) instead of ``Authorization:
        # Bearer`` — see :func:`declarative._build_inner`. ``None`` keeps the
        # historic bearer-only behaviour.
        self._default_headers = default_headers
        # Remember the env-var name so the reactive 401 path can re-read
        # the (possibly rotated) secret without bounding adapter
        # instances to a specific env name. ``Azure`` subclass overrides
        # to ``AZURE_OPENAI_API_KEY``; ``openai_compatible`` keeps the
        # default ``OPENAI_API_KEY`` (its operator typically scopes
        # bring-your-own keys through the same env).
        self._env_key = env_key

    async def _refresh_credential(self) -> bool:
        """Reactive 401 path: re-read the env var and update ``_api_key``.

        Returns ``True`` when the env var carries a non-empty value
        that differs from the one currently held in-process —
        signalling :func:`with_401_recovery` to retry the open phase
        with the new key. Returns ``False`` when the env var is empty
        or unchanged; retrying with the same key would just hit the
        same 401, so the original :class:`AuthError` propagates and
        the failover layer can pick the next adapter.
        """
        def _set(new_value: str) -> None:
            self._api_key = new_value

        return await refresh_env_key_if_rotated(
            env_name=self._env_key,
            current=self._api_key,
            on_update=_set,
        )

    @classmethod
    def build(cls, spec: ProviderSpec) -> OpenAIProvider:
        """Construct from a :class:`ProviderSpec`.

        Falls back to the ``OPENAI_API_KEY`` env var when the spec omits one
        — matches the historic constructor behaviour so existing envs keep
        working even when the new config path is active.
        """
        return cls(
            api_key=spec.api_key,
            base_url=spec.base_url,
        )

    @classmethod
    def params_schema(cls) -> dict[str, Any]:
        """JSON Schema (draft 2020-12) for per-request params.

        Covers the portable chat-completion knobs plus the ``reasoning_effort``
        escape hatch for the ``o1``/``o3`` reasoning family (forwarded via
        ``extra``; ignored by models that don't accept it).
        """
        return _OPENAI_PARAMS_SCHEMA

    def _make_client(self) -> Any:
        """Construct the async OpenAI-wire client used by :meth:`chat_stream`.

        Factored into a hook so wire-compatible siblings (Azure OpenAI —
        see :class:`corlinman_providers.market_providers.AzureProvider`)
        can swap in a differently-shaped client (``AsyncAzureOpenAI`` with
        deployment-id routing and ``api-key`` auth) while reusing the
        stream-parsing + tool-call-aggregation logic verbatim.
        """
        from openai import AsyncOpenAI  # type: ignore[import-not-found]

        client_kwargs: dict[str, Any] = {"api_key": self._api_key}
        if self._base_url:
            client_kwargs["base_url"] = self._base_url
        if self._default_headers:
            # Custom-header auth: the real credential rides in the declared
            # header (already baked into ``_default_headers``). The openai SDK
            # *requires* a truthy ``api_key`` to construct, so feed it a
            # non-credential sentinel rather than the real key — the resulting
            # ``Authorization: Bearer`` then carries the sentinel, never the
            # secret. Gateways keyed on the custom header ignore it.
            client_kwargs["default_headers"] = dict(self._default_headers)
            if not self._api_key:
                client_kwargs["api_key"] = _HEADER_AUTH_SENTINEL
        return AsyncOpenAI(**client_kwargs)

    async def chat_stream(
        self,
        *,
        model: str,
        messages: Sequence[Any],
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[ProviderChunk]:
        # Custom-header auth carries the credential in ``_default_headers``
        # rather than ``_api_key``, so a missing ``_api_key`` is only an error
        # when no header credential is configured either.
        if not self._api_key and not self._default_headers:
            raise RuntimeError(f"API key missing for provider {self.name}")

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [_normalise_message(m) for m in messages],
            "stream": True,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = list(tools)
        if extra:
            kwargs.update(extra)

        # index → per-call streaming state. We emit `tool_call_start` at most
        # once per index (with the *real* id) and always close with
        # `tool_call_end`. When the first delta for an index arrives without
        # an `id` (some OpenAI-compatible servers send it only in a later
        # chunk), we hold the `start` back — buffering name + args — until the
        # real id arrives, then emit `start` once and flush the buffer. This
        # avoids splitting a single call across a synthetic + real id (see
        # BUG-006); the downstream reasoning loop keys all state by
        # `tool_call_id`, so a late promotion via a second `start` would
        # orphan the args accumulated under the synthetic id.
        open_calls: dict[int, _ToolCallState] = {}
        finish_reason = "stop"

        async def _open() -> tuple[Any, Any]:
            """Build the client + open the stream, mapping any vendor SDK
            exception to a :class:`CorlinmanError`.

            Factored so :func:`with_401_recovery` can drive a single
            reactive retry around the open phase only — once the stream
            has yielded its first chunk, mid-stream failures still
            propagate verbatim (a partial-stream retry would duplicate
            tokens). The client is constructed inside the closure so the
            second attempt picks up the refreshed ``self._api_key``.

            Lifecycle: if ``create()`` raises we close the client here
            before re-raising — otherwise a 401-then-retry path would
            leak the abandoned first client's httpx pool (the retry
            inside :func:`with_401_recovery` builds a fresh client for
            the second attempt). On success the caller in ``chat_stream``
            owns the close via a ``try/finally`` around the iteration.
            """
            client_ = self._make_client()
            try:
                stream_ = await client_.chat.completions.create(**kwargs)
            except CorlinmanError:
                await _safe_close(client_)
                raise
            except Exception as exc:
                await _safe_close(client_)
                raise _map_openai_error(exc, model=model, provider=self.name) from exc
            return client_, stream_

        client: Any = None
        try:
            client, stream = await with_401_recovery(
                _open, refresh=self._refresh_credential, provider=self.name
            )
            async for chunk in stream:
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]
                delta = getattr(choice, "delta", None)
                finish = getattr(choice, "finish_reason", None)

                if delta is not None:
                    text = getattr(delta, "content", None)
                    if text:
                        yield ProviderChunk(kind="token", text=text)

                    tool_deltas = getattr(delta, "tool_calls", None) or []
                    for td in tool_deltas:
                        idx = getattr(td, "index", 0) or 0
                        tc_id = getattr(td, "id", None)
                        fn = getattr(td, "function", None)
                        fn_name = getattr(fn, "name", None) if fn else None
                        fn_args = getattr(fn, "arguments", None) if fn else None

                        # First sighting of this index → open the call. If the
                        # real id is already present we can emit `start` now;
                        # otherwise hold it back (started=False) under a
                        # synthetic id and wait for the late id to arrive.
                        state = open_calls.get(idx)
                        if state is None:
                            state = _ToolCallState(
                                call_id=tc_id or f"call_{idx}",
                                started=tc_id is not None,
                            )
                            if fn_name:
                                state.name = fn_name
                            open_calls[idx] = state
                            if state.started:
                                yield ProviderChunk(
                                    kind="tool_call_start",
                                    tool_call_id=state.call_id,
                                    tool_name=state.name,
                                )
                        elif tc_id and not state.started:
                            # Late id arrived → promote the synthetic id to the
                            # real one, emit the deferred `start`, then flush
                            # any args buffered before the id appeared.
                            state.call_id = tc_id
                            if fn_name and not state.name:
                                state.name = fn_name
                            state.started = True
                            yield ProviderChunk(
                                kind="tool_call_start",
                                tool_call_id=state.call_id,
                                tool_name=state.name,
                            )
                            for buffered in state.pending_args:
                                yield ProviderChunk(
                                    kind="tool_call_delta",
                                    tool_call_id=state.call_id,
                                    arguments_delta=buffered,
                                )
                            state.pending_args.clear()
                        elif fn_name and not state.name:
                            # Name can also dribble in across deltas.
                            state.name = fn_name

                        if fn_args:
                            if state.started:
                                yield ProviderChunk(
                                    kind="tool_call_delta",
                                    tool_call_id=state.call_id,
                                    arguments_delta=fn_args,
                                )
                            else:
                                # No real id yet — buffer until promotion so we
                                # never emit a delta under the synthetic id.
                                state.pending_args.append(fn_args)

                if finish is not None:
                    # Close any still-open tool calls before the terminal done.
                    for state in open_calls.values():
                        if not state.started:
                            # The real id never arrived — emit the deferred
                            # `start` (under the synthetic id) + flush the
                            # buffer rather than silently dropping the call.
                            yield ProviderChunk(
                                kind="tool_call_start",
                                tool_call_id=state.call_id,
                                tool_name=state.name,
                            )
                            for buffered in state.pending_args:
                                yield ProviderChunk(
                                    kind="tool_call_delta",
                                    tool_call_id=state.call_id,
                                    arguments_delta=buffered,
                                )
                            state.pending_args.clear()
                            state.started = True
                        yield ProviderChunk(
                            kind="tool_call_end",
                            tool_call_id=state.call_id,
                        )
                    open_calls.clear()
                    finish_reason = _map_finish_reason(finish)
                    break
        except CorlinmanError:
            raise
        except Exception as exc:
            raise _map_openai_error(exc, model=model, provider=self.name) from exc
        finally:
            # Always release the httpx pool, regardless of which exit
            # path (success, mapped CorlinmanError, mid-stream raw exc,
            # or generator ``aclose()`` from a cancelled caller) we took.
            # Without this every chat call leaks a pool entry — see
            # audit R1-003.
            if client is not None:
                await _safe_close(client)

        yield ProviderChunk(kind="done", finish_reason=finish_reason)

    async def embed(
        self,
        *,
        model: str,
        inputs: Sequence[str],
        extra: dict[str, Any] | None = None,
    ) -> list[list[float]]:
        # TODO(M3): implement via client.embeddings.create.
        raise NotImplementedError("OpenAIProvider.embed lands in M3")

    @classmethod
    def supports(cls, model: str) -> bool:
        """Claim ``gpt-*`` / ``o1-*`` / ``o3-*`` model ids."""
        return (
            model.startswith("gpt-")
            or model.startswith("o1-")
            or model.startswith("o3-")
            or model == "gpt-3.5-turbo"
        )


async def _safe_close(client: Any) -> None:
    """Best-effort ``await client.close()`` that never masks the real exception.

    The vendor SDK exposes ``AsyncOpenAI.close()`` (an async, idempotent
    call that drains the underlying ``httpx.AsyncClient``). We never let
    a close-time error bubble up — if the network is already broken,
    the close attempt may itself fail and we'd rather surface the
    original chat-stream error than a confusing close-failure trace.
    Test doubles that omit ``close`` cause a quiet no-op.
    """
    close = getattr(client, "close", None)
    if close is None:
        return
    try:
        result = close()
        if hasattr(result, "__await__"):
            await result
    except Exception as exc:  # pragma: no cover — defensive close-path guard
        logger.warning("openai.client_close_failed", error=str(exc))


def _normalise_message(m: Any) -> dict[str, Any]:
    """Accept both dicts and objects with ``role``/``content`` attributes."""
    if isinstance(m, dict):
        return m
    out: dict[str, Any] = {
        "role": getattr(m, "role", "user"),
        "content": getattr(m, "content", "") or "",
    }
    name = getattr(m, "name", None)
    if name:
        out["name"] = name
    tool_call_id = getattr(m, "tool_call_id", None)
    if tool_call_id:
        out["tool_call_id"] = tool_call_id
    return out


def _map_finish_reason(reason: str | None) -> str:
    """Normalise OpenAI ``finish_reason`` values.

    OpenAI already emits ``stop`` / ``length`` / ``tool_calls`` verbatim; we
    keep the same surface. ``content_filter`` and ``function_call`` (legacy)
    collapse to ``stop`` so the downstream reasoning loop has a stable set.
    """
    if reason in ("stop", "length", "tool_calls"):
        return reason
    return "stop"


def _retry_after_ms_from_exc(exc: Exception) -> int | None:
    """Extract the ``Retry-After`` header off a vendor 429 as milliseconds.

    The OpenAI SDK preserves the upstream header on
    ``exc.response.headers`` (key ``retry-after``). Per RFC 9110 the value
    is either delta-seconds (an integer) or an HTTP-date. We honour the
    delta-seconds form (converted to ms); the HTTP-date form is rare in
    practice and parsing it would require a clock-skew tolerance we don't
    want to design here, so it is ignored gracefully (returns ``None``,
    letting the failover layer fall back to its default backoff schedule).
    Mirrors ``_retry._retry_after_or_backoff`` but returns ms for
    :attr:`failover.RateLimitError.retry_after_ms`.
    """
    response = getattr(exc, "response", None)
    headers: Any = getattr(response, "headers", None) if response is not None else None
    if headers is None:
        # The SDK sometimes attaches headers directly on the exception.
        headers = getattr(exc, "headers", None)
    if headers is None or not hasattr(headers, "get"):
        return None

    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
    except Exception:  # defensive: tolerate odd header mappings
        return None
    if raw is None:
        return None

    try:
        seconds = float(str(raw).strip())
    except (TypeError, ValueError):
        # HTTP-date form (or anything unparseable) — ignore gracefully.
        return None
    if seconds < 0:
        return None
    return int(seconds * 1000)


def _map_openai_error(exc: Exception, *, model: str, provider: str) -> CorlinmanError:
    """Coerce any OpenAI SDK exception into a :class:`CorlinmanError` subtype."""
    try:
        from openai import (  # type: ignore[import-not-found]
            APIStatusError,
            APITimeoutError,
            AuthenticationError,
            BadRequestError,
            NotFoundError,
            PermissionDeniedError,
        )
        from openai import (
            RateLimitError as OaRateLimit,
        )
    except Exception:  # pragma: no cover
        return CorlinmanError(str(exc), provider=provider, model=model)

    ctx: dict[str, Any] = {"provider": provider, "model": model}
    if isinstance(exc, OaRateLimit):
        return RateLimitError(
            str(exc),
            status_code=429,
            retry_after_ms=_retry_after_ms_from_exc(exc),
            **ctx,
        )
    if isinstance(exc, APITimeoutError):
        return TimeoutError(str(exc), **ctx)
    if isinstance(exc, AuthenticationError):
        return AuthError(str(exc), status_code=401, **ctx)
    if isinstance(exc, PermissionDeniedError):
        return AuthPermanentError(str(exc), status_code=403, **ctx)
    if isinstance(exc, NotFoundError):
        return ModelNotFoundError(str(exc), status_code=404, **ctx)
    if isinstance(exc, BadRequestError):
        msg = str(exc).lower()
        if "quota" in msg or "billing" in msg or "credit" in msg:
            return BillingError(str(exc), status_code=402, **ctx)
        if "context" in msg or "too long" in msg or "maximum context" in msg:
            return ContextOverflowError(str(exc), status_code=400, **ctx)
        return FormatError(str(exc), status_code=400, **ctx)
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", 0) or 0
        if status in (503, 529):
            return OverloadedError(str(exc), status_code=status, **ctx)
        if status == 429:
            return RateLimitError(
                str(exc),
                status_code=status,
                retry_after_ms=_retry_after_ms_from_exc(exc),
                **ctx,
            )
        if status in (401, 403):
            return AuthError(str(exc), status_code=status, **ctx)
        if status == 404:
            return ModelNotFoundError(str(exc), status_code=status, **ctx)
        return CorlinmanError(str(exc), status_code=status, **ctx)
    return CorlinmanError(str(exc), **ctx)


# Hand-authored JSON Schema (draft 2020-12). Kept tight per the contract:
# common knobs as a slider-friendly ``number`` with bounds, plus the one
# OpenAI-family-specific extra (``reasoning_effort``).
_OPENAI_PARAMS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "temperature": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 2.0,
            "description": "Sampling temperature. 0 = deterministic.",
        },
        "top_p": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Nucleus sampling probability mass.",
        },
        "max_tokens": {
            "type": "integer",
            "minimum": 1,
            "description": "Maximum tokens in the completion.",
        },
        "system_prompt": {
            "type": "string",
            "maxLength": 16000,
            "description": "System message prepended to the conversation.",
        },
        "timeout_ms": {
            "type": "integer",
            "minimum": 100,
            "description": "Client-side request timeout in milliseconds.",
        },
        "reasoning_effort": {
            "type": "string",
            "enum": ["minimal", "low", "medium", "high"],
            "description": "o1/o3-family reasoning effort hint.",
        },
    },
}
