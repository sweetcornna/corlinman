"""Codex (ChatGPT subscription) OAuth provider.

Calls https://chatgpt.com/backend-api/codex using the OpenAI Responses API
with Cloudflare bypass headers. This is NOT the standard api.openai.com/v1/
endpoint — using that endpoint with a Codex OAuth token returns 429 quota
errors because ChatGPT subscriptions don't grant OpenAI API credits.

The Codex backend uses the Responses API (/responses), not chat/completions,
and rejects temperature and max_output_tokens parameters.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, ClassVar, Sequence

import structlog

from corlinman_providers._codex_oauth import (
    CodexOAuthCredential,
    CodexOAuthRefreshError,
    codex_cloudflare_headers,
    load_codex_credential,
    refresh_codex_token,
)
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

    def __init__(self, *, credential: CodexOAuthCredential) -> None:
        self._credential = credential

    @classmethod
    def build(cls, spec: ProviderSpec, **_kwargs: Any) -> CodexProvider:
        """Load the Codex credential from ``~/.codex/auth.json`` and build.

        Raises :class:`RuntimeError` when the file is missing or has no
        ``access_token`` — the operator must run ``codex login`` first.
        """
        cred = load_codex_credential()
        if cred is None:
            raise RuntimeError(
                "Codex provider: ~/.codex/auth.json not found or missing tokens. "
                "Run `codex login` to authenticate."
            )
        return cls(credential=cred)

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
    ) -> AsyncIterator[ProviderChunk]:
        await self._ensure_fresh()
        client = self._make_client()

        # Extract system prompt as instructions (Responses API uses "instructions"
        # instead of a system role message).
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
            "reasoning": {"effort": "medium", "summary": "auto"},
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

        # item_id (fc_…) → call_id (call_…). Arg-delta events carry only
        # item_id; the call_id needed for the function_call_output
        # round-trip lives on the output_item.added event.
        call_ids: dict[str, str] = {}
        # call_id → whether any argument delta was streamed for it. Lets
        # us fall back to the full arguments on output_item.done when the
        # backend ships args in one shot instead of streaming fragments.
        args_streamed: dict[str, bool] = {}
        saw_tool_call = False

        try:
            async with client.responses.stream(**kwargs) as stream:
                async for event in stream:
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
                            saw_tool_call = True
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
                    elif event_type in {"response.incomplete", "response.failed"}:
                        resp_obj = getattr(event, "response", None)
                        status = getattr(resp_obj, "status", None) if resp_obj else None
                        logger.warning(
                            "codex.stream_terminated",
                            event_type=event_type,
                            status=status,
                        )
                        yield ProviderChunk(kind="done", finish_reason="error")
                        return
        except Exception as exc:
            logger.warning("codex.stream_error", error=str(exc))
            yield ProviderChunk(kind="done", finish_reason="error")
            return

        yield ProviderChunk(
            kind="done",
            finish_reason="tool_calls" if saw_tool_call else "stop",
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _ensure_fresh(self) -> None:
        """Refresh the access token if it is expired or close to expiry."""
        if not self._credential.is_expired():
            return
        if not self._credential.refresh_token:
            return  # no refresh_token; try with current token (may still be valid)
        try:
            refreshed = await refresh_codex_token(
                refresh_token=self._credential.refresh_token,
            )
            self._credential = refreshed
            logger.debug("codex.token_refreshed")
        except CodexOAuthRefreshError as exc:
            logger.warning("codex.token_refresh_failed", error=str(exc))
            # Fall through — try the current (possibly expired) token;
            # the upstream will return a 401 if it's truly dead.


__all__ = ["CodexProvider", "_messages_to_responses_input"]
