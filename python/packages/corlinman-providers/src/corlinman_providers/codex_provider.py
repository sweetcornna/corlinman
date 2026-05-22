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


def _messages_to_responses_input(messages: Sequence[Any]) -> list[dict]:
    """Convert OpenAI chat messages to Responses API input items."""
    result = []
    for msg in messages:
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", None)
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
        if role == "user":
            result.append({
                "role": "user",
                "content": [{"type": "input_text", "text": str(content or "")}],
            })
        elif role == "assistant":
            result.append({
                "role": "assistant",
                "content": [{"type": "output_text", "text": str(content or "")}],
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

        try:
            async with client.responses.stream(**kwargs) as stream:
                async for event in stream:
                    event_type = getattr(event, "type", "")
                    if "output_text.delta" in event_type:
                        delta = getattr(event, "delta", "")
                        if delta:
                            yield ProviderChunk(kind="token", text=delta)
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

        yield ProviderChunk(kind="done", finish_reason="stop")

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
