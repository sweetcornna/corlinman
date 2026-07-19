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

import base64
import binascii
import json
import os
import re
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
from corlinman_providers.reasoning_tiers import clamp_reasoning_tier
from corlinman_providers.specs import ProviderKind, ProviderSpec

logger = structlog.get_logger(__name__)


# Header style for the resolved credential. ``bearer`` is used for OAuth
# tokens (PKCE file or ``ANTHROPIC_TOKEN`` env); ``api_key`` is used for
# the legacy ``x-api-key`` header path.
CredentialStyle = Literal["bearer", "api_key"]

# Placeholder ``api_key`` used only to satisfy the Anthropic SDK's
# authentication-method check when the real credential travels in a custom
# auth header (declarative ``auth_kind="header"``). It is deliberately NOT a
# real secret: when the custom header is NOT the default ``x-api-key``, the
# SDK still emits its default ``x-api-key`` header — carrying this sentinel,
# never the secret — and gateways keyed on the custom header ignore it. When
# the custom header IS ``x-api-key`` the supplied ``default_headers`` value
# overrides the sentinel, so the real key rides ``x-api-key`` as intended.
# Mirrors :data:`openai_provider._HEADER_AUTH_SENTINEL`.
_HEADER_AUTH_SENTINEL = "header-auth-no-bearer"


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
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self._spec_api_key = api_key
        self._data_dir = data_dir
        # Static headers forwarded on every request. Used by declarative
        # providers whose ``auth_kind == "header"`` carry their credential in
        # a custom header (e.g. ``X-Api-Key``) instead of the default vendor
        # ``x-api-key`` auth — see :func:`declarative._build_inner`. ``None``
        # keeps the historic api_key/OAuth-only behaviour.
        self._default_headers = default_headers
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
        # Single-flight gate for the async on-use OAuth refresh
        # (:meth:`_ensure_fresh`). Mirrors the Codex provider: two concurrent
        # chat streams sharing the same expiring credential must not both POST
        # to the token endpoint (the server rotates ``refresh_token`` on each,
        # leaving the loser with a dead token). The lock is created lazily on
        # first use so the provider stays importable / constructible outside an
        # event loop (the synchronous resolution path never touches it).
        self._refresh_lock: Any | None = None

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

    async def _ensure_fresh(self) -> None:
        """Proactively refresh an expiring OAuth bearer before ``chat_stream``.

        Unlike the synchronous :meth:`_resolve_oauth_token` path (which is a
        no-op when called inside a running loop — see
        ``provider-auth-anthropic-refresh-noop``), this runs on the async hot
        path and actually performs the network refresh. Single-flight via
        :attr:`_refresh_lock`: two concurrent streams that both observe an
        expiring credential serialise so only one POST hits the token
        endpoint; the second arrival re-checks expiry under the lock and
        no-ops when the first already rotated the token.

        Best-effort: a missing data_dir / credential file, a credential
        without a refresh token, or a refresh failure all leave the existing
        (possibly stale) access token in place — the upstream returns a 401
        if it is truly dead and the reactive 401 path (below) recovers.
        """
        if self._data_dir is None:
            return
        cred = self._load_credential_cached()
        if cred is None:
            return
        # Fast-path: a credential comfortably within its window needs no
        # refresh and no lock.
        if not (cred.is_expired(skew_seconds=120) and cred.refresh_token):
            return
        lock = self._get_refresh_lock()
        async with lock:
            # Re-read under the lock: a racing task may have rotated the file.
            cred = self._load_credential_cached() or cred
            if not (cred.is_expired(skew_seconds=120) and cred.refresh_token):
                return
            await self._do_refresh(cred)

    async def _async_refresh_credential(self) -> bool:
        """Reactive 401 recovery: force a single OAuth refresh + retry signal.

        Returns ``True`` when the in-process credential was rotated (so the
        caller should retry the open phase), ``False`` otherwise. Serialised
        through the same :attr:`_refresh_lock` as :meth:`_ensure_fresh` so a
        proactive refresh racing a 401 recovery cannot double-POST.
        """
        if self._data_dir is None:
            return False
        cred = self._load_credential_cached()
        if cred is None or not cred.refresh_token:
            return False
        pre_token = cred.access_token
        lock = self._get_refresh_lock()
        async with lock:
            cred = self._load_credential_cached() or cred
            if cred.access_token != pre_token:
                # Another task already rotated the token while we waited —
                # retry with the fresh one without issuing our own POST.
                return True
            if not cred.refresh_token:
                return False
            return await self._do_refresh(cred)

    async def _do_refresh(self, cred: AnthropicOAuthCredential) -> bool:
        """Run the async token refresh + persist. Returns True on success.

        Caller MUST hold :attr:`_refresh_lock`. Updates the credential memo
        with the freshly-written value so the next request reuses it without
        re-reading the file.
        """
        assert cred.refresh_token is not None
        try:
            refreshed = await refresh_anthropic_token(refresh_token=cred.refresh_token)
        except Exception as exc:  # noqa: BLE001 — refresh failure must not kill the turn
            logger.warning("anthropic.oauth_refresh_failed", error=str(exc))
            return False
        new_cred = cred.with_refreshed(
            access_token=refreshed["access_token"],
            refresh_token=refreshed.get("refresh_token"),
            expires_at_ms=refreshed.get("expires_at_ms"),
        )
        if self._data_dir is not None:
            try:
                save_anthropic_credential(self._data_dir, new_cred)
            except OSError as exc:
                logger.warning("anthropic.oauth_save_failed", error=str(exc))
        # Prime the memo with the freshly-written credential keyed on the
        # post-write stat; drop the key on stat failure so the next call
        # re-reads from disk.
        try:
            st = self._credential_file_path().stat()
            self._cred_cache_key = (st.st_mtime_ns, st.st_size)
            self._cred_cache_value = new_cred
        except OSError:
            self._cred_cache_key = None
            self._cred_cache_value = None
        return True

    def _get_refresh_lock(self) -> Any:
        """Return the single-flight lock, creating it lazily on first use.

        Created lazily so the provider stays constructible outside an event
        loop. ``asyncio.Lock()`` binds to the running loop on first await; all
        refresh calls happen on the same chat-stream loop so a single lazily
        created lock is correct.
        """
        if self._refresh_lock is None:
            import asyncio

            self._refresh_lock = asyncio.Lock()
        return self._refresh_lock

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
        # OAuth proactive refresh: when the active credential is a soon-to-
        # expire OAuth bearer, refresh it (single-flight) BEFORE resolving the
        # token we'll bind to the client. Runs on the async hot path — the
        # synchronous resolution path's refresh is a no-op inside a loop
        # (provider-auth-anthropic-refresh-noop), so this is where the
        # proactive refresh actually fires.
        await self._ensure_fresh()

        token, style = self._credential_resolution()
        # Custom-header auth carries the credential in ``_default_headers``
        # rather than a resolved token, so a missing token is only an error
        # when no header credential is configured either.
        if not token and not self._default_headers:
            raise RuntimeError("API key missing: set ANTHROPIC_API_KEY")

        # Imported lazily so test environments without the SDK still import this
        # module (and so importing the module doesn't require network).
        from anthropic import AsyncAnthropic  # type: ignore[import-not-found]

        def _build_client(bearer_token: str | None) -> Any:
            """Construct the AsyncAnthropic client for the resolved auth path.

            Factored into a closure so the reactive 401 path can rebuild it
            with a freshly-refreshed bearer token after recovery.
            """
            if self._default_headers:
                # Custom-header auth: the real credential rides in the declared
                # header (already baked into ``_default_headers``). The
                # Anthropic SDK refuses to build a request unless an auth method
                # is set OR one of ``x-api-key`` / ``Authorization`` is
                # explicitly carried, so feed it a non-credential sentinel
                # ``api_key`` — the default ``x-api-key`` then carries the
                # sentinel, never the secret. Gateways keyed on the custom
                # header ignore it. (When the custom header IS ``x-api-key`` the
                # supplied header value overrides the sentinel, so the real key
                # still rides ``x-api-key``.)
                return AsyncAnthropic(
                    api_key=_HEADER_AUTH_SENTINEL,
                    default_headers=dict(self._default_headers),
                )
            if style == "bearer":
                # OAuth path: the Anthropic SDK supports ``auth_token=`` which
                # sets ``Authorization: Bearer <token>`` and suppresses the
                # default ``x-api-key`` header. Identity headers are required
                # so the Anthropic API recognises the OAuth subscription token.
                _OAUTH_EXTRA_HEADERS = {
                    "anthropic-beta": "oauth-2025-04-20",
                    "x-app": "cli",
                    "user-agent": "claude-cli/2.1.88 (claude-code)",
                }
                return AsyncAnthropic(
                    auth_token=bearer_token, default_headers=_OAUTH_EXTRA_HEADERS
                )
            return AsyncAnthropic(api_key=bearer_token)

        client = _build_client(token)
        system, anthropic_messages = _split_system(messages)
        if style == "bearer":
            _cc_prefix = "You are Claude Code, Anthropic's official CLI for Claude.\n\n"
            system = (_cc_prefix + system) if system else _cc_prefix
        _caching = _supports_prompt_caching(model)
        # Anthropic caps the number of ``cache_control`` markers per request at
        # 4. We spend that budget across (in priority order): the trailing
        # tools entry (large, stable tool schemas), the last stable system
        # block, then up to 2 trailing user turns. ``_cache_budget`` tracks
        # the remaining markers so we never emit a 5th and 400 the request.
        _cache_budget = 4 if _caching else 0
        if system:
            # The system block carries a cache_control marker only when the
            # model supports caching and we still have budget. The prefix is
            # always present on the bearer path; the marker is the cache hook.
            if _caching and _cache_budget > 0:
                _system_param: list[dict[str, Any]] | None = [
                    {
                        "type": "text",
                        "text": system,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                _cache_budget -= 1
            else:
                _system_param = [{"type": "text", "text": system}]
        else:
            _system_param = None
        _tools_list: list[dict[str, Any]] | None = None
        if tools:
            _tools_list = list(tools)
            # Attach cache_control to the trailing tools entry so the whole
            # (stable) tool array becomes a cached prefix segment. Spends one
            # marker from the budget; skipped when the budget is exhausted.
            if _caching and _cache_budget > 0:
                _tools_list = _inject_tools_cache_control(_tools_list)
                _cache_budget -= 1
        # WP8: inject cache_control on the last user-turn messages when the
        # model supports prompt caching and the system prompt is >= 1024 chars
        # (approximately 256 tokens — enough to make caching worthwhile). Bound
        # the number of marked turns by the remaining marker budget so the
        # total stays <= 4.
        if _caching and len(system or "") >= 1024 and _cache_budget > 0:
            anthropic_messages = _inject_user_cache_control(
                anthropic_messages, n_turns=min(2, _cache_budget)
            )
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens if max_tokens else 1024,
        }
        if _system_param:
            kwargs["system"] = _system_param
        if temperature is not None:
            kwargs["temperature"] = temperature
        if _tools_list:
            kwargs["tools"] = _tools_list
        if extra:
            kwargs.update(extra)
        # Canonical reasoning tier → Anthropic wire shape. 4.6+/Fable take
        # adaptive thinking + ``output_config.effort``; the budget_tokens era
        # (4.5 and below, all Haiku) has no effort knob so the tier is
        # dropped (clamp returns None) and the request stays untouched.
        _requested_effort = kwargs.pop("reasoning_effort", None)
        if isinstance(_requested_effort, str) and _requested_effort.strip():
            _tier = clamp_reasoning_tier(model, _requested_effort)
            if _tier:
                kwargs["thinking"] = {"type": "adaptive"}
                _extra_body = kwargs.setdefault("extra_body", {})
                if isinstance(_extra_body, dict):
                    _extra_body.setdefault("output_config", {})
                    if isinstance(_extra_body["output_config"], dict):
                        _extra_body["output_config"]["effort"] = _tier

        _max_retries = 3
        _attempt = 0
        # One-shot reactive 401 recovery (provider-auth-anthropic-refresh-noop):
        # if the upstream rejects the bearer with a 401 we refresh the OAuth
        # token once, rebuild the client, and retry. Only the OAuth (``bearer``)
        # path is eligible — an api_key 401 is not self-healable here.
        _did_401_refresh = False
        try:
            while True:
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
                        _raw_usage = getattr(final, "usage", None)
                        _usage_dict: dict[str, int] | None = None
                        if _raw_usage is not None:
                            _usage_dict = {}
                            for _uk in (
                                "input_tokens",
                                "output_tokens",
                                "cache_read_input_tokens",
                                "cache_creation_input_tokens",
                            ):
                                _uv = getattr(_raw_usage, _uk, None)
                                if isinstance(_uv, int):
                                    _usage_dict[_uk] = _uv
                            if not _usage_dict:
                                _usage_dict = None
                        yield ProviderChunk(kind="done", finish_reason=finish, usage=_usage_dict)
                    break  # success — exit retry loop
                except CorlinmanError as _cl_exc:
                    _mapped: CorlinmanError = _cl_exc
                except Exception as _raw_exc:
                    # Map the vendor SDK exception to our typed hierarchy so the
                    # retry / 401-recovery arms below can discriminate on type.
                    _mapped = _map_anthropic_error(_raw_exc, model=model)
                    _mapped.__cause__ = _raw_exc
                # --- error dispatch (shared by mapped + raw paths) ----------
                # Reached only when one of the two ``except`` arms above fired
                # (the success path ``break``s out of the loop before here).
                # Rate-limit / overload are NOT retried at the adapter layer.
                # The Anthropic SDK already retries transient 429/503/529, and
                # cross-call backoff + model fallback are owned by the reasoning
                # loop. We raise the typed error (carrying retry_after_ms /
                # reset_at_ms) so the loop can act on it. (A previous adapter-
                # level sleep-retry here double-retried and made the error-
                # mapping tests block for minutes — removed.)
                if (
                    isinstance(_mapped, AuthError)
                    and style == "bearer"
                    and not _did_401_refresh
                ):
                    _did_401_refresh = True
                    recovered = await self._async_refresh_credential()
                    if recovered:
                        # Rebuild the client with the freshly-refreshed bearer
                        # token, closing the stale one first to avoid leaking
                        # its httpx pool (audit R1-003 lineage).
                        await _safe_close(client)
                        _new_token, _ = self._credential_resolution()
                        client = _build_client(_new_token)
                        continue
                raise _mapped
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


# ---------------------------------------------------------------------------
# WP8: Prompt caching helpers
# ---------------------------------------------------------------------------

# Model prefixes that support the cache_control: {type: ephemeral} extension.
# claude-3-haiku, claude-3-5-*, claude-3-7-*, claude-opus-4, claude-sonnet-4-*
_CACHING_MODEL_PREFIXES: tuple[str, ...] = (
    "claude-3-haiku",
    "claude-3-5",
    "claude-3-7",
    "claude-opus-4",
    "claude-sonnet-4",
    "claude-haiku-4",
)


def _supports_prompt_caching(model: str) -> bool:
    """Return True when ``model`` supports the ``cache_control`` extension."""
    return any(model.startswith(p) for p in _CACHING_MODEL_PREFIXES)


def _inject_tools_cache_control(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a copy of ``tools`` with cache_control on the trailing entry.

    Anthropic caches a tool array prefix when the LAST tool definition
    carries ``{"cache_control": {"type": "ephemeral"}}`` — the marker on the
    final entry caches every tool definition up to and including it. We copy
    only the trailing entry (shallow-copy the list, deep-copy the one dict we
    touch) so the caller's list is never mutated. A non-dict trailing entry or
    an empty list is returned unchanged (idempotent passthrough).
    """
    if not tools:
        return tools
    last = tools[-1]
    if not isinstance(last, dict):
        return tools
    result = list(tools)
    new_last = dict(last)
    new_last["cache_control"] = {"type": "ephemeral"}
    result[-1] = new_last
    return result


def _inject_user_cache_control(
    messages: list[dict[str, Any]], *, n_turns: int = 2
) -> list[dict[str, Any]]:
    """Return a copy of ``messages`` with cache_control on the last N user turns.

    Injects ``{"cache_control": {"type": "ephemeral"}}`` on the last text
    block of the last ``n_turns`` user-role messages. This tells Anthropic's
    API to treat those messages as part of the stable prompt prefix and cache
    them, reducing input-token costs on subsequent rounds that replay the same
    history.

    Only modifies user-turn string content (converts to list form) or the
    last text block in an existing list. Tool-result blocks are left as-is
    (they appear as ``role="user"`` in Anthropic's wire format but carry
    ``type="tool_result"`` blocks — we don't cache those).

    Returns the input list unchanged if there are fewer than 1 user turns
    (passthrough, idempotent for short conversations).
    """
    # Find the indices of user-role messages (in reverse order).
    user_indices = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if not user_indices:
        return messages

    # Work on a shallow copy; only the targeted messages are deep-copied.
    result = list(messages)
    marked = 0
    for idx in reversed(user_indices):
        if marked >= n_turns:
            break
        msg = result[idx]
        content = msg.get("content")
        if isinstance(content, str):
            # Convert plain string → list with cache_control on the single block.
            new_msg = dict(msg)
            new_msg["content"] = [
                {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
            ]
            result[idx] = new_msg
            marked += 1
        elif isinstance(content, list) and content:
            # Find the last text block and annotate it.
            new_content = list(content)
            for bi in range(len(new_content) - 1, -1, -1):
                blk = new_content[bi]
                if isinstance(blk, dict) and blk.get("type") in ("text", "tool_result"):
                    if blk.get("type") == "tool_result":
                        # Skip tool_result blocks — caching those is less useful
                        # and may confuse the API.
                        continue
                    new_blk = dict(blk)
                    new_blk["cache_control"] = {"type": "ephemeral"}
                    new_content[bi] = new_blk
                    break
            new_msg = dict(msg)
            new_msg["content"] = new_content
            result[idx] = new_msg
            marked += 1
    return result


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

    Tool-calling turns (OpenAI shape, fed back by the reasoning loop /
    ``agent_servicer.feed_tool_result``) are translated to Anthropic's
    vendor blocks (audit B1):

    * an assistant message carrying ``tool_calls`` →
      ``{"type": "tool_use", "id": ..., "name": ..., "input": {...}}``
      blocks (preceded by any text content), so the call survives the
      round-trip instead of collapsing to an empty assistant turn;
    * a ``role="tool"`` message →
      ``{"role": "user", "content": [{"type": "tool_result",
      "tool_use_id": ..., "content": ...}]}``.

    PARALLEL tool calls (audit G2): when an assistant turn emits N>1
    ``tool_use`` blocks, Anthropic requires ALL N answering
    ``tool_result`` blocks in the ONE immediately-following user turn
    (separate user turns 400). We therefore COALESCE a run of consecutive
    ``role="tool"`` messages into a single user turn whose content is the
    ordered list of their ``tool_result`` blocks, flushing that run before
    the next non-tool message.
    """
    system_parts: list[str] = []
    chat: list[dict[str, Any]] = []
    # Accumulator for a run of consecutive tool results; flushed as one
    # user turn before the next non-tool message (parallel-tool fix).
    pending_tool_results: list[dict[str, Any]] = []

    def _flush_tool_results() -> None:
        if pending_tool_results:
            chat.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for m in messages:
        role = _get(m, "role")
        content = _get(m, "content")
        if role == "tool":
            # OpenAI tool result → Anthropic tool_result block. Accumulate
            # so consecutive results coalesce into one user turn.
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": _get(m, "tool_call_id") or "",
                "content": content if isinstance(content, str) else (content or ""),
            }
            # C4: the reasoning loop marks an errored tool result with a
            # truthy ``_is_error`` key on the ``role="tool"`` message; surface
            # it as Anthropic's ``is_error`` on the tool_result block so the
            # model can distinguish a failed call from a successful one.
            if _get(m, "_is_error"):
                block["is_error"] = True
            pending_tool_results.append(block)
            continue
        # Any non-tool message ends a run of tool results.
        _flush_tool_results()
        if role == "system":
            text = _content_to_text(content)
            if text:
                system_parts.append(text)
        else:
            tool_calls = _get(m, "tool_calls")
            if tool_calls:
                # Assistant tool-call turn → text (if any) + tool_use blocks.
                blocks: list[dict[str, Any]] = []
                text = _content_to_text(content)
                if text:
                    blocks.append({"type": "text", "text": text})
                blocks.extend(_tool_calls_to_anthropic_blocks(tool_calls))
                if not blocks:
                    # Zero-block guard: every tool_call was filtered (all
                    # non-dict) and there was no text — Anthropic rejects
                    # an empty content array, so emit a fallback text block
                    # (mirrors _parts_to_anthropic_blocks).
                    blocks = [{"type": "text", "text": ""}]
                chat.append({"role": "assistant", "content": blocks})
                continue
            # Anthropic requires role in {"user", "assistant"}.
            anth_role = "user" if role == "user" else "assistant"
            if isinstance(content, list):
                content_blocks = _parts_to_anthropic_blocks(content)
                chat.append({"role": anth_role, "content": content_blocks})
            else:
                chat.append({"role": anth_role, "content": content or ""})
    # Flush a trailing run of tool results (the common multi-round case
    # ends here: assistant tool_use turn followed by the tool results).
    _flush_tool_results()
    system = "\n\n".join(system_parts) if system_parts else None
    return system, chat


def _tool_calls_to_anthropic_blocks(tool_calls: Any) -> list[dict[str, Any]]:
    """Translate OpenAI-shape ``tool_calls`` into Anthropic ``tool_use`` blocks.

    Each OpenAI entry is ``{"id": ..., "type": "function", "function":
    {"name": ..., "arguments": <json string>}}`` (the exact shape emitted
    by ``reasoning_loop._extend_with_tool_round``). Anthropic's
    ``tool_use`` block carries the arguments as a parsed ``input`` dict, so
    we ``json.loads`` the arguments string and fall back to ``{}`` on a
    malformed / empty payload rather than failing the whole request.
    """
    blocks: list[dict[str, Any]] = []
    if not isinstance(tool_calls, list):
        return blocks
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments")
        if isinstance(raw_args, dict):
            args: Any = raw_args
        else:
            try:
                args = json.loads(raw_args) if raw_args else {}
            except (ValueError, TypeError):
                args = {}
        blocks.append(
            {
                "type": "tool_use",
                "id": tc.get("id") or "",
                "name": fn.get("name") or "",
                "input": args,
            }
        )
    return blocks


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
    * ``{"type": "file", "file": {...}}`` with inline ``file_data`` →
      PDF / plain-text payloads become Anthropic ``document`` blocks
      (:func:`_file_block_from_part`); other mimes degrade to a text
      block naming the file so the model can tell the user the active
      provider can't read it (instead of being silently blind to an
      upload the UI accepted).
    """
    blocks: list[dict[str, Any]] = []

    def _file_block_from_part(f: dict[str, Any]) -> dict[str, Any] | None:
        """Best-effort ``file`` part → Anthropic block.

        PDF payloads map to the GA ``document``/``base64`` block; plain
        text maps to ``document``/``text``. Anything else (audio, video,
        binary formats Anthropic has no block for) degrades to a text
        block that NAMES the attachment — the model can then tell the
        user it can't read it, instead of silently never seeing an
        upload the UI accepted.
        """
        name = str(f.get("file_name") or f.get("filename") or "attachment")
        mime = str(f.get("mime") or "").split(";", 1)[0].strip().lower()
        data_url = str(f.get("file_data") or "")
        b64 = ""
        if data_url.startswith("data:") and ";base64," in data_url:
            b64 = data_url.split(",", 1)[1]
        if b64 and mime == "application/pdf":
            return {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": b64,
                },
            }
        if b64 and (mime.startswith("text/") or mime == "application/json"):
            try:
                text = base64.b64decode(b64).decode("utf-8", errors="replace")
            except (binascii.Error, ValueError):
                text = ""
            if text:
                return {
                    "type": "document",
                    "source": {
                        "type": "text",
                        "media_type": "text/plain",
                        "data": text,
                    },
                }
        logger.warning(
            "anthropic.unsupported_attachment",
            kind=f.get("kind"),
            mime=mime,
        )
        return {
            "type": "text",
            "text": (
                f"[attachment {name!r} ({mime or 'unknown type'}) was "
                "provided but this model cannot read that format]"
            ),
        }

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
            block = _file_block_from_part(part.get("file") or {})
            if block is not None:
                blocks.append(block)
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


def _headers_from_exc(exc: Exception) -> Any:
    """Return a header mapping off a vendor SDK exception, or ``None``.

    The Anthropic SDK preserves the upstream headers on
    ``exc.response.headers``; some error shapes attach them directly on
    the exception instead. Returns the first mapping-like object found.
    """
    response = getattr(exc, "response", None)
    headers: Any = getattr(response, "headers", None) if response is not None else None
    if headers is None:
        # The SDK sometimes attaches headers directly on the exception.
        headers = getattr(exc, "headers", None)
    if headers is None or not hasattr(headers, "get"):
        return None
    return headers


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
    headers = _headers_from_exc(exc)
    if headers is None:
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


def _unified_reset_ms_from_exc(exc: Exception) -> int | None:
    """Parse the ``anthropic-ratelimit-unified-reset`` header to an epoch-ms.

    Anthropic's unified rate-limit window publishes its reset point via the
    ``anthropic-ratelimit-unified-reset`` response header. Two encodings are
    seen in the wild: an absolute Unix timestamp (seconds since epoch — the
    documented form) or a small delta-seconds value (a relative reset). We
    disambiguate by magnitude: values below a ~10-year span (``< 10**9``) are
    treated as a delta from now, larger values as an absolute epoch. Returns
    the reset point as epoch milliseconds, or ``None`` when the header is
    absent / unparseable — callers then fall back to ``retry_after_ms`` or
    the default backoff schedule.
    """
    headers = _headers_from_exc(exc)
    if headers is None:
        return None
    try:
        raw = headers.get("anthropic-ratelimit-unified-reset") or headers.get(
            "Anthropic-Ratelimit-Unified-Reset"
        )
    except Exception:  # defensive: tolerate odd header mappings
        return None
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    import time as _time

    # Heuristic split: a bare delta-seconds window (server resets in N s)
    # vs an absolute Unix-epoch-seconds reset point.
    if value < 1_000_000_000:
        return int((_time.time() + value) * 1000)
    return int(value * 1000)


# Pattern matching Anthropic's context-overflow body. The vendor phrases it
# as e.g. ``input length and ``max_tokens`` exceed context limit: 200000 +
# 8192 > 200000`` — three integers ``A + B > C`` where C is the model's
# context window. We pull all three so the reasoning loop can compute an
# available-context budget and shrink-retry.
_CONTEXT_OVERFLOW_RE = re.compile(
    r"(\d[\d,]*)\s*\+\s*(\d[\d,]*)\s*>\s*(\d[\d,]*)"
)


def _parse_context_overflow_limits(message: str) -> tuple[int, int, int] | None:
    """Extract ``(input_tokens, max_tokens, limit)`` from an overflow body.

    Returns ``None`` when the ``A + B > C`` shape is absent so the caller
    raises a plain :class:`ContextOverflowError` without numeric hints.
    """
    m = _CONTEXT_OVERFLOW_RE.search(message or "")
    if m is None:
        return None
    try:
        a = int(m.group(1).replace(",", ""))
        b = int(m.group(2).replace(",", ""))
        c = int(m.group(3).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return a, b, c


def _build_rate_limit_error(
    exc: Exception, *, status_code: int, ctx: dict[str, Any]
) -> RateLimitError:
    """Build a :class:`RateLimitError`, attaching window-based reset_at_ms.

    In addition to the standard ``retry-after`` delta-seconds parse we read
    Anthropic's ``anthropic-ratelimit-unified-reset`` header (window-based
    reset point) and attach it as ``reset_at_ms`` so the failover/retry
    layer can wait until the unified window actually reopens rather than
    relying solely on a relative delay. ``failover.RateLimitError`` does not
    declare the field as a constructor kwarg yet (see wire_contract), so we
    set it on the instance after construction — a no-op-safe extra attribute
    that readers access via ``getattr(err, "reset_at_ms", None)``.
    """
    err = RateLimitError(
        str(exc),
        status_code=status_code,
        retry_after_ms=_retry_after_ms_from_exc(exc),
        **ctx,
    )
    err.reset_at_ms = _unified_reset_ms_from_exc(exc)  # type: ignore[attr-defined]
    return err


def _build_context_overflow_error(
    exc: Exception, *, ctx: dict[str, Any]
) -> ContextOverflowError:
    """Build a :class:`ContextOverflowError` carrying the parsed numeric limit.

    Parses the ``A + B > C`` triple out of the vendor body and attaches
    ``input_tokens`` (A), ``max_tokens`` (B), and ``limit`` (C) onto the
    instance so the reasoning loop can compute an available-context budget
    (``limit - input_tokens - buffer``) and shrink-retry with a smaller
    ``max_tokens``. ``failover.ContextOverflowError`` does not declare these
    as constructor kwargs (see wire_contract); readers use ``getattr(err,
    "limit", None)`` etc.
    """
    err = ContextOverflowError(str(exc), status_code=400, **ctx)
    parsed = _parse_context_overflow_limits(str(exc))
    if parsed is not None:
        input_tokens, max_tokens, limit = parsed
        err.input_tokens = input_tokens  # type: ignore[attr-defined]
        err.max_tokens = max_tokens  # type: ignore[attr-defined]
        err.limit = limit  # type: ignore[attr-defined]
    return err


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
        return _build_rate_limit_error(exc, status_code=429, ctx=ctx)
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
            return _build_context_overflow_error(exc, ctx=ctx)
        return FormatError(str(exc), status_code=400, **ctx)
    if isinstance(exc, APIStatusError):
        status = getattr(exc, "status_code", 0) or 0
        if status == 503 or status == 529:
            return OverloadedError(str(exc), status_code=status, **ctx)
        if status == 429:
            return _build_rate_limit_error(exc, status_code=status, ctx=ctx)
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
        "reasoning_effort": {
            "type": "string",
            # Canonical tier superset — clamped onto the model's real
            # ladder (output_config.effort, 4.6+/Fable only) in chat_stream.
            "enum": ["none", "minimal", "low", "on", "medium", "high", "xhigh", "max"],
            "description": "Canonical reasoning-effort tier (clamped per model).",
        },
    },
}
