"""``CorlinmanProvider`` Protocol + unified streaming chunk shape.

Every concrete adapter (Anthropic, OpenAI, Google, DeepSeek, Qwen, GLM)
implements the :class:`CorlinmanProvider` Protocol. The agent
``reasoning_loop`` only ever sees this type; vendor SDK differences are
absorbed inside each adapter, vendor errors are normalised to the
:class:`CorlinmanError` hierarchy defined in
:mod:`corlinman_providers.failover`, and vendor streaming shapes are
normalised to the :class:`ProviderChunk` dataclass defined below.

Plan §14 R5 decision: tool calls travel as **OpenAI-standard JSON
``tool_calls``** — the provider adapter emits ``tool_call_start`` once per
new call, streams ``tool_call_delta`` with argument JSON fragments, then a
single ``tool_call_end`` when the call is complete. There is no
``<<<[TOOL_REQUEST]>>>`` text regex anywhere in corlinman.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

import structlog

logger = structlog.get_logger(__name__)


ChunkKind = Literal[
    "token",
    "tool_call_start",
    "tool_call_delta",
    "tool_call_end",
    "done",
]


@dataclass(slots=True)
class ProviderChunk:
    """One streaming event in the unified provider-chunk protocol.

    The agent ``reasoning_loop`` consumes :class:`ProviderChunk` values and
    assembles them into :class:`ServerFrame` events. Fields are optional
    because each ``kind`` only populates a subset:

    ``token``
        :attr:`text` is the incremental text delta. :attr:`is_reasoning`
        is ``True`` when the delta is a reasoning / thinking trace
        (DeepSeek-R1 / QwQ ``delta.reasoning_content``, Anthropic
        ``thinking`` blocks) rather than answer text — the reasoning
        loop renders these as a separate ``reasoning`` block and they
        are never echoed back to the provider on the next round.
    ``tool_call_start``
        :attr:`tool_call_id` and :attr:`tool_name` identify a newly-started
        call. :attr:`arguments_delta` is usually empty; arg JSON arrives in
        subsequent ``tool_call_delta`` chunks.
    ``tool_call_delta``
        :attr:`tool_call_id` identifies which open call this fragment
        belongs to; :attr:`arguments_delta` carries a JSON fragment (the
        concatenation of all deltas for a given call_id is valid JSON).
    ``tool_call_end``
        :attr:`tool_call_id` identifies the call that has finished
        streaming its arguments.
    ``done``
        :attr:`finish_reason` is the normalised terminal reason:
        ``"stop"``, ``"length"``, ``"tool_calls"``, or ``"error"``.
        :attr:`usage` carries vendor token-accounting integers when the
        upstream reports them — ``input_tokens`` and ``output_tokens``
        are the durable cross-vendor keys; ``cached_input_tokens``,
        ``cached_output_tokens``, and ``reasoning_tokens`` are included
        when present. ``None`` when the provider did not report usage
        (e.g. mid-stream errors, retries that bailed pre-completion).
    """

    kind: ChunkKind
    text: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments_delta: str | None = None
    finish_reason: str | None = None
    usage: dict[str, int] | None = None
    is_reasoning: bool = False


class ChatMessage(Protocol):
    """Structural shape of a single chat message passed to a provider."""

    role: str
    content: str


@runtime_checkable
class CorlinmanProvider(Protocol):
    """LLM provider adapter contract.

    Implementations MUST:

    * be safe to share across asyncio tasks (stateless per-call, or guarded
      with an internal lock);
    * raise a subclass of :class:`corlinman_providers.failover.CorlinmanError`
      on any non-success path — never leak vendor SDK exceptions;
    * respect cooperative cancellation (``asyncio.CancelledError`` must
      propagate and close any underlying HTTP stream);
    * yield :class:`ProviderChunk` values — no vendor-shaped objects escape.
    """

    name: str
    """Short provider id, e.g. ``"anthropic"``, ``"deepseek"``."""

    # NOTE: declared as ``def`` (not ``async def``) because implementations
    # are async generator functions (``async def`` + ``yield``), which return
    # an ``AsyncIterator[...]`` directly — not a coroutine producing one.
    # Declaring ``async def`` here would make mypy expect
    # ``Coroutine[Any, Any, AsyncIterator[...]]`` and structurally reject
    # every concrete adapter.
    def chat_stream(
        self,
        *,
        model: str,
        messages: Sequence[ChatMessage],
        tools: Sequence[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> AsyncIterator[ProviderChunk]:
        """Stream a chat completion as a sequence of :class:`ProviderChunk`.

        Yields until the provider signals end-of-stream, then emits a
        terminal ``done`` chunk whose ``finish_reason`` is one of
        ``"stop" | "length" | "tool_calls" | "error"``. Must raise a
        :class:`CorlinmanError` subclass for billing / rate-limit / auth /
        timeout / overload / context-overflow conditions so the Rust
        agent-client can map to the right ``FailoverReason``.
        """
        ...

    async def embed(
        self,
        *,
        model: str,
        inputs: Sequence[str],
        extra: dict[str, Any] | None = None,
    ) -> list[list[float]]:
        """Compute embedding vectors for ``inputs``.

        Returns one ``list[float]`` per input. Dimensionality is provider-
        and model-specific; the caller is responsible for asserting the
        expected dim (default 3072 for the corlinman RAG pipeline).
        """
        ...

    @classmethod
    def supports(cls, model: str) -> bool:
        """Return whether this adapter claims ``model``.

        Used by :mod:`corlinman_providers.registry` to resolve
        ``ModelRedirect.json`` entries. Should be cheap and side-effect free.
        """
        ...

    def supports_tools(self, model: str) -> bool:
        """Return whether ``model`` accepts OpenAI-style ``tools`` schemas.

        Defaults to ``True`` in every concrete adapter — tool support is
        the norm. Adapters fronting tool-less models (small local models
        behind an ``openai_compatible`` gateway, declarative TOML specs
        with ``params.tools = false``) return ``False`` so the servicer
        can skip builtin-tool injection instead of triggering a vendor
        400. Callers treat a *missing* implementation as ``True``
        (``getattr``-degrade) — the method is part of the contract, not a
        hard runtime requirement. Cheap and side-effect free.
        """
        ...
