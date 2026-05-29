"""Anthropic provider adapter.

Wraps :class:`anthropic.AsyncAnthropic` behind
:class:`corlinman_providers.base.CorlinmanProvider`, maps vendor errors to
the :mod:`corlinman_providers.failover` hierarchy, and streams deltas as
:class:`ProviderChunk` values.

Tool-call handling (plan §14 R5): we listen for Anthropic's
``content_block_start`` / ``content_block_delta`` / ``content_block_stop``
events. When the starting content block is a ``tool_use``, we emit
``tool_call_start`` / ``tool_call_delta`` / ``tool_call_end`` chunks
mirroring the OpenAI-standard ``tool_calls`` surface. Text blocks become
ordinary ``token`` chunks. OpenAI-compatible tool_use blocks only.

Tested against ``anthropic==0.96`` (the ``messages.stream()`` raw-event API
stabilised in the 0.40+ line; we use ``event.type`` string tags rather than
``isinstance`` so minor SDK bumps don't break the adapter).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any, ClassVar, Literal

import structlog

from corlinman_providers._anthropic_oauth import (
    AnthropicOAuthCredential,
    load_anthropic_credential,
    refresh_anthropic_token,
    save_anthropic_credential,
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


# Header style for the resolved credential. ``bearer`` is used for OAuth
# tokens (PKCE file or ``ANTHROPIC_TOKEN`` env); ``api_key`` is used for
# the legacy ``x-api-key`` header path.
CredentialStyle = Literal["bearer", "api_key"]


class AnthropicProvider:
    """Anthropic adapter.

    Instantiate with ``AnthropicProvider()`` (default) or
    ``AnthropicProvider(api_key="...")``. Calls lazily construct
    ``anthropic.AsyncAnthropic`` so import-time failures stay benign.

    Credential resolution at construction time follows this order
    (highest priority first):

    1. ``<data_dir>/.oauth/anthropic.json`` — a PKCE-issued OAuth bundle
       persisted by the gateway's OAuth router. When the access token is
       expired (or within 120 s of expiry) and a refresh token is
       present, we attempt a same-thread refresh via provider-local
       OAuth helpers.
    2. ``ANTHROPIC_TOKEN`` env var — manual OAuth override (matches the
       hermes-agent contract for users who exported a bearer token from
       another tool).
    3. ``spec.api_key`` — the existing TOML-config path.
    4. ``ANTHROPIC_API_KEY`` env var — legacy fallback.

    Sources (1)/(2) bind to the ``Authorization: Bearer <token>`` header
    (the Anthropic SDK accepts this via the ``auth_token`` kwarg).
    Sources (3)/(4) bind to the historic ``x-api-key`` header path (the
    SDK's ``api_key`` kwarg).

    The ``data_dir`` is optional — when absent (e.g. test environments
    without a gateway-bootstrapped path), the OAuth lookup is skipped
    and resolution proceeds at step (2).
    """

    name: ClassVar[str] = "anthropic"
    kind: ClassVar[ProviderKind] = ProviderKind.ANTHROPIC

    def __init__(
        self,
        api_key: str | None = None,
        *,
        data_dir: Path | None = None,
    ) -> None:
        self._spec_api_key = api_key
        self._data_dir = data_dir
        # mtime-keyed memo of the parsed OAuth credential (audit R4-D4 /
        # PERF-003). ``_resolve_oauth_token`` runs per request on the
        # async hot path; without this cache it did a blocking
        # ``path.read_text()`` + ``json.loads()`` every call. We instead
        # ``stat()`` the file (far cheaper) and only re-read + parse when
        # the file's identity changes. The cache key is
        # ``(st_mtime_ns, st_size)``; a token refresh rewrites the file
        # via ``os.replace`` (atomic, new inode/mtime) so the key changes
        # and the cache invalidates automatically. ``None`` marks "no key
        # computed yet" — distinct from the cached value which may itself
        # be ``None`` (a malformed/absent file that parsed to nothing).
        self._cred_cache_key: tuple[int, int] | None = None
        self._cred_cache_value: AnthropicOAuthCredential | None = None

    @classmethod
    def build(
        cls,
        spec: ProviderSpec,
        *,
        data_dir: Path | None = None,
    ) -> AnthropicProvider:
        # ``data_dir`` is forwarded by :class:`ProviderRegistry` so the
        # OAuth-file resolution path (source 1 in the docstring above)
        # actually fires in production. Adapters that don't take the
        # kwarg are called through the legacy 1-arg shim in
        # :meth:`ProviderRegistry._build_adapter`; either signature is
        # accepted forever.
        return cls(api_key=spec.api_key, data_dir=data_dir)

    def _credential_resolution(self) -> tuple[str | None, CredentialStyle]:
        """Resolve the active credential and the header style to use.

        Returns ``(token, style)``. ``token`` is ``None`` when no source
        yields one — the caller raises ``RuntimeError`` so the operator
        sees a clear "API key missing" message at the first stream
        attempt instead of a confusing 401 from Anthropic.

        This method is intentionally synchronous: the OAuth file is a
        small JSON read and the refresh path is rare (token TTL is 1h
        and refresh runs at <120s remaining). When a refresh is
        attempted we run it via ``asyncio.run`` only when no loop is
        active — when we're already inside one (the common case for a
        live request), we skip the refresh and return the still-valid
        access token, letting the caller hit Anthropic with a stale
        token at most once before the next pass refreshes. This avoids
        deadlocks from spawning a sync-driven loop inside an existing
        one.
        """
        # 1. OAuth file under data_dir/.oauth/anthropic.json
        oauth_token = self._resolve_oauth_token()
        if oauth_token:
            return oauth_token, "bearer"

        # 2. ANTHROPIC_TOKEN env (manual OAuth override)
        env_token = (os.environ.get("ANTHROPIC_TOKEN") or "").strip()
        if env_token:
            return env_token, "bearer"

        # 3. spec.api_key
        if self._spec_api_key:
            return self._spec_api_key, "api_key"

        # 4. ANTHROPIC_API_KEY env (legacy)
        env_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        if env_key:
            return env_key, "api_key"

        return None, "api_key"

    def _credential_file_path(self) -> Path:
        """Path of the OAuth credential file under ``data_dir``."""
        # Mirrors ``_anthropic_oauth._credential_path`` (kept local to
        # avoid coupling to that module's private symbol). The layout is
        # part of the stored contract and does not change.
        assert self._data_dir is not None  # guarded by the caller
        return Path(self._data_dir) / ".oauth" / "anthropic.json"

    def _load_credential_cached(self) -> AnthropicOAuthCredential | None:
        """Return the parsed OAuth credential, re-reading only on change.

        ``stat()`` is far cheaper than ``read_text()`` + ``json.loads()``;
        we key the memo on ``(st_mtime_ns, st_size)`` and only fall
        through to :func:`load_anthropic_credential` (the expensive
        read + parse) when that key changes. A missing file resets the
        cache and returns ``None``.

        Threading note: the memo is a pair of plain instance attributes
        updated without a lock. That's deliberate — the worst a
        concurrent async caller can observe is a stale-but-valid
        credential or a redundant re-read, both idempotent. Holding a
        lock across the blocking refresh would be worse than the rare
        duplicate stat/read it would prevent.
        """
        path = self._credential_file_path()
        try:
            st = path.stat()
        except OSError:
            # File absent or unreadable — drop any stale memo so a later
            # (re)appearance triggers a fresh read.
            self._cred_cache_key = None
            self._cred_cache_value = None
            return None

        key = (st.st_mtime_ns, st.st_size)
        if key == self._cred_cache_key:
            return self._cred_cache_value

        # File is new/changed since we last parsed it — read + parse once
        # and memo the result against the observed key.
        cred = load_anthropic_credential(self._data_dir)  # type: ignore[arg-type]
        self._cred_cache_key = key
        self._cred_cache_value = cred
        return cred

    def _resolve_oauth_token(self) -> str | None:
        """Read the OAuth file (if any) and refresh if near-expiry."""
        if self._data_dir is None:
            return None

        cred = self._load_credential_cached()
        if cred is None:
            return None

        # Refresh on-use when <120s remaining and a refresh token is
        # present. Skipped silently when we're already inside an event
        # loop — see method docstring for the rationale.
        if cred.is_expired(skew_seconds=120) and cred.refresh_token:
            try:
                refreshed = self._refresh_sync(cred.refresh_token)
            except Exception as exc:
                logger.warning("anthropic.oauth_refresh_failed", error=str(exc))
                refreshed = None
            if refreshed is not None:
                new_cred = cred.with_refreshed(
                    access_token=refreshed["access_token"],
                    refresh_token=refreshed.get("refresh_token"),
                    expires_at_ms=refreshed.get("expires_at_ms"),
                )
                try:
                    save_anthropic_credential(self._data_dir, new_cred)
                except OSError as exc:
                    logger.warning("anthropic.oauth_save_failed", error=str(exc))
                # Prime the memo with the freshly-written credential keyed
                # on the post-write stat, so the next request reuses it
                # without re-reading the file we just wrote. If the stat
                # fails for any reason, drop the key so the next call
                # re-reads from disk (correctness over the perf shortcut).
                try:
                    st = self._credential_file_path().stat()
                    self._cred_cache_key = (st.st_mtime_ns, st.st_size)
                    self._cred_cache_value = new_cred
                except OSError:
                    self._cred_cache_key = None
                    self._cred_cache_value = None
                cred = new_cred
        return cred.access_token

    @staticmethod
    def _refresh_sync(refresh_token: str) -> dict[str, Any] | None:
        """Refresh wrapper that bridges async refresh into sync caller.

        When called from inside a running event loop we return ``None``
        to avoid a nested-loop deadlock; the access token is returned
        unchanged and the next pass through ``_resolve_oauth_token`` (a
        few seconds later when the token finally expires) will refresh
        from outside the loop or, more commonly, from the
        ``/admin/oauth/anthropic/refresh`` endpoint the operator
        triggers manually.
        """
        import asyncio

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(refresh_anthropic_token(refresh_token=refresh_token))
        return None

    # Compatibility shim: existing tests/call-sites read ``_api_key`` to
    # gate the "is anything configured?" check. We compute it on the fly
    # from the resolution chain so the property reflects the live state.
    @property
    def _api_key(self) -> str | None:
        token, _style = self._credential_resolution()
        return token

    @classmethod
    def params_schema(cls) -> dict[str, Any]:
        """Per-request params accepted by the Anthropic messages API."""
        return _ANTHROPIC_PARAMS_SCHEMA

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
        """Stream a chat completion via ``anthropic.messages.stream``.

        Raises :class:`RuntimeError` when no API key is configured —
        surfacing config gaps early instead of silent failure.
        """
        token, style = self._credential_resolution()
        if not token:
            raise RuntimeError("API key missing: set ANTHROPIC_API_KEY")

        # Imported lazily so test environments without the SDK still import this
        # module (and so importing the module doesn't require network).
        from anthropic import AsyncAnthropic  # type: ignore[import-not-found]

        if style == "bearer":
            # OAuth path: the Anthropic SDK supports ``auth_token=`` which
            # sets ``Authorization: Bearer <token>`` and suppresses the
            # default ``x-api-key`` header.
            client = AsyncAnthropic(auth_token=token)
        else:
            client = AsyncAnthropic(api_key=token)
        system, anthropic_messages = _split_system(messages)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens if max_tokens else 1024,
        }
        if system:
            kwargs["system"] = system
        if temperature is not None:
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = list(tools)
        if extra:
            kwargs.update(extra)

        try:
            async with client.messages.stream(**kwargs) as stream:
                # Per-block state: which content blocks are tool_use vs text.
                open_tool_ids: dict[int, str] = {}
                async for event in stream:
                    etype = getattr(event, "type", None)
                    if etype == "content_block_start":
                        block = getattr(event, "content_block", None)
                        idx = getattr(event, "index", 0)
                        if getattr(block, "type", None) == "tool_use":
                            call_id = getattr(block, "id", "") or ""
                            name = getattr(block, "name", "") or ""
                            open_tool_ids[idx] = call_id
                            yield ProviderChunk(
                                kind="tool_call_start",
                                tool_call_id=call_id,
                                tool_name=name,
                            )
                    elif etype == "content_block_delta":
                        delta = getattr(event, "delta", None)
                        dtype = getattr(delta, "type", None)
                        idx = getattr(event, "index", 0)
                        if dtype == "text_delta":
                            text = getattr(delta, "text", "") or ""
                            if text:
                                yield ProviderChunk(kind="token", text=text)
                        elif dtype == "input_json_delta":
                            partial = getattr(delta, "partial_json", "") or ""
                            call_id = open_tool_ids.get(idx, "")
                            if call_id:
                                yield ProviderChunk(
                                    kind="tool_call_delta",
                                    tool_call_id=call_id,
                                    arguments_delta=partial,
                                )
                    elif etype == "content_block_stop":
                        idx = getattr(event, "index", 0)
                        call_id = open_tool_ids.pop(idx, None)
                        if call_id:
                            yield ProviderChunk(
                                kind="tool_call_end",
                                tool_call_id=call_id,
                            )
                    # Other event types (message_start, message_delta,
                    # message_stop) carry only accounting data we pick up via
                    # get_final_message below.
                final = await stream.get_final_message()
                finish = _map_stop_reason(getattr(final, "stop_reason", None))
                yield ProviderChunk(kind="done", finish_reason=finish)
        except CorlinmanError:
            raise
        except Exception as exc:
            raise _map_anthropic_error(exc, model=model) from exc
        finally:
            # Always release the underlying httpx pool. The SDK's
            # ``messages.stream`` context manager closes the stream but
            # the surrounding ``AsyncAnthropic`` client owns its own
            # connection pool that only releases on ``client.close()``.
            # Without this every chat call leaks a pool entry — see
            # audit R1-003.
            await _safe_close(client)

    async def embed(
        self,
        *,
        model: str,
        inputs: Sequence[str],
        extra: dict[str, Any] | None = None,
    ) -> list[list[float]]:
        raise NotImplementedError("Anthropic has no embedding API — route to OpenAI / local")

    @classmethod
    def supports(cls, model: str) -> bool:
        """Claim any model id starting with ``claude-``."""
        return model.startswith("claude-")


async def _safe_close(client: Any) -> None:
    """Best-effort ``await client.close()`` that never masks the real exception.

    Mirrors the OpenAI provider's helper. ``AsyncAnthropic.close()`` is
    async + idempotent; a close-time error stays in the log so the
    operator can investigate, but never bubbles up into the chat-stream
    flow (which would mask the original error). Test doubles without a
    ``close`` attribute no-op.
    """
    close = getattr(client, "close", None)
    if close is None:
        return
    try:
        result = close()
        if hasattr(result, "__await__"):
            await result
    except Exception as exc:  # pragma: no cover — defensive close-path guard
        logger.warning("anthropic.client_close_failed", error=str(exc))


def _split_system(messages: Sequence[Any]) -> tuple[str | None, list[dict[str, Any]]]:
    """Split out ``role="system"`` messages — Anthropic takes ``system`` as a
    top-level parameter rather than an entry in ``messages``.

    ``content`` may be either a string (text-only turn, pre-multimodal
    callers) or a list of OpenAI-shaped content parts (``{"type": "text",
    ...}`` / ``{"type": "image_url", ...}`` — see
    :func:`corlinman_agent.reasoning_loop._inject_attachments`). For
    multi-part content we translate to Anthropic's vendor blocks
    in-place: ``image_url`` → ``{"type": "image", "source": {...}}``.
    Non-text system messages carrying list content collapse into
    concatenated text (Anthropic's system parameter is a string).
    """
    system_parts: list[str] = []
    chat: list[dict[str, Any]] = []
    for m in messages:
        role = _get(m, "role")
        content = _get(m, "content")
        if role == "system":
            text = _content_to_text(content)
            if text:
                system_parts.append(text)
        else:
            # Anthropic requires role in {"user", "assistant"}; collapse "tool" for now.
            anth_role = "user" if role in ("user", "tool") else "assistant"
            if isinstance(content, list):
                blocks = _parts_to_anthropic_blocks(content)
                chat.append({"role": anth_role, "content": blocks})
            else:
                chat.append({"role": anth_role, "content": content or ""})
    system = "\n\n".join(system_parts) if system_parts else None
    return system, chat


def _content_to_text(content: Any) -> str:
    """Flatten content (str or list of parts) to a plain string."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text = part.get("text") or ""
                if text:
                    out.append(text)
        return "".join(out)
    return str(content)


def _parts_to_anthropic_blocks(parts: Sequence[Any]) -> list[dict[str, Any]]:
    """Translate OpenAI-shape content parts to Anthropic content blocks.

    Supported:
    * ``{"type": "text", "text": "..."}`` → ``{"type": "text", "text": "..."}``
    * ``{"type": "image_url", "image_url": {"url": "..."}}`` →
      ``{"type": "image", "source": {"type": "url", "url": "..."}}``
      or ``{"type": "image", "source": {"type": "base64", ...}}`` when
      the url is a ``data:`` URI.

    Unsupported (audio / generic file): logged at warn and dropped.
    Anthropic's current content-block vocabulary is text + image only
    (file API is beta and not wired here yet — TODO). A downstream
    ``TODO: multimodal file support`` covers the gap.
    """
    blocks: list[dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            text = part.get("text") or ""
            blocks.append({"type": "text", "text": text})
        elif ptype == "image_url":
            url = (part.get("image_url") or {}).get("url") or ""
            block = _image_block_from_url(url)
            if block is not None:
                blocks.append(block)
        elif ptype == "file":
            # Audio / video / generic files — not yet representable as
            # an Anthropic content block. Skip with a warn so the chat
            # proceeds with text only instead of failing the request.
            logger.warning(
                "anthropic.unsupported_attachment",
                kind=(part.get("file") or {}).get("kind"),
            )
        # Unknown part types quietly skipped — forward compat.
    if not blocks:
        # Anthropic rejects empty content arrays; fall back to an empty
        # text block so the turn is at least syntactically valid.
        blocks = [{"type": "text", "text": ""}]
    return blocks


def _image_block_from_url(url: str) -> dict[str, Any] | None:
    """Build an Anthropic ``image`` content block from a URL.

    Accepts both ``https://...`` (url source, Claude 4+) and
    ``data:<mime>;base64,...`` URIs (base64 source — works on earlier
    Claude versions too). Returns ``None`` for an empty / malformed url.
    """
    if not url:
        return None
    if url.startswith("data:") and ";base64," in url:
        header, b64 = url.split(",", 1)
        # header is "data:<mime>;base64"
        mime = header[5:].split(";", 1)[0] or "image/jpeg"
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": b64},
        }
    return {"type": "image", "source": {"type": "url", "url": url}}


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _map_stop_reason(reason: str | None) -> str:
    """Map Anthropic ``stop_reason`` to our normalised finish_reason set."""
    mapping = {
        "end_turn": "stop",
        "max_tokens": "length",
        "stop_sequence": "stop",
        "tool_use": "tool_calls",
    }
    return mapping.get(reason or "", "stop")


def _retry_after_ms_from_exc(exc: Exception) -> int | None:
    """Extract the ``Retry-After`` header off a vendor 429 as milliseconds.

    The Anthropic SDK preserves the upstream header on
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


def _map_anthropic_error(exc: Exception, *, model: str) -> CorlinmanError:
    """Coerce any vendor SDK exception into a :class:`CorlinmanError` subtype."""
    # Late import keeps module safe when anthropic isn't installed.
    try:
        from anthropic import (  # type: ignore[import-not-found]
            APIStatusError,
            APITimeoutError,
            AuthenticationError,
            BadRequestError,
            NotFoundError,
            PermissionDeniedError,
        )
        from anthropic import (
            RateLimitError as AnthRateLimit,
        )
    except Exception:  # pragma: no cover — import-time guard
        return CorlinmanError(str(exc), provider="anthropic", model=model)

    ctx: dict[str, Any] = {"provider": "anthropic", "model": model}
    if isinstance(exc, AnthRateLimit):
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
        if "credit" in msg or "billing" in msg or "quota" in msg:
            return BillingError(str(exc), status_code=402, **ctx)
        if "context" in msg or "too long" in msg or "tokens" in msg:
            return ContextOverflowError(str(exc), status_code=400, **ctx)
        return FormatError(str(exc), status_code=400, **ctx)
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", 0) or 0
        if status == 503 or status == 529:
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


# Hand-authored JSON Schema (draft 2020-12). Anthropic accepts ``top_p`` via
# ``extra`` — the SDK forwards unknown-to-us kwargs to the HTTP body, so we
# declare it here and the adapter threads it through.
_ANTHROPIC_PARAMS_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "temperature": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
            "description": "Sampling temperature (Anthropic caps at 1.0).",
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
            "description": "Maximum tokens in the completion (required by Anthropic).",
        },
        "system_prompt": {
            "type": "string",
            "maxLength": 16000,
            "description": "Top-level Anthropic system parameter.",
        },
        "timeout_ms": {
            "type": "integer",
            "minimum": 100,
            "description": "Client-side request timeout in milliseconds.",
        },
    },
}
