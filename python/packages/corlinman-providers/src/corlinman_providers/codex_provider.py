"""Codex (ChatGPT subscription) OAuth provider.

Calls https://chatgpt.com/backend-api/codex using the OpenAI Responses API
with Cloudflare bypass headers. This is NOT the standard api.openai.com/v1/
endpoint — using that endpoint with a Codex OAuth token returns 429 quota
errors because ChatGPT subscriptions don't grant OpenAI API credits.

The Codex backend uses the Responses API (/responses), not chat/completions,
and rejects temperature and max_output_tokens parameters.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from functools import partial
from pathlib import Path
from typing import Any, ClassVar

import structlog

from corlinman_providers._codex_oauth import (
    CodexOAuthCredential,
    CodexOAuthRefreshError,
    codex_cloudflare_headers,
    load_codex_credential,
    persist_codex_credential,
    refresh_codex_token,
)
from corlinman_providers._retry import default_retryable_codex, with_retry
from corlinman_providers.base import ProviderChunk
from corlinman_providers.specs import ProviderKind, ProviderSpec

logger = structlog.get_logger(__name__)

_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
_DEFAULT_MODEL = "gpt-5.5"


def _msg_attr(msg: Any, name: str) -> Any:
    """Read ``name`` off a dict-or-object chat message."""
    if isinstance(msg, dict):
        return msg.get(name)
    return getattr(msg, name, None)


def _messages_to_responses_input(messages: Sequence[Any]) -> list[dict]:
    """Convert OpenAI chat messages to Responses API input items.

    Handles plain user/assistant text plus the tool-calling round-trip
    the reasoning loop drives:

    * an ``assistant`` message carrying ``tool_calls`` becomes one
      ``function_call`` item per call;
    * a ``role="tool"`` message becomes a ``function_call_output`` item
      keyed by ``tool_call_id``.

    System messages are skipped — the caller lifts them into the
    Responses API ``instructions`` field.
    """
    result: list[dict] = []
    for msg in messages:
        role = _msg_attr(msg, "role")
        content = _msg_attr(msg, "content")
        if role == "user":
            result.append({
                "role": "user",
                "content": [{"type": "input_text", "text": str(content or "")}],
            })
        elif role == "assistant":
            tool_calls = _msg_attr(msg, "tool_calls")
            if tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                    result.append({
                        "type": "function_call",
                        "call_id": str(tc.get("id", "") if isinstance(tc, dict) else ""),
                        "name": str(fn.get("name", "")),
                        "arguments": str(fn.get("arguments", "") or "{}"),
                    })
            if content:
                result.append({
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": str(content)}],
                })
        elif role == "tool":
            result.append({
                "type": "function_call_output",
                "call_id": str(_msg_attr(msg, "tool_call_id") or ""),
                "output": str(content or ""),
            })
    return result


def _is_terminal_event(event: Any) -> bool:
    """``response.incomplete`` / ``response.failed`` mean the server gave up."""
    event_type = getattr(event, "type", "")
    return event_type in {"response.incomplete", "response.failed"}


# Responses-API usage keys we forward onto the ``done`` ProviderChunk.
# ``input_tokens`` / ``output_tokens`` are the durable cross-vendor pair
# the cost meter aggregates on; the cached / reasoning fields are
# best-effort extras included only when the upstream reported them.
_USAGE_INT_KEYS = (
    "input_tokens",
    "output_tokens",
    "cached_input_tokens",
    "cached_output_tokens",
    "reasoning_tokens",
)
_CODEX_REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})


def _reasoning_effort_from_extra(extra: dict[str, Any] | None) -> str:
    if not isinstance(extra, dict):
        return "medium"
    raw = extra.get("reasoning_effort")
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in _CODEX_REASONING_EFFORTS:
            return value
    return "medium"


def _extract_usage(event: Any) -> dict[str, int] | None:
    """Pull a plain ``dict[str, int]`` off a ``response.completed`` event.

    The Responses-API ``response.completed`` event carries the final
    response object on ``event.response``; its ``usage`` attribute is
    the vendor's token accounting (a pydantic model on the live SDK, a
    ``SimpleNamespace`` in tests). We read only the documented integer
    fields, coerce defensively, and skip anything missing or
    non-coercible. Returns ``None`` if no usable fields were present
    so callers can leave the ``done`` chunk's ``usage`` attribute at
    its default ``None``.
    """
    response = getattr(event, "response", None)
    if response is None:
        return None
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    out: dict[str, int] = {}
    for key in _USAGE_INT_KEYS:
        raw = getattr(usage, key, None)
        if raw is None and isinstance(usage, dict):
            raw = usage.get(key)
        if raw is None:
            continue
        try:
            out[key] = int(raw)
        except (TypeError, ValueError):
            # Tolerate weird upstream shapes — drop the field, keep the rest.
            continue
    return out or None


def _log_terminal(event: Any) -> None:
    resp_obj = getattr(event, "response", None)
    status = getattr(resp_obj, "status", None) if resp_obj else None
    logger.warning(
        "codex.stream_terminated",
        event_type=getattr(event, "type", ""),
        status=status,
    )


def _is_token_invalidated(exc: BaseException) -> bool:
    """Detect a server-side ChatGPT token revocation.

    The Codex backend invalidates a token (returns HTTP 401 with
    ``error.code == "token_invalidated"``) when the user signs out, the
    account rotates a session, or the server otherwise decides the
    token is no longer trustworthy — independent of JWT ``exp``. The
    refresh token is invalidated separately with code
    ``refresh_token_invalidated``; that branch we cannot recover from
    locally and the caller should surface the original error.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None) if resp is not None else None
    if status != 401:
        return False
    body = ""
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            data = resp.json()
            code = data.get("error", {}).get("code", "") if isinstance(data, dict) else ""
            if code == "token_invalidated":
                return True
            if code == "refresh_token_invalidated":
                # Refresh token already dead — caller cannot self-heal.
                return False
            body = str(data)
        except Exception:  # noqa: BLE001 — best-effort body sniff
            body = ""
    msg = (str(exc) + " " + body).lower()
    if "refresh_token_invalidated" in msg:
        return False
    return "token_invalidated" in msg or "token has been invalidated" in msg


async def _safe_close(client: Any) -> None:
    """Best-effort ``await client.close()`` that never masks the real exception.

    Mirrors the OpenAI / Anthropic providers' helper (audit R1-003). The
    Codex backend is driven through an ``AsyncOpenAI`` client whose
    ``close()`` is async + idempotent and drains the underlying
    ``httpx.AsyncClient`` pool — the ``responses.stream(...)`` CM only
    closes the response, not the owning client's pool. A close-time error
    stays in the log but never bubbles up into the chat-stream flow
    (which would mask the original error). Test doubles without a
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
        logger.warning("codex.client_close_failed", error=str(exc))


async def _open_stream_and_first_event(
    client: Any, kwargs: dict[str, Any]
) -> tuple[Any, Any] | None:
    """Enter ``client.responses.stream(**kwargs)`` and pull the first event.

    Used as the retry boundary by :meth:`CodexProvider.chat_stream`: if
    either the ``async with`` open or the first ``__anext__`` raises a
    transient error, the whole open phase is retried. We hand back the
    already-entered context manager + its async iterator + the first
    event we already consumed; the caller is responsible for exiting
    the CM.

    Returns ``None`` if the stream opens but closes without yielding
    any events at all (rare — surfaced as a no-content done in the
    caller).

    The returned object is the original CM with a ``_cm_iter``
    attribute attached for the caller to drive the remaining events.
    """
    stream_cm = client.responses.stream(**kwargs)
    entered = await stream_cm.__aenter__()
    try:
        stream_iter = entered.__aiter__()
    except Exception:
        await stream_cm.__aexit__(None, None, None)
        raise

    try:
        first_event = await stream_iter.__anext__()
    except StopAsyncIteration:
        # Stream closed immediately. Tell the caller to emit a no-content
        # done by returning None. We exit the CM ourselves since the
        # caller never gets a chance to.
        try:
            await stream_cm.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass
        return None
    except BaseException:
        # First-event failure — surface to retry logic. Exit the CM
        # cleanly so the next attempt opens a fresh one.
        try:
            await stream_cm.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass
        raise

    # Stash the iterator on the CM so the caller can consume the
    # remainder without re-iterating from scratch.
    stream_cm._cm_iter = stream_iter  # type: ignore[attr-defined]
    return stream_cm, first_event


class CodexProvider:
    """Codex (ChatGPT subscription) OAuth provider.

    Calls chatgpt.com/backend-api/codex with the Responses API and
    Cloudflare bypass headers sourced from ~/.codex/auth.json.
    Tokens are auto-refreshed when close to expiry.
    """

    name: ClassVar[str] = "codex"
    kind: ClassVar[ProviderKind] = ProviderKind.CODEX

    #: Default model surfaced to the channels runtime when ``models.default``
    #: is not set in config and Codex is auto-detected.
    DEFAULT_MODEL: ClassVar[str] = _DEFAULT_MODEL

    def __init__(
        self,
        *,
        credential: CodexOAuthCredential,
        credential_path: Path | None = None,
    ) -> None:
        self._credential = credential
        self._credential_path = credential_path
        # Single-flight gate for token refresh. Both ``_ensure_fresh``
        # (the proactive JWT-exp path) and ``_attempt_token_recovery``
        # (the reactive 401 ``token_invalidated`` path) acquire this
        # before issuing a POST to ``/oauth/token``. Without it, two
        # concurrent chat streams sharing the same expired credential
        # both fire a refresh request; the auth server rotates
        # ``refresh_token`` on each, only one process wins, and the
        # other ends up holding a dead refresh token. Holding this
        # lock through the refresh + ``_credential`` write ensures
        # exactly one HTTP POST per genuine expiry / invalidation.
        self._refresh_lock = asyncio.Lock()

    @classmethod
    def build(
        cls,
        spec: ProviderSpec,
        *,
        data_dir: Path | None = None,
        **_kwargs: Any,
    ) -> CodexProvider:
        """Load the Codex credential from the operator data dir and build.

        Raises :class:`RuntimeError` when the file is missing or has no
        ``access_token`` — the operator must run ``codex login`` first.
        """
        credential_path = Path(data_dir) / ".codex" / "auth.json" if data_dir else None
        cred = load_codex_credential(credential_path) if credential_path else None
        if cred is None:
            cred = load_codex_credential()
            if cred is not None:
                credential_path = None
        if cred is None:
            raise RuntimeError(
                "Codex provider: .codex/auth.json not found or missing tokens. "
                "Run `codex login` to authenticate."
            )
        return cls(credential=cred, credential_path=credential_path)

    @classmethod
    def supports(cls, model: str) -> bool:
        """Claim OpenAI / Codex model families."""
        return model.startswith(("gpt-5", "gpt-4", "o1-", "o3-", "o4-", "codex-", "chatgpt-"))

    def _make_client(self) -> Any:
        from openai import AsyncOpenAI

        return AsyncOpenAI(
            api_key=self._credential.access_token,
            base_url=_CODEX_BASE_URL,
            default_headers=codex_cloudflare_headers(self._credential.access_token),
        )

    async def chat_stream(
        self,
        *,
        model: str,
        messages: Sequence[Any],
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra: dict[str, Any] | None = None,
        background: bool = False,
    ) -> AsyncIterator[ProviderChunk]:
        """Stream Codex Responses-API chunks with retry on the open phase.

        The open + first-event phase is wrapped in
        :func:`corlinman_providers._retry.with_retry`. Once any token /
        tool-call chunk has been emitted, mid-stream failures still
        propagate (a partial-stream retry would duplicate text).

        ``background=True`` tells the retry classifier to treat 529
        overload as terminal — Claude Code's foreground/background gate.
        No caller passes this today; wired for future use.
        """
        await self._ensure_fresh()
        client = self._make_client()
        reasoning_effort = _reasoning_effort_from_extra(extra)
        # Hand ownership of the httpx pool to a try/finally so EVERY exit
        # path — success, early return, mid-stream error, generator
        # ``aclose()`` from a cancelled caller — closes the client. The
        # ``responses.stream(...)`` CM only closes the response, not the
        # owning client's pool; without this every Codex chat turn leaks a
        # pool entry (audit R4-D2, same class as R1-003). The token-recovery
        # path rebinds ``client`` after closing the first one (below), so the
        # ``finally`` always closes whichever client is live at exit.
        try:
            # Extract system prompt as instructions (Responses API uses
            # "instructions" instead of a system role message).
            instructions = ""
            payload_messages: list[Any] = list(messages)
            if payload_messages:
                first = payload_messages[0]
                first_role = (
                    first.get("role") if isinstance(first, dict)
                    else getattr(first, "role", None)
                )
                if first_role == "system":
                    instructions = (
                        first.get("content") if isinstance(first, dict)
                        else getattr(first, "content", "")
                    ) or ""
                    payload_messages = payload_messages[1:]

            kwargs: dict[str, Any] = {
                "model": model,
                "instructions": instructions,
                "input": _messages_to_responses_input(payload_messages),
                "store": False,
                "reasoning": {"effort": reasoning_effort, "summary": "auto"},
                "include": ["reasoning.encrypted_content"],
            }
            # NOTE: Codex backend rejects temperature and max_output_tokens — omit.

            if tools:
                kwargs["tools"] = [
                    {
                        "type": "function",
                        "name": t["function"]["name"],
                        "description": t["function"].get("description", ""),
                        "parameters": t["function"].get("parameters", {}),
                    }
                    for t in tools
                    if isinstance(t, dict) and "function" in t
                ]
                kwargs["tool_choice"] = "auto"

            # T3.5: prompt cache hint. The Responses API supports
            # ``prompt_cache_key`` — when the Codex backend honours it,
            # consecutive turns sharing a key reuse the cached prompt prefix.
            # Off by default since we cannot probe support without trying;
            # enable with ``CORLINMAN_CODEX_PROMPT_CACHE=1`` and pass the
            # cache key via ``extra={"prompt_cache_key": "<session>"}`` from
            # the caller (the agent servicer threads ``session_key`` in).
            # If the backend rejects the parameter the request fails clearly
            # with a 400 (T1.2's classifier won't retry, by design).
            if extra and isinstance(extra, dict):
                cache_key = extra.get("prompt_cache_key")
                if (
                    cache_key
                    and os.environ.get(
                        "CORLINMAN_CODEX_PROMPT_CACHE", ""
                    ).strip().lower()
                    in ("1", "true", "yes", "on")
                ):
                    kwargs["prompt_cache_key"] = str(cache_key)

            # --- Open phase with retry --------------------------------------
            # `_open_stream_once` enters the `responses.stream(...)` context
            # manager and pulls the first event. If either step raises a
            # transient error (429 / 5xx / connection blip), with_retry
            # backs off and tries again. We hand back the still-open CM
            # *and* the first event so we don't lose it. If the first event
            # already triggered a yield, we couldn't safely retry — which is
            # why the retry boundary is precisely first-event, not later.

            def _on_retry(attempt: int, delay: float, exc: BaseException) -> None:
                logger.warning(
                    "codex.retry",
                    attempt=attempt,
                    delay=round(delay, 3),
                    reason=type(exc).__name__,
                    error=str(exc)[:200],
                )

            try:
                open_result = await with_retry(
                    lambda: _open_stream_and_first_event(client, kwargs),
                    retryable=partial(default_retryable_codex, background=background),
                    on_retry=_on_retry,
                )
            except Exception as exc:
                # Reactive token recovery: when the Codex server has actively
                # invalidated the access_token (independent of JWT exp), refresh
                # with the still-valid refresh_token, persist, and retry the open
                # ONCE. Distinct from with_retry's generic backoff because
                # ``default_retryable_codex`` correctly treats 401 as terminal
                # for the generic case — we only re-attempt after we've fixed
                # the credential.
                if _is_token_invalidated(exc):
                    logger.warning("codex.token_invalidated_detected")
                    recovered = await self._attempt_token_recovery()
                    if recovered:
                        # Close the stale client BEFORE rebuilding — the
                        # first client (built with the invalidated token)
                        # would otherwise leak its httpx pool while the
                        # second takes over (audit R4-D2).
                        await _safe_close(client)
                        client = self._make_client()
                        try:
                            open_result = await with_retry(
                                lambda: _open_stream_and_first_event(client, kwargs),
                                retryable=partial(
                                    default_retryable_codex, background=background
                                ),
                                on_retry=_on_retry,
                            )
                        except Exception as exc2:
                            logger.warning(
                                "codex.stream_open_failed_after_recovery",
                                error=str(exc2),
                            )
                            yield ProviderChunk(kind="done", finish_reason="error")
                            return
                    else:
                        logger.warning(
                            "codex.stream_open_failed",
                            error=str(exc),
                            recovery="failed",
                        )
                        yield ProviderChunk(kind="done", finish_reason="error")
                        return
                else:
                    logger.warning("codex.stream_open_failed", error=str(exc))
                    yield ProviderChunk(kind="done", finish_reason="error")
                    return

            if open_result is None:
                # Stream opened but produced zero events (e.g. immediate
                # close). Surface a stop done — nothing to retry against.
                yield ProviderChunk(kind="done", finish_reason="stop")
                return

            stream_cm, first_event = open_result

            # item_id (fc_…) → call_id (call_…). Arg-delta events carry only
            # item_id; the call_id needed for the function_call_output
            # round-trip lives on the output_item.added event.
            call_ids: dict[str, str] = {}
            # call_id → whether any argument delta was streamed for it. Lets
            # us fall back to the full arguments on output_item.done when the
            # backend ships args in one shot instead of streaming fragments.
            args_streamed: dict[str, bool] = {}
            saw_tool_call = False
            # T1.4: capture token accounting from the ``response.completed``
            # event so the terminal ``done`` chunk can carry it back to the
            # reasoning loop → DoneEvent → server-side cost meter. Stays
            # ``None`` when the upstream omits usage (e.g. mid-stream
            # errors, retry that bailed pre-completion).
            captured_usage: dict[str, int] | None = None

            async def _process_event(event: Any) -> AsyncIterator[ProviderChunk]:
                """Translate one Responses-API event into ProviderChunks.

                Inner generator so the first-event we already pulled and the
                remaining events go through identical processing.
                """
                event_type = getattr(event, "type", "")
                if "output_text.delta" in event_type:
                    delta = getattr(event, "delta", "")
                    if delta:
                        yield ProviderChunk(kind="token", text=delta)
                elif event_type == "response.output_item.added":
                    item = getattr(event, "item", None)
                    if item is not None and getattr(item, "type", "") == "function_call":
                        item_id = getattr(item, "id", "") or ""
                        call_id = getattr(item, "call_id", "") or item_id
                        name = getattr(item, "name", "") or ""
                        call_ids[item_id] = call_id
                        args_streamed[call_id] = False
                        yield ProviderChunk(
                            kind="tool_call_start",
                            tool_call_id=call_id,
                            tool_name=name,
                        )
                elif event_type == "response.function_call_arguments.delta":
                    item_id = getattr(event, "item_id", "") or ""
                    call_id = call_ids.get(item_id, item_id)
                    delta = getattr(event, "delta", "") or ""
                    if delta:
                        args_streamed[call_id] = True
                        yield ProviderChunk(
                            kind="tool_call_delta",
                            tool_call_id=call_id,
                            arguments_delta=delta,
                        )
                elif event_type == "response.output_item.done":
                    item = getattr(event, "item", None)
                    if item is not None and getattr(item, "type", "") == "function_call":
                        item_id = getattr(item, "id", "") or ""
                        call_id = call_ids.get(item_id, item_id)
                        # Backend shipped args in one shot — replay them
                        # as a single delta so the loop can aggregate.
                        if not args_streamed.get(call_id):
                            full_args = getattr(item, "arguments", "") or ""
                            if full_args:
                                yield ProviderChunk(
                                    kind="tool_call_delta",
                                    tool_call_id=call_id,
                                    arguments_delta=str(full_args),
                                )
                        yield ProviderChunk(
                            kind="tool_call_end",
                            tool_call_id=call_id,
                        )

            terminated_early = False
            try:
                # ``stream_cm`` is already entered (by _open_stream_and_first_event)
                # so we drive the inner async iterator directly and close the
                # CM in the finally block. The first event we already pulled
                # goes through the same _process_event path as the rest.
                stream_iter = stream_cm._cm_iter  # set by _open_stream_and_first_event

                # First event.
                if _is_terminal_event(first_event):
                    _log_terminal(first_event)
                    yield ProviderChunk(kind="done", finish_reason="error")
                    terminated_early = True
                else:
                    if getattr(first_event, "type", "") == "response.completed":
                        captured_usage = _extract_usage(first_event) or captured_usage
                    async for chunk in _process_event(first_event):
                        if chunk.kind == "tool_call_start":
                            saw_tool_call = True
                        yield chunk

                    # Remaining events.
                    if not terminated_early:
                        async for event in stream_iter:
                            if _is_terminal_event(event):
                                _log_terminal(event)
                                yield ProviderChunk(kind="done", finish_reason="error")
                                terminated_early = True
                                break
                            if getattr(event, "type", "") == "response.completed":
                                # Last-writer-wins: a streamed response only
                                # carries one completed event, but be defensive.
                                captured_usage = _extract_usage(event) or captured_usage
                            async for chunk in _process_event(event):
                                if chunk.kind == "tool_call_start":
                                    saw_tool_call = True
                                yield chunk
            except Exception as exc:
                logger.warning("codex.stream_error", error=str(exc))
                yield ProviderChunk(kind="done", finish_reason="error")
                return
            finally:
                # The original `async with` would have called __aexit__ for
                # us; since we manually entered above, we mirror that here.
                try:
                    await stream_cm.__aexit__(None, None, None)
                except Exception:  # noqa: BLE001 — exit failures must not mask the result
                    pass

            if terminated_early:
                return

            yield ProviderChunk(
                kind="done",
                finish_reason="tool_calls" if saw_tool_call else "stop",
                usage=captured_usage,
            )
        finally:
            # Always release the underlying httpx pool, regardless of which
            # exit path (success, early return, mid-stream error, or
            # generator ``aclose()`` from a cancelled caller) we took.
            # ``client`` is whichever client is live at exit — the
            # token-recovery path closes + rebinds it above, so a single
            # close here covers the recovered client too. Without this
            # every Codex chat call leaks a pool entry — audit R4-D2.
            await _safe_close(client)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _ensure_fresh(self) -> None:
        """Refresh the access token if it is expired or close to expiry.

        Single-flight: serialised through :attr:`_refresh_lock` so two
        concurrent chat streams that both see an expired credential
        don't both POST to ``/oauth/token``. The lock-holder
        re-checks ``is_expired()`` after acquisition — if the racing
        task already refreshed, the second arrival is a no-op.
        """
        # Fast-path: short-circuit on a fresh credential without taking
        # the lock. Safe because writes to ``self._credential`` are
        # only ever done under the lock, so we may see a stale-but-
        # valid token (in which case we proceed) or a freshly-rotated
        # token (in which case the inner check below covers the race).
        if not self._credential.is_expired():
            return
        if not self._credential.refresh_token:
            return  # no refresh_token; try with current token (may still be valid)
        async with self._refresh_lock:
            if not self._credential.is_expired():
                # Another task refreshed while we waited on the lock.
                return
            try:
                refreshed = await refresh_codex_token(
                    refresh_token=self._credential.refresh_token,
                )
                self._credential = refreshed
                persist_codex_credential(refreshed, path=self._credential_path)
                logger.debug("codex.token_refreshed")
            except CodexOAuthRefreshError as exc:
                logger.warning("codex.token_refresh_failed", error=str(exc))
                # Fall through — try the current (possibly expired) token;
                # the upstream will return a 401 if it's truly dead.

    async def _attempt_token_recovery(self) -> bool:
        """Reactive refresh after a server-side ``token_invalidated`` 401.

        Calls :func:`refresh_codex_token` with the current refresh token;
        on success updates :attr:`_credential` and writes the new pair
        back to ``~/.codex/auth.json`` so subsequent process restarts
        see the fresh token. Returns ``True`` iff the refresh + persist
        succeeded. The persist step is best-effort — a write failure
        still leaves the in-memory credential updated so the current
        turn can recover.

        Serialised through the same :attr:`_refresh_lock` as
        :meth:`_ensure_fresh` so a 401 ⇄ ensure_fresh race can't
        double-refresh. The lock-holder snapshots the access token
        on entry and short-circuits if another task already rotated
        it — common when two concurrent streams both hit
        ``token_invalidated`` and queue up at the lock.
        """
        # Snapshot the access token we observed *before* waiting on the
        # lock. If another task already rotated the credential while we
        # waited, the caller can retry with the fresh token without us
        # issuing another HTTP round-trip — the server-side
        # ``token_invalidated`` we hit is for an access token that's
        # already been superseded in-process.
        pre_access = self._credential.access_token
        if not self._credential.refresh_token:
            logger.warning("codex.token_recovery_skipped", reason="no_refresh_token")
            return False
        async with self._refresh_lock:
            if self._credential.access_token != pre_access:
                # Lost the race but won the war — another task refreshed
                # while we waited on the lock. Skip the POST.
                logger.debug("codex.token_recovery_skipped_after_race")
                return True
            current_refresh = self._credential.refresh_token
            if not current_refresh:
                logger.warning(
                    "codex.token_recovery_skipped", reason="no_refresh_token"
                )
                return False
            try:
                refreshed = await refresh_codex_token(
                    refresh_token=current_refresh,
                )
            except CodexOAuthRefreshError as exc:
                logger.warning("codex.token_recovery_failed", error=str(exc))
                return False
            self._credential = refreshed
            persisted = persist_codex_credential(
                refreshed,
                path=self._credential_path,
            )
            logger.info("codex.token_recovered", persisted=persisted)
            return True


__all__ = ["CodexProvider", "_messages_to_responses_input"]
