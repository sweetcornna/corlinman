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
import re
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

# OpenAI reasoning-model family (o1 / o3 / o4 / gpt-5). These models reject
# the classic sampling knobs: ``max_tokens`` must be sent as
# ``max_completion_tokens`` and ``temperature`` must be omitted entirely
# (only the default of 1 is accepted — sending any value 400s). Matched by
# ``str.startswith`` so dated/sized variants (``o3-mini``, ``o4-mini``,
# ``gpt-5-turbo``) are covered. Standard models are untouched.
_REASONING_MODEL_PREFIXES: tuple[str, ...] = ("o1", "o3", "o4", "gpt-5")

# Sampling knobs the o1/o3/o4/gpt-5 reasoning family rejects with a 400.
# The positional ``temperature`` argument is already dropped in
# :meth:`OpenAIProvider.chat_stream`, but alias/provider params merged via
# ``extra`` can carry any of these too — they are stripped from the merged
# ``extra`` for reasoning models (logged at debug) so an alias tuned for a
# standard model doesn't 400 when pointed at a reasoning one.
# ``temperature`` is included so an ``extra``-borne copy can't reintroduce
# what the positional-arg path already drops; ``top_logprobs`` rides along
# with ``logprobs`` (the API rejects both on reasoning models).
_REASONING_UNSUPPORTED_PARAMS: tuple[str, ...] = (
    "temperature",
    "top_p",
    "presence_penalty",
    "frequency_penalty",
    "logprobs",
    "top_logprobs",
    "logit_bias",
)

# Vendors whose chat APIs enforce strict user/assistant alternation and
# reject two consecutive same-role messages (DeepSeek, Qwen / QwQ via
# DashScope, GLM). For these we merge consecutive same-role ``user`` /
# ``assistant`` messages pre-flight instead of letting the vendor 400 —
# degrading gracefully beats erroring. See :func:`_merge_consecutive_roles`.
_STRICT_ALTERNATION_MODEL_PREFIXES: tuple[str, ...] = ("deepseek", "qwen", "qwq", "glm")


def _is_reasoning_model(model: str) -> bool:
    """Return whether ``model`` belongs to the o1/o3/o4/gpt-5 reasoning family."""
    return model.startswith(_REASONING_MODEL_PREFIXES)


def _requires_strict_alternation(model: str) -> bool:
    """Return whether ``model``'s vendor enforces strict role alternation."""
    return model.startswith(_STRICT_ALTERNATION_MODEL_PREFIXES)


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
        image_model: str | None = None,
        image_capable: bool = False,
    ) -> None:
        self._api_key = api_key or os.environ.get(env_key) or None
        self._base_url = base_url
        # Image-generation knobs persisted on the ``[providers.<name>]``
        # block (``image_model`` / ``image_capable``). The agent image
        # dispatcher reads ``image_model`` off the *built adapter* via
        # ``getattr(provider, "image_model", None)`` (see
        # ``corlinman_agent.image.generate._resolve_runtime_config``), so the
        # persisted knob is only honoured if we stamp it onto the instance
        # here — ``ProviderSpec`` carries it but the adapter must surface it.
        # Public attribute names (no underscore) match the spec field names
        # the dispatcher's ``getattr`` expects.
        self.image_model = image_model
        self.image_capable = image_capable
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
            image_model=spec.image_model,
            image_capable=spec.image_capable,
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
        # when no header credential is configured either. Raised as an
        # :class:`AuthError` (not a bare RuntimeError) so the failover layer
        # classifies it like any other auth failure, and the message names
        # the env var this adapter actually reads — for vendor wrappers
        # (Mistral / Groq / Moonshot / …) that's their vendor key, never
        # ``OPENAI_API_KEY``.
        if not self._api_key and not self._default_headers:
            raise AuthError(
                f"API key missing for provider {self.name}: set {self._env_key}",
                provider=self.name,
                model=model,
            )

        normalised = [_normalise_message(m) for m in messages]
        if _requires_strict_alternation(model):
            normalised = _merge_consecutive_roles(normalised)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": normalised,
            "stream": True,
        }
        # Reasoning models (o1/o3/o4/gpt-5) accept only the default
        # temperature and spell the completion budget
        # ``max_completion_tokens`` — see _REASONING_MODEL_PREFIXES.
        reasoning_model = _is_reasoning_model(model)
        if temperature is not None and not reasoning_model:
            kwargs["temperature"] = temperature
        if max_tokens:
            kwargs["max_completion_tokens" if reasoning_model else "max_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = list(tools)
        if extra:
            extra_params = dict(extra)
            if reasoning_model:
                # Alias/provider params merged via ``extra`` may carry the
                # classic sampling knobs; reasoning models 400 on every one
                # of them, so strip the whole family — not just temperature.
                dropped = [k for k in _REASONING_UNSUPPORTED_PARAMS if k in extra_params]
                for key in dropped:
                    extra_params.pop(key)
                if dropped:
                    logger.debug(
                        "openai.reasoning_params_dropped",
                        model=model,
                        params=dropped,
                    )
            kwargs.update(extra_params)

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
                    # Reasoning deltas (DeepSeek-R1, Qwen QwQ, and many
                    # OpenAI-compatible gateways) arrive on the non-standard
                    # ``delta.reasoning_content`` field, interleaved before
                    # the answer's ``delta.content``. Surface them as token
                    # chunks flagged ``is_reasoning=True`` so the reasoning
                    # loop renders a separate block and never replays them.
                    reasoning_text = getattr(delta, "reasoning_content", None)
                    if reasoning_text:
                        yield ProviderChunk(
                            kind="token", text=reasoning_text, is_reasoning=True
                        )
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
        """Claim ``gpt-*`` / ``o1-*`` / ``o3-*`` / ``o4-*`` model ids."""
        return (
            model.startswith("gpt-")
            or model.startswith("o1-")
            or model.startswith("o3-")
            or model.startswith("o4-")
            or model == "gpt-3.5-turbo"
        )

    def supports_tools(self, model: str) -> bool:
        """Whether ``model`` accepts OpenAI ``tools`` schemas — default yes.

        The OpenAI first-party catalogue is tool-capable across the board;
        subclasses fronting bring-your-own gateways
        (:class:`~corlinman_providers.openai_compatible.OpenAICompatibleProvider`)
        override this to honour an operator-declared ``tools = false``.
        """
        return True


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
    """Accept both dicts and objects with ``role``/``content`` attributes.

    CRITICAL replay rule: any ``reasoning_content`` carried on a dict
    message (a prior assistant turn captured from a reasoning stream) is
    stripped before the message goes back on the wire — DeepSeek-R1
    rejects requests that echo reasoning back with a 400, and no
    OpenAI-compatible vendor accepts it as an input field.
    """
    if isinstance(m, dict):
        if "reasoning_content" in m:
            return {k: v for k, v in m.items() if k != "reasoning_content"}
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


def _merge_consecutive_roles(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge consecutive same-role ``user`` / ``assistant`` messages.

    Strict-alternation vendors (see _STRICT_ALTERNATION_MODEL_PREFIXES)
    400 on two consecutive messages with the same role; joining their
    contents with a blank line degrades gracefully instead. Scope is
    deliberately narrow:

    * ``system`` messages are exempt — they are alternation-legal and
      merging them would reorder prompt-assembly semantics;
    * ``tool`` messages are exempt — consecutive tool results are legal
      (one per ``tool_call_id``) and merging would corrupt the call
      protocol;
    * assistant messages carrying ``tool_calls`` are exempt — their
      content is structurally bound to the calls.

    Pure: returns a new list; merged entries are fresh dicts, the
    caller's messages are never mutated.
    """
    merged: list[dict[str, Any]] = []
    for msg in messages:
        role = msg.get("role")
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and role in ("user", "assistant")
            and prev.get("role") == role
            and not prev.get("tool_calls")
            and not msg.get("tool_calls")
            and isinstance(prev.get("content"), str)
            and isinstance(msg.get("content"), str)
        ):
            joined = "\n\n".join(
                part for part in (prev["content"], msg["content"]) if part
            )
            combined = dict(prev)
            combined["content"] = joined
            merged[-1] = combined
            continue
        merged.append(msg)
    return merged


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


# OpenAI phrases a context overflow as e.g. ``This model's maximum context
# length is 128000 tokens. However, your messages resulted in 130000 tokens``
# or the ``A + B > C`` arithmetic form some compatible servers emit. We pull a
# best-effort ``(input_tokens, limit)`` so the reasoning loop can shrink-retry.
_OPENAI_CONTEXT_LIMIT_RE = re.compile(
    r"maximum context length is\s+(\d[\d,]*)\s+tokens", re.IGNORECASE
)
_OPENAI_CONTEXT_RESULTED_RE = re.compile(
    r"resulted in\s+(\d[\d,]*)\s+tokens", re.IGNORECASE
)
_OPENAI_CONTEXT_TRIPLE_RE = re.compile(
    r"(\d[\d,]*)\s*\+\s*(\d[\d,]*)\s*>\s*(\d[\d,]*)"
)


def _parse_openai_context_overflow(message: str) -> tuple[int | None, int | None, int | None]:
    """Best-effort ``(input_tokens, max_tokens, limit)`` from an overflow body.

    Returns a triple with ``None`` for any field that couldn't be parsed.
    Two body shapes are recognised: the OpenAI prose form (``maximum context
    length is C ... resulted in A tokens``) and the ``A + B > C`` arithmetic
    form some OpenAI-compatible servers emit.
    """
    text = message or ""

    def _to_int(s: str | None) -> int | None:
        if s is None:
            return None
        try:
            return int(s.replace(",", ""))
        except (TypeError, ValueError):
            return None

    triple = _OPENAI_CONTEXT_TRIPLE_RE.search(text)
    if triple is not None:
        return _to_int(triple.group(1)), _to_int(triple.group(2)), _to_int(triple.group(3))
    limit_m = _OPENAI_CONTEXT_LIMIT_RE.search(text)
    used_m = _OPENAI_CONTEXT_RESULTED_RE.search(text)
    limit = _to_int(limit_m.group(1)) if limit_m else None
    used = _to_int(used_m.group(1)) if used_m else None
    return used, None, limit


def _build_openai_context_overflow_error(
    exc: Exception, *, ctx: dict[str, Any]
) -> ContextOverflowError:
    """Build a :class:`ContextOverflowError` carrying the parsed numeric limit.

    Attaches ``input_tokens`` / ``max_tokens`` / ``limit`` onto the instance
    (when parseable) so the reasoning loop can compute an available-context
    budget and shrink-retry. ``failover.ContextOverflowError`` does not declare
    these as constructor kwargs (see wire_contract); readers use
    ``getattr(err, "limit", None)`` etc.
    """
    err = ContextOverflowError(str(exc), status_code=400, **ctx)
    input_tokens, max_tokens, limit = _parse_openai_context_overflow(str(exc))
    if input_tokens is not None:
        err.input_tokens = input_tokens  # type: ignore[attr-defined]
    if max_tokens is not None:
        err.max_tokens = max_tokens  # type: ignore[attr-defined]
    if limit is not None:
        err.limit = limit  # type: ignore[attr-defined]
    return err


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
            return _build_openai_context_overflow_error(exc, ctx=ctx)
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
