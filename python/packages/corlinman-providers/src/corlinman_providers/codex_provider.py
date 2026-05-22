"""Codex (ChatGPT subscription) OAuth provider.

Uses the OAuth tokens written by ``codex login`` (the official Codex CLI)
to call the OpenAI API on behalf of the operator's ChatGPT subscription.

Why this works: OpenAI's API at ``api.openai.com/v1/`` accepts both
``sk-…`` API keys and ChatGPT OAuth access tokens (JWT) in the
``Authorization: Bearer <token>`` header.  The ``openai`` Python SDK
sends them identically — we pass the OAuth token as ``api_key``.

Credentials are read from ``~/.codex/auth.json`` (or ``$CODEX_HOME/auth.json``)
on every ``build()`` call, and auto-refreshed in ``chat_stream`` when the
JWT ``exp`` claim is within 5 minutes.

Default model is ``chatgpt-4o-latest``. At auto-injection time the gateway
probes ``/v1/models`` and picks the best available model from a preference
list; ``chatgpt-4o-latest`` is the fallback when the probe fails. Any model
supported by the OpenAI API (``gpt-*``, ``o1-*``, ``o3-*``, ``o4-*``,
``codex-*``, ``chatgpt-*``) is accepted.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, ClassVar, Sequence

import structlog

from corlinman_providers._codex_oauth import (
    CodexOAuthCredential,
    CodexOAuthRefreshError,
    load_codex_credential,
    refresh_codex_token,
)
from corlinman_providers.base import ProviderChunk
from corlinman_providers.openai_provider import OpenAIProvider
from corlinman_providers.specs import ProviderKind, ProviderSpec

logger = structlog.get_logger(__name__)

_DEFAULT_MODEL = "chatgpt-4o-latest"


class CodexProvider(OpenAIProvider):
    """Codex (ChatGPT subscription) OAuth provider.

    Extends :class:`OpenAIProvider` with OAuth-token sourcing from
    ``~/.codex/auth.json`` and auto-refresh before stale tokens cause a
    ``401``.  The upstream API endpoint and wire format are identical to
    the standard OpenAI provider.
    """

    name: ClassVar[str] = "codex"
    kind: ClassVar[ProviderKind] = ProviderKind.CODEX

    #: Default model surfaced to the channels runtime when ``models.default``
    #: is not set in config and Codex is auto-detected.
    DEFAULT_MODEL: ClassVar[str] = _DEFAULT_MODEL

    def __init__(self, *, credential: CodexOAuthCredential) -> None:
        # Pass access_token as api_key — the OpenAI SDK sends
        # ``Authorization: Bearer <api_key>`` which accepts both API keys
        # and OAuth JWTs on the same endpoint.
        super().__init__(api_key=credential.access_token)
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
        return (
            model.startswith(("gpt-", "o1-", "o3-", "o4-", "codex-", "chatgpt-"))
            or model == "gpt-3.5-turbo"
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
        # Delegate to the parent's generator via explicit class dispatch
        # so Python's super()-in-async-generator restriction is avoided.
        async for chunk in OpenAIProvider.chat_stream(
            self,
            model=model,
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            extra=extra,
        ):
            yield chunk

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
            self._api_key = refreshed.access_token
            logger.debug("codex.token_refreshed")
        except CodexOAuthRefreshError as exc:
            logger.warning("codex.token_refresh_failed", error=str(exc))
            # Fall through — try the current (possibly expired) token;
            # the upstream will return a 401 if it's truly dead.


__all__ = ["CodexProvider"]
