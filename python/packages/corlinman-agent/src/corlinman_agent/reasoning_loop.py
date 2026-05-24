"""Reasoning loop — drives a chat completion with interleaved tool calls.

Consumes a :class:`ChatStart` descriptor, invokes the provider's
``chat_stream``, and emits events that mirror the gRPC ``ServerFrame``
surface:

* :class:`TokenEvent` for each text delta;
* :class:`ToolCallEvent` for every completed OpenAI-standard tool call
  (``tool_call_start`` → ``tool_call_delta``\\* → ``tool_call_end``);
* :class:`DoneEvent` on normal end-of-stream;
* :class:`ErrorEvent` if the provider blows up.

Plan §14 R5 decision: the legacy ``<<<[TOOL_REQUEST]>>>`` regex protocol
is gone. Providers emit :class:`ProviderChunk` values with a fixed
``kind`` vocabulary (``token`` / ``tool_call_start`` /
``tool_call_delta`` / ``tool_call_end`` / ``done``), and this loop
aggregates the tool-call fragments into one event per call.

Tool execution is **not** performed here. The loop yields
:class:`ToolCallEvent` and — optionally — awaits :class:`ToolResult`
values pushed via :meth:`ReasoningLoop.feed_tool_result` before
appending a ``role="tool"`` message and looping back to the provider for
a follow-up turn. Callers that don't feed results (notably the M2
single-shot path) just receive the initial round and a terminal Done /
Error event; real multi-round execution lands with the plugin runtime in
M3.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class Attachment:
    """Non-text input attached to the trailing user turn.

    Mirrors the Rust ``corlinman_gateway_api::Attachment`` type and the
    proto ``corlinman.v1.Attachment`` message. ``kind`` is one of
    ``"image"``, ``"audio"``, ``"video"``, ``"file"``.

    ``url`` and ``bytes_`` are mutually complementary — channel adapters
    typically populate ``url`` only (the provider downloads or the
    vendor accepts URL-form inputs directly); callers with the payload
    in hand (scheduler, admin imports) populate ``bytes_``. Both-None is
    valid but useless — providers will skip the attachment with a warn.
    """

    kind: str
    url: str | None = None
    bytes_: bytes | None = None
    mime: str | None = None
    file_name: str | None = None


@dataclass(slots=True)
class ChatStart:
    """Minimal descriptor fed to the reasoning loop.

    ``extra`` carries Feature-C provider-specific params (e.g. ``top_p``,
    ``reasoning_effort``, ``safety_settings``) that the servicer computed
    by merging ``[providers.<name>].params`` under
    ``[models.aliases.<alias>].params``. The loop forwards it verbatim to
    :meth:`CorlinmanProvider.chat_stream`.
    """

    model: str
    messages: Sequence[dict[str, Any]]
    tools: Sequence[dict[str, Any]] = field(default_factory=list)
    session_key: str = ""
    temperature: float | None = None
    max_tokens: int | None = None
    attachments: Sequence[Attachment] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class TokenEvent:
    """Token delta emission."""

    text: str
    is_reasoning: bool = False


@dataclass(slots=True)
class ToolCallEvent:
    """Parsed tool-call emission (observed, not executed).

    ``args_json`` is the fully-aggregated JSON argument payload as raw
    bytes (the standard OpenAI ``tool_calls[].function.arguments`` string,
    utf-8 encoded).
    """

    call_id: str
    plugin: str
    tool: str
    args_json: bytes


@dataclass(slots=True)
class DoneEvent:
    """Terminal event; always the last yielded.

    ``usage`` carries the provider's vendor-reported token accounting
    when available (``input_tokens``, ``output_tokens`` plus optional
    cached / reasoning counts). For multi-round turns, the outer
    :class:`DoneEvent` yielded by :meth:`ReasoningLoop.run` reflects the
    **last** round's usage — the per-round usage is consumed inside the
    loop and not re-emitted individually. The servicer's cost meter
    accumulates these on each turn.
    """

    finish_reason: str = "stop"
    usage: dict[str, int] | None = None


@dataclass(slots=True)
class ErrorEvent:
    """Terminal error event."""

    message: str
    reason: str = "unknown"


@dataclass(slots=True)
class ToolResult:
    """Tool-execution result pushed back into the loop by the caller.

    ``content`` is the stringified result payload that becomes the
    ``content`` of the ``role="tool"`` message appended to the chat
    history on the next provider call.
    """

    call_id: str
    content: str
    is_error: bool = False


Event = TokenEvent | ToolCallEvent | DoneEvent | ErrorEvent


# Maximum provider rounds allowed before we short-circuit to avoid runaway
# tool-call loops. A real coding task interleaves many tool calls
# (todo_write updates, file ops, run_shell verification) — 8 was far too
# low and left the agent out of rounds before its final answer. Codex /
# Claude Code style agents need a high ceiling; the doom-loop guard
# (``_is_awaiting_placeholder``) is the real runaway protection.
# Override with ``$CORLINMAN_AGENT_MAX_ROUNDS``.
try:
    _MAX_ROUNDS = max(8, int(os.environ.get("CORLINMAN_AGENT_MAX_ROUNDS", "60")))
except ValueError:
    _MAX_ROUNDS = 60


# Per-tool-result character cap for messages re-fed to the provider on
# the next round. A few unbounded ``run_shell`` / ``read_file`` results
# can blow the model's context window mid-task; the loop keeps every
# result for all subsequent rounds, so the damage is permanent. Cap and
# **freeze**: once truncated, a result is never re-expanded.
#
# We keep a head slice (the prompt / first error / file header) and a
# heavier tail slice (stack traces and the latest exit status live at
# the tail). Override with ``$CORLINMAN_TOOL_RESULT_CAP``.
try:
    _TOOL_RESULT_CAP = max(1_000, int(os.environ.get("CORLINMAN_TOOL_RESULT_CAP", "8000")))
except ValueError:
    _TOOL_RESULT_CAP = 8_000

# Head/tail split for the head+tail truncation strategy. The tail is
# weighted heavier because shell errors and `pytest` failure summaries
# appear at the bottom of the output.
_TOOL_RESULT_HEAD_CHARS = 2_000
_TOOL_RESULT_TAIL_CHARS = 5_000


# T2.3: cap on total estimated context tokens fed into the provider on
# each round. Once exceeded, ``_compact_history`` elides older
# ``role="tool"`` payloads to the literal ``_ELIDED_TOOL_CONTENT``
# sentinel (kept under-budget for natural idempotence). The most-recent
# 3 assistant rounds plus the seed system/user messages stay verbatim.
# Override with ``$CORLINMAN_CONTEXT_BUDGET``; floor mirrors
# ``_TOOL_RESULT_CAP``'s pattern.
try:
    _CONTEXT_BUDGET = max(8_000, int(os.environ.get("CORLINMAN_CONTEXT_BUDGET", "120000")))
except ValueError:
    _CONTEXT_BUDGET = 120_000

# Sentinel string written into elided ``role="tool"`` messages. Keep
# short — it's sub-budget by construction so subsequent
# ``_compact_history`` passes are no-ops.
_ELIDED_TOOL_CONTENT = "[older tool output elided]"

# How many trailing assistant rounds stay verbatim through compaction.
_COMPACT_RECENT_ROUNDS = 3


# Claude-Code-style summarization threshold. When ``_estimate_tokens``
# crosses ``budget * _COMPACT_SUMMARY_THRESHOLD`` we fire a dedicated
# sub-provider call to compress the older messages into a single
# system-message summary. Below that, the cheaper tool-result elision
# fast path runs. Tunable via ``$CORLINMAN_COMPACT_SUMMARY_THRESHOLD``;
# clamped to ``(0.5, 1.0]`` so a misconfigured value can't disable
# elision entirely or fire the heavyweight path on every round.
try:
    _COMPACT_SUMMARY_THRESHOLD = float(
        os.environ.get("CORLINMAN_COMPACT_SUMMARY_THRESHOLD", "0.95")
    )
except ValueError:
    _COMPACT_SUMMARY_THRESHOLD = 0.95
if _COMPACT_SUMMARY_THRESHOLD <= 0.5 or _COMPACT_SUMMARY_THRESHOLD > 1.0:
    _COMPACT_SUMMARY_THRESHOLD = 0.95


# Lower threshold for the cheap elision path. Triggers compaction
# earlier than the original ``before > budget`` cutoff so the model
# never sees a turn that's already over the wire limit. Default 60%
# of budget — well clear of the summary threshold, with enough
# headroom that a single big tool result can't pop us past 95% in one
# step.
_COMPACT_ELIDE_THRESHOLD: float = 0.60


# Cap on the summary call's output. The summary must fit in a single
# system message and must itself stay well under budget so subsequent
# compaction passes treat it as inert. ~1500 tokens is enough room for
# a dense ~400-word paragraph (the prompt below caps the summary at
# 400 words so the actual emission is comfortably below this ceiling).
_COMPACT_SUMMARY_MAX_TOKENS = 1_500


# Prompt that drives the summarization sub-call. Kept verbatim from
# the design doc — Claude-Code-style "preserve the durable facts and
# drop the per-tool churn". Output is one dense paragraph so it
# slots into a single system message and is cheap to re-feed to the
# provider on every subsequent round.
_SUMMARY_PROMPT = (
    "You are compacting a chat agent's conversation history to free up\n"
    "context. Below is the older portion of the conversation. Summarize\n"
    "it preserving:\n"
    "- The user's original task and any explicit goals.\n"
    "- Decisions already made and conclusions reached.\n"
    "- Tool outputs that may still be referenced (URLs found, files\n"
    "  read, key data points).\n"
    "- Any pending work the agent committed to.\n"
    "Drop: per-tool-call boilerplate, redundant retries, internal\n"
    "reasoning that didn't produce decisions.\n"
    "Output: a single dense paragraph of at most ~400 words. No\n"
    "markdown, no headers — just the summary text."
)


def _estimate_chars(messages: Sequence[dict[str, Any]]) -> int:
    """Sum the user-visible character count across ``messages``.

    Pure helper underpinning :func:`_estimate_tokens` and the
    :class:`ReasoningLoop` incremental cache. Accumulates over:

    * string ``content``;
    * multimodal ``content`` parts' ``"text"`` field (non-text parts
      like images / files are ignored — vendor-specific binary metadata
      must not blow the estimate);
    * ``tool_calls[].function.arguments`` JSON strings (the provider
      re-tokenises these on the next round).

    Keeping the char-level total exposed lets the cache add new tails
    and divide-by-4 at retrieval time, which gives exact equality with
    ``_estimate_tokens(messages)`` (rather than the off-by-one errors
    you get from summing per-slice ``chars // 4`` results).
    """
    total_chars = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        total_chars += len(text)
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function")
                if isinstance(fn, dict):
                    args = fn.get("arguments")
                    if isinstance(args, str):
                        total_chars += len(args)
    return total_chars


def _estimate_tokens(messages: Sequence[dict[str, Any]]) -> int:
    """Cheap ``chars // 4`` token estimator over a message list.

    Returns ``_estimate_chars(messages) // 4``. Pure / no-I/O — the
    :func:`_compact_history` budget check calls this every round.
    """
    return _estimate_chars(messages) // 4


async def _compact_history(
    messages: list[dict[str, Any]],
    *,
    budget: int,
    provider: Any = None,
    model: str | None = None,
    fast_path_only: bool = False,
    prev_estimate: int | None = None,
) -> list[dict[str, Any]]:
    """Return a possibly-compacted copy of ``messages`` capped at ``budget`` tokens.

    Two strategies, picked by token pressure:

    1. **Fast path — naive tool-result elision.** Below the summary
       threshold (``budget * _COMPACT_SUMMARY_THRESHOLD``, default 95%
       of budget) we replace older ``role="tool"`` payloads with the
       literal ``_ELIDED_TOOL_CONTENT`` sentinel and keep the seed
       system / first-user / recent-3 rounds verbatim. Cheap, sync, and
       idempotent — once a tool message has been replaced with the
       short sentinel a re-run yields an equal result.

    2. **Slow path — Claude-Code-style summarization.** When pressure
       crosses the threshold we issue a dedicated provider call that
       compresses everything from the seed through the third-from-last
       assistant turn into one dense paragraph. The summary replaces
       the older messages as a synthetic ``role="system"`` block. On
       any provider failure we fall back to the fast path so context
       overflow never bricks the chat.

    Passthrough when ``_estimate_tokens(messages) <= budget`` — returns
    the input list unchanged (callers may rely on identity).

    ``provider`` / ``model`` are required for the slow path; pass the
    SAME provider instance the parent reasoning loop is using so the
    summarization happens against the same vendor / auth context.

    ``fast_path_only`` forces the cheap path regardless of pressure.
    Used by tests + edge cases where a sub-provider call is undesirable.

    ``prev_estimate`` — perf hook for callers (notably
    :class:`ReasoningLoop`) that already track a running token total
    via the incremental cache. When supplied, the initial
    ``_estimate_tokens(messages)`` walk is skipped; the value is used
    as-is for the budget check. Pass ``None`` (the default) to retain
    the original behaviour — compute it here.
    """
    before = prev_estimate if prev_estimate is not None else _estimate_tokens(messages)
    elide_threshold = int(budget * _COMPACT_ELIDE_THRESHOLD)
    if before < elide_threshold:
        # Sub-elide pressure — no compaction needed. Returning the
        # input unchanged preserves caller identity assumptions
        # (existing tests rely on this for the small-message case).
        return messages

    # Locate every assistant index — the recency anchor lives here.
    assistant_indices = [
        i for i, m in enumerate(messages) if m.get("role") == "assistant"
    ]
    if len(assistant_indices) <= _COMPACT_RECENT_ROUNDS:
        # Not enough history to safely elide — nothing in the "older"
        # zone. Return the input unchanged so we don't accidentally
        # strip the leading turns.
        return messages

    # Cutoff: every message at or after this index is verbatim.
    recent_cutoff = assistant_indices[-_COMPACT_RECENT_ROUNDS]

    # Slow path — summarization. Only fire when context is genuinely
    # under pressure (>= threshold * budget) AND we have a provider to
    # call AND the caller hasn't forced fast-only.
    summary_threshold = int(budget * _COMPACT_SUMMARY_THRESHOLD)
    if (
        not fast_path_only
        and provider is not None
        and model
        and before >= summary_threshold
    ):
        try:
            summarized = await _summarize_old_messages(
                messages=messages,
                recent_cutoff=recent_cutoff,
                provider=provider,
                model=model,
            )
        except Exception as exc:  # noqa: BLE001 — degrade to fast path
            logger.warning(
                "agent.context.summarize_failed",
                error=str(exc),
                before=before,
                budget=budget,
            )
            summarized = None
        if summarized is not None:
            after = _estimate_tokens(summarized)
            logger.info(
                "agent.context.summarized",
                before=before,
                after=after,
                dropped=len(messages) - len(summarized),
                summary_chars=_summary_chars(summarized),
            )
            return summarized

    return _compact_history_elide(
        messages=messages,
        recent_cutoff=recent_cutoff,
        before=before,
    )


def _compact_history_elide(
    *,
    messages: list[dict[str, Any]],
    recent_cutoff: int,
    before: int,
) -> list[dict[str, Any]]:
    """Naive tool-result elision — the fast path of :func:`_compact_history`.

    * preserves the **leading system message(s)** (every message at the
      head with ``role="system"``);
    * preserves the **first ``role="user"`` message** (the original task);
    * preserves the **most-recent 3 rounds verbatim**, where a "round"
      ends at a ``role="assistant"`` turn. The simplest correct slice:
      keep everything physically at-or-after the third-from-last
      assistant message;
    * for every other ``role="tool"`` message in the older zone,
      REPLACES its ``content`` with ``_ELIDED_TOOL_CONTENT`` while
      keeping the matching ``tool_call_id``. Removing the tool message
      would orphan the matching assistant ``tool_calls`` entry and
      break the transcript;
    * leaves older ``role="assistant"`` messages alone (their
      ``tool_calls`` shells must remain to match the elided tool
      messages).

    Returns a NEW list of NEW message dicts — callers can mutate the
    result safely without affecting the input.

    Idempotent: once a tool message has been replaced with
    ``_ELIDED_TOOL_CONTENT`` (short, sub-budget by construction),
    re-running this helper yields an equal result.
    """
    # Identify the first role="user" index (the task seed).
    first_user_idx: int | None = None
    for i, m in enumerate(messages):
        if m.get("role") == "user":
            first_user_idx = i
            break

    out: list[dict[str, Any]] = []
    elided_count = 0
    for i, msg in enumerate(messages):
        if i >= recent_cutoff:
            out.append(dict(msg))
            continue
        role = msg.get("role")
        # Preserve seed system + first user message verbatim.
        if role == "system":
            out.append(dict(msg))
            continue
        if first_user_idx is not None and i == first_user_idx:
            out.append(dict(msg))
            continue
        if role == "tool":
            existing = msg.get("content")
            if existing == _ELIDED_TOOL_CONTENT:
                # Already elided — preserve as-is (idempotence path).
                out.append(dict(msg))
                continue
            new_msg = dict(msg)
            new_msg["content"] = _ELIDED_TOOL_CONTENT
            out.append(new_msg)
            elided_count += 1
            continue
        # role="assistant" in the older zone keeps its tool_calls shell
        # so the elided tool messages still match.
        out.append(dict(msg))

    if elided_count:
        after = _estimate_tokens(out)
        logger.info(
            "agent.context.compacted",
            before=before,
            after=after,
            elided=elided_count,
        )
    return out


def _summary_chars(messages: list[dict[str, Any]]) -> int:
    """Return the character count of the synthetic summary system block.

    The compaction logger emits this so an operator can tell at a
    glance how dense the summary came out. Walks for the first system
    message whose content starts with the ``PRIOR CONVERSATION
    SUMMARY:`` marker; falls back to ``0`` if the slow path didn't
    actually emit one (e.g. degraded back to elision).
    """
    for m in messages:
        if m.get("role") != "system":
            continue
        content = m.get("content")
        if isinstance(content, str) and content.startswith(
            "PRIOR CONVERSATION SUMMARY:"
        ):
            return len(content)
    return 0


async def _summarize_old_messages(
    *,
    messages: list[dict[str, Any]],
    recent_cutoff: int,
    provider: Any,
    model: str,
) -> list[dict[str, Any]] | None:
    """Issue a sub-provider call to compress the older portion of ``messages``.

    Returns a new message list:

    * the leading system messages (verbatim, preserved as-is — they
      carry the agent card / coding-agent system prompt);
    * one synthetic ``{"role": "system", "content": f"PRIOR CONVERSATION
      SUMMARY:\\n{summary}\\n..."}`` block;
    * everything at-or-after ``recent_cutoff`` (the last 3 assistant
      rounds), verbatim.

    Returns ``None`` when summarization produced no usable text — the
    caller falls back to the elision path. Raises only the structural
    "no usable provider" path; transport / API failures bubble out so
    :func:`_compact_history` can degrade silently.
    """
    # Split: leading system block, "old" middle, recent tail.
    leading_system: list[dict[str, Any]] = []
    head_end = 0
    for i, m in enumerate(messages):
        if m.get("role") == "system":
            leading_system.append(dict(m))
            head_end = i + 1
            continue
        break
    old_slice = messages[head_end:recent_cutoff]
    if not old_slice:
        # Nothing to summarize — caller should have taken the
        # passthrough branch. Defensive return.
        return None
    recent_slice = [dict(m) for m in messages[recent_cutoff:]]

    # Drive the sub-provider with a single system prompt + the old
    # messages, no tools, capped output. We re-use the SAME provider
    # the parent loop holds so SDK auth / billing / rate-limit accounting
    # stay scoped to the same context.
    summary_messages: list[dict[str, Any]] = [
        {"role": "system", "content": _SUMMARY_PROMPT}
    ]
    # Append the old messages as-is. We coerce ``content`` to a string
    # so multimodal blocks (image_url parts) don't confuse a vendor
    # that doesn't expect them in a tool-less summarization call.
    for m in old_slice:
        out_msg = dict(m)
        content = out_msg.get("content")
        if isinstance(content, list):
            text_parts = [
                str(p.get("text", ""))
                for p in content
                if isinstance(p, dict) and p.get("type") in ("text", "input_text")
            ]
            out_msg["content"] = " ".join(text_parts).strip()
        summary_messages.append(out_msg)

    chunks: list[str] = []
    stream = provider.chat_stream(
        model=model,
        messages=summary_messages,
        tools=None,
        temperature=None,
        max_tokens=_COMPACT_SUMMARY_MAX_TOKENS,
        extra=None,
    )
    async for chunk in stream:
        if chunk.kind == "token" and chunk.text:
            chunks.append(chunk.text)
        elif chunk.kind == "done":
            break
        # tool_call_* chunks are ignored — tools=None should suppress
        # them but a stubborn provider might still emit. Either way
        # we only care about the text summary.

    summary_text = "".join(chunks).strip()
    if not summary_text:
        return None

    synthetic = {
        "role": "system",
        "content": (
            "PRIOR CONVERSATION SUMMARY:\n"
            f"{summary_text}\n\n"
            "The agent should continue from where the recent messages "
            "leave off."
        ),
    }
    return [*leading_system, synthetic, *recent_slice]


def _truncate_tool_result(content: str) -> str:
    """Cap a tool result at ``_TOOL_RESULT_CAP`` chars, keeping head + tail.

    Strings at or below the cap pass through unchanged. Otherwise the
    return value is ``head + notice + tail`` where ``head`` is the first
    ``_TOOL_RESULT_HEAD_CHARS``, ``tail`` is the last
    ``_TOOL_RESULT_TAIL_CHARS``, and ``notice`` is
    ``\\n…[N chars elided]…\\n``. The final length is therefore strictly
    less than the original ``len(content)`` and bounded by
    ``_TOOL_RESULT_HEAD_CHARS + _TOOL_RESULT_TAIL_CHARS + len(notice)``,
    which sits under ``_TOOL_RESULT_CAP`` for the default config.

    This helper is intentionally pure and idempotent — apply it once at
    history-extension time and freeze the result there.
    """
    if not isinstance(content, str):
        # Defensive: the message builder upstream may hand us a list
        # (multimodal content parts). Don't munge those — only string
        # tool results are at risk of blowing the budget.
        return content  # type: ignore[return-value]
    n = len(content)
    if n <= _TOOL_RESULT_CAP:
        return content
    head = content[:_TOOL_RESULT_HEAD_CHARS]
    tail = content[-_TOOL_RESULT_TAIL_CHARS:]
    elided = n - len(head) - len(tail)
    return f"{head}\n…[{elided} chars elided]…\n{tail}"


class ReasoningLoop:
    """Drives one chat turn (or a chain of turns if tool results flow in).

    ``tool_result_timeout`` controls how long :meth:`run` waits for each
    tool result to come back via :meth:`feed_tool_result` before giving up
    and terminating the loop. The default (0.05s) is tuned for the M2
    single-shot path where the servicer does **not** forward tool results
    yet — production wiring in M3 should raise this (5-30s) to accommodate
    real plugin execution.
    """

    def __init__(self, provider: Any, *, tool_result_timeout: float = 0.05) -> None:
        """``provider`` must implement :class:`corlinman_providers.base.CorlinmanProvider`."""
        self._provider = provider
        self._tool_result_timeout = tool_result_timeout
        self._tool_results: asyncio.Queue[ToolResult] = asyncio.Queue()
        self._cancelled = asyncio.Event()
        self._cancel_reason: str = ""
        self._input_closed = asyncio.Event()
        # Mid-turn user supplements (Claude-Code-style). Drained at the
        # top of every round and appended as user messages with the
        # ``[追加上下文]`` prefix so the model recognises them as
        # supplemental instructions rather than the original task.
        # Unbounded — group-chat injections are rare and bounded by the
        # turn duration, so a hard cap risks dropping the user's text
        # silently.
        self._pending_user_messages: asyncio.Queue[str] = asyncio.Queue()
        # Session key carried over from the most recent ``run()`` so
        # ``inject_user_message`` can stamp it on the hook event without
        # the caller having to plumb it through manually.
        self._session_key: str = ""
        # Incremental token-estimate cache. ``_estimate_tokens`` walks
        # the entire message list every call; on a 100-msg conversation
        # over 10 rounds that's hundreds of full walks per turn. We keep
        # a running CHAR total here (not the //4 token count — summing
        # per-slice token counts accumulates rounding error) and divide
        # by 4 at retrieval time so the cached result matches
        # ``_estimate_tokens(messages)`` exactly. Invalidated whenever
        # ``_compact_history`` returns a fresh list (identity change)
        # or the list shrinks / its head changes.
        self._messages_char_total: int = 0
        self._messages_token_seen: int = 0
        # Cheap fingerprint of the FIRST message — detects in-place
        # edits to the seed that don't change ``len(messages)``.
        self._messages_token_head_hash: int = 0

    def feed_tool_result(self, result: ToolResult) -> None:
        """Push a :class:`ToolResult` for consumption by the next round.

        Non-blocking. Intended to be called from the gateway/servicer when a
        ``ClientFrame.tool_result`` arrives while the loop is still running.
        """
        self._tool_results.put_nowait(result)

    def inject_user_message(self, text: str) -> None:
        """Queue a user message to be appended at the start of the next round.

        Non-blocking. Claude-Code-style "supplemental context": while the
        loop is in flight, the user can send another message to the same
        chat and have it absorbed by the running turn rather than serialised
        behind a new RPC. The text lands in :attr:`_pending_user_messages`
        and is drained — verbatim, in arrival order — at the top of every
        round, before the provider call. Empty / whitespace-only strings
        are dropped; a hook event is fired at ``info`` so subscribers can
        observe injections.
        """
        if not text or not text.strip():
            return
        self._pending_user_messages.put_nowait(text)
        logger.info(
            "reasoning_loop.user_injected",
            session=self._session_key,
            preview=text.strip()[:200],
        )

    def cancel(self, reason: str = "user_abort") -> None:
        """Signal the loop to terminate at the next safe point.

        Sets an internal :class:`asyncio.Event` observed by :meth:`run` at
        round boundaries and by :meth:`_collect_results` while waiting for
        tool results. On cancellation the loop emits an :class:`ErrorEvent`
        with ``reason="cancelled"`` and returns. Non-blocking; idempotent.
        """
        if not self._cancelled.is_set():
            self._cancel_reason = reason or "user_abort"
            self._cancelled.set()

    def signal_input_closed(self) -> None:
        """Signal that no more :class:`ToolResult` values will arrive.

        Called by the servicer when the client half of the bidi stream
        closes. Unblocks any in-flight :meth:`_collect_results` wait so
        the loop can terminate promptly with the provider's last
        ``finish_reason`` (typically ``"tool_calls"``). Distinct from
        :meth:`cancel`, which surfaces as an :class:`ErrorEvent`.
        """
        self._input_closed.set()

    def _invalidate_token_cache(self) -> None:
        """Reset the incremental token-estimate cache.

        Called whenever ``_compact_history`` returns a fresh list
        (identity check failure) — the new list may have dropped
        messages, replaced tool payloads with the elision sentinel, or
        injected a synthetic summary system block. Any of those mutates
        the running total in ways the incremental walker can't track,
        so we drop the cache and let the next call re-walk from
        scratch.
        """
        self._messages_char_total = 0
        self._messages_token_seen = 0
        self._messages_token_head_hash = 0

    def messages_total_token_estimate(
        self, messages: Sequence[dict[str, Any]]
    ) -> int:
        """Return the cached running-total token estimate for ``messages``.

        On the steady-state hot path (round N+1 appended new tool
        messages onto round N's list) only the tail slice is re-walked;
        the cached prefix total is reused. Falls back to a full re-walk
        when:

        * the list shrank below ``_messages_token_seen`` (compaction or
          manual replacement);
        * the head fingerprint diverged from the cached value (in-place
          edit of ``messages[0]``);
        * the cache hasn't been seeded yet (``_messages_token_seen ==
          0``).

        Internally tracks the raw character total and divides by 4 on
        return, so the cached value is bit-exact equal to
        ``_estimate_tokens(messages)`` for any sequence of appends —
        no rounding error from summing per-slice token counts.
        """
        n = len(messages)
        # Head fingerprint detects in-place edits to messages[0] that
        # don't change ``len(messages)``. ``repr`` is stable for the
        # dict / list-of-dict content we store in messages.
        head_hash = hash(repr(messages[0])) if n > 0 else 0

        # Cache miss: shrink, head divergence, or no prior seed.
        if (
            self._messages_token_seen == 0
            or n < self._messages_token_seen
            or head_hash != self._messages_token_head_hash
        ):
            chars = _estimate_chars(messages)
            self._messages_char_total = chars
            self._messages_token_seen = n
            self._messages_token_head_hash = head_hash
            return chars // 4

        # Cache hit: walk only the new tail (char count, not tokens —
        # avoids accumulating ``//4`` rounding error across rounds).
        if n > self._messages_token_seen:
            tail = messages[self._messages_token_seen:]
            self._messages_char_total += _estimate_chars(tail)
            self._messages_token_seen = n
        return self._messages_char_total // 4

    async def run(self, start: ChatStart) -> AsyncIterator[Event]:
        """Execute the loop, yielding events until the stream ends."""
        # Stash session_key so ``inject_user_message`` can stamp it on
        # the hook log line without the caller threading it through.
        self._session_key = start.session_key or ""
        messages: list[dict[str, Any]] = _inject_attachments(
            list(start.messages), start.attachments
        )
        rounds = 0
        # T1.4: most-recently-seen provider usage across rounds. The
        # outer DoneEvent carries the LAST round's value (the cost meter
        # is called once per turn and pricing-by-final-round matches the
        # provider's own report).
        last_usage: dict[str, int] | None = None

        while rounds < _MAX_ROUNDS:
            if self._cancelled.is_set():
                yield ErrorEvent(
                    message=self._cancel_reason or "cancelled",
                    reason="cancelled",
                )
                return
            # Claude-Code-style mid-turn supplements: drain anything the
            # user injected since the last round and append as user
            # messages. The ``[追加上下文]`` prefix tells the model these
            # are supplemental instructions, not the original task — so
            # the agent treats them as additional context rather than
            # restarting from scratch.
            messages = _drain_injected_user_messages(
                messages, self._pending_user_messages
            )
            # T2.3: cap context before each provider call. The first
            # pass after a tool round may elide; subsequent passes are
            # idempotent on already-elided history (the sentinel is
            # sub-budget by construction). At extreme pressure the
            # summarization path fires — a sub-provider call that
            # compresses the older portion into one system block.
            #
            # Perf: feed the cached running total in so ``_compact_history``
            # skips its own full-list walk. We retain the identity of
            # ``messages`` across rounds whenever compaction is a no-op,
            # so the cache keeps growing incrementally; only the new
            # tail (the tool messages just appended) needs re-walking.
            prev_estimate = self.messages_total_token_estimate(messages)
            messages_before_compact = messages
            messages = await _compact_history(
                messages,
                budget=_CONTEXT_BUDGET,
                provider=self._provider,
                model=start.model,
                prev_estimate=prev_estimate,
            )
            # Identity check: ``_compact_history`` returns the SAME
            # list when no compaction was needed (passthrough below
            # the elide threshold). If it returned a fresh list, our
            # cached running total no longer corresponds to the new
            # message identities — invalidate so the next round
            # re-seeds from scratch.
            if messages is not messages_before_compact:
                self._invalidate_token_cache()
            rounds += 1
            tool_calls_this_round: list[ToolCallEvent] = []
            finish_reason = "stop"

            try:
                async for event in self._run_one_round(start, messages):
                    if isinstance(event, ToolCallEvent):
                        tool_calls_this_round.append(event)
                        yield event
                    elif isinstance(event, DoneEvent):
                        finish_reason = event.finish_reason
                        if event.usage is not None:
                            last_usage = event.usage
                    elif isinstance(event, ErrorEvent):
                        yield event
                        return
                    else:
                        yield event
            except Exception as exc:
                logger.warning("reasoning_loop.error", error=str(exc))
                reason = getattr(exc, "reason", "unknown")
                yield ErrorEvent(message=str(exc), reason=reason)
                return

            if self._cancelled.is_set():
                yield ErrorEvent(
                    message=self._cancel_reason or "cancelled",
                    reason="cancelled",
                )
                return

            # No tool calls → we're done; emit the terminal Done and exit.
            if not tool_calls_this_round:
                yield DoneEvent(finish_reason=finish_reason, usage=last_usage)
                return

            # Tool calls were emitted. If the caller hasn't wired the
            # feedback channel, we can't make progress; end the loop with
            # the provider's finish_reason (typically "tool_calls") so the
            # gateway sees the terminal frame and the pipeline drains.
            results = await self._collect_results(tool_calls_this_round)
            if self._cancelled.is_set():
                yield ErrorEvent(
                    message=self._cancel_reason or "cancelled",
                    reason="cancelled",
                )
                return
            if results is None:
                yield DoneEvent(finish_reason=finish_reason, usage=last_usage)
                return

            # Otherwise, append an assistant message recording the calls
            # followed by one tool message per result and keep looping.
            messages = _extend_with_tool_round(
                messages, tool_calls_this_round, results
            )
            if any(_is_awaiting_placeholder(r.content) for r in results):
                # Prevent a doom loop: if every result is a placeholder, the
                # next round will ask for the same tool again.
                yield DoneEvent(finish_reason=finish_reason, usage=last_usage)
                return

        # Rounds exhausted — surface a terminal Done with "length" so the
        # caller can tell this wasn't a clean end.
        yield DoneEvent(finish_reason="length", usage=last_usage)

    async def _run_one_round(
        self, start: ChatStart, messages: Sequence[dict[str, Any]]
    ) -> AsyncIterator[Event]:
        """Drive a single provider call, aggregating tool-call fragments."""
        # call_id → (plugin/tool name, args fragments list).
        open_calls: dict[str, list[str]] = {}
        open_names: dict[str, str] = {}
        finish_reason = "stop"

        stream = self._provider.chat_stream(
            model=start.model,
            messages=messages,
            tools=start.tools or None,
            temperature=start.temperature,
            max_tokens=start.max_tokens,
            extra=start.extra or None,
        )
        async for chunk in stream:
            kind = chunk.kind
            if kind == "token" and chunk.text:
                yield TokenEvent(text=chunk.text)
            elif kind == "tool_call_start":
                call_id = chunk.tool_call_id or ""
                if not call_id:
                    continue
                open_calls[call_id] = []
                open_names[call_id] = chunk.tool_name or ""
            elif kind == "tool_call_delta":
                call_id = chunk.tool_call_id or ""
                frag = chunk.arguments_delta or ""
                if call_id in open_calls and frag:
                    open_calls[call_id].append(frag)
            elif kind == "tool_call_end":
                call_id = chunk.tool_call_id or ""
                ev = _finalise_tool_call(call_id, open_calls, open_names)
                if ev is not None:
                    yield ev
            elif kind == "done":
                finish_reason = chunk.finish_reason or "stop"
                # Close any still-open calls the provider forgot to terminate.
                for call_id in list(open_calls.keys()):
                    ev = _finalise_tool_call(call_id, open_calls, open_names)
                    if ev is not None:
                        yield ev
                # T1.4: forward provider-reported token usage onto the
                # per-round DoneEvent. ``run()`` then bubbles the LAST
                # seen value onto the outer terminal Done.
                yield DoneEvent(finish_reason=finish_reason, usage=chunk.usage)
                return
        # Provider closed without an explicit `done` chunk — treat as stop.
        for call_id in list(open_calls.keys()):
            ev = _finalise_tool_call(call_id, open_calls, open_names)
            if ev is not None:
                yield ev
        yield DoneEvent(finish_reason="stop")

    async def _collect_results(
        self, calls: list[ToolCallEvent]
    ) -> list[ToolResult] | None:
        """Wait for one :class:`ToolResult` per emitted call.

        Returns ``None`` if no result arrives within
        ``self._tool_result_timeout`` — the caller isn't wired for the
        feedback cycle and the loop should terminate after the current
        round. Also returns ``None`` if the loop is cancelled while
        waiting; the caller checks :attr:`_cancelled` to distinguish the
        two outcomes.

        Behaviour:

        * **C4 — out-of-order drain**: results pushed for a ``call_id``
          outside the current round's ``needed`` set are not retained for
          a future round. The function drains the queue once on entry
          with non-blocking ``get_nowait()`` calls, keeps entries whose
          ``call_id`` is needed, and drops the rest with a structured
          warning. This prevents stale results from polluting a later
          round.
        * **C3 — input-closed termination**: when the client half of the
          bidi stream closes, :meth:`signal_input_closed` fires. The wait
          loop watches that event in addition to the per-result get and
          cancel, so the loop terminates promptly (under the round's
          tool-result timeout) instead of waiting full
          ``tool_result_timeout`` on a queue that will never be fed.
          When triggered the partial ``got`` dict is dropped (we have no
          way to synthesise the missing tool messages) and ``None`` is
          returned — the outer loop then emits a terminal Done with the
          last ``finish_reason``.
        """
        needed = {ev.call_id for ev in calls}
        got: dict[str, ToolResult] = {}

        # --- C4: one-shot drain of stale queue entries -----------------
        while True:
            try:
                queued = self._tool_results.get_nowait()
            except asyncio.QueueEmpty:
                break
            if queued.call_id in needed and queued.call_id not in got:
                got[queued.call_id] = queued
            else:
                logger.warning(
                    "reasoning_loop.stale_tool_result",
                    call_id=queued.call_id,
                    needed=sorted(needed),
                )

        while needed - got.keys():
            if self._cancelled.is_set():
                return None
            if self._input_closed.is_set():
                # C3: client closed its half — no more results will come.
                # Drop partial state and let the outer loop terminate.
                return None
            get_task = asyncio.ensure_future(self._tool_results.get())
            cancel_task = asyncio.ensure_future(self._cancelled.wait())
            closed_task = asyncio.ensure_future(self._input_closed.wait())
            done, pending = await asyncio.wait(
                {get_task, cancel_task, closed_task},
                timeout=self._tool_result_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            # Await cancellations so they don't leak as "Task was destroyed".
            for t in pending:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t

            if cancel_task in done:
                # Cancelled. If get_task also finished with a result, drop it.
                if get_task in done and not get_task.cancelled():
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        _ = get_task.result()
                return None
            if closed_task in done:
                # C3: input closed. If get_task also produced a result,
                # drop it — we still cannot satisfy the remainder of
                # ``needed`` and synthesising partial tool messages
                # would leave the assistant turn with orphan tool_calls.
                if get_task in done and not get_task.cancelled():
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        _ = get_task.result()
                return None
            if get_task in done:
                try:
                    result = get_task.result()
                except (asyncio.CancelledError, Exception):
                    return None
                if result.call_id in needed and result.call_id not in got:
                    got[result.call_id] = result
                else:
                    logger.warning(
                        "reasoning_loop.stale_tool_result",
                        call_id=result.call_id,
                        needed=sorted(needed),
                    )
                continue
            # Neither completed → timeout; caller isn't wired.
            return None
        return [got[c.call_id] for c in calls]


def _finalise_tool_call(
    call_id: str,
    open_calls: dict[str, list[str]],
    open_names: dict[str, str],
) -> ToolCallEvent | None:
    """Pop a fully-aggregated call out of ``open_calls`` and yield a
    :class:`ToolCallEvent`. Returns ``None`` if ``call_id`` was unknown."""
    if call_id not in open_calls:
        return None
    frags = open_calls.pop(call_id)
    name = open_names.pop(call_id, "")
    joined = "".join(frags).strip() or "{}"
    # If the provider handed us invalid JSON we still forward the raw bytes
    # unchanged — the executor (future) is allowed to decide what to do.
    try:
        json.loads(joined)
    except json.JSONDecodeError:
        logger.warning(
            "reasoning_loop.bad_tool_args", call_id=call_id, raw=joined[:200]
        )
    return ToolCallEvent(
        call_id=call_id,
        # OpenAI tool_calls don't distinguish plugin vs tool — the name is
        # the tool id, and the plugin-to-tool mapping happens at execute
        # time (M3). For now, plugin == tool == function.name.
        plugin=name,
        tool=name,
        args_json=joined.encode("utf-8"),
    )


def _extend_with_tool_round(
    messages: Sequence[dict[str, Any]],
    calls: list[ToolCallEvent],
    results: list[ToolResult],
) -> list[dict[str, Any]]:
    """Return ``messages`` extended with the assistant tool_calls message
    and one ``role="tool"`` message per result."""
    extended: list[dict[str, Any]] = list(messages)
    extended.append(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": c.call_id,
                    "type": "function",
                    "function": {
                        "name": c.tool,
                        "arguments": c.args_json.decode("utf-8"),
                    },
                }
                for c in calls
            ],
        }
    )
    for r in results:
        # T1.1: cap each tool result before it lands in history. The
        # truncation is permanent — on the next round we re-send this
        # exact (already-capped) content, so this "freezes" the result.
        extended.append(
            {
                "role": "tool",
                "tool_call_id": r.call_id,
                "content": _truncate_tool_result(r.content),
            }
        )
    return extended


def _drain_injected_user_messages(
    messages: list[dict[str, Any]],
    queue: asyncio.Queue[str],
) -> list[dict[str, Any]]:
    """Pull every queued mid-turn user supplement and append as user msgs.

    Each drained text becomes one ``{"role": "user", "content": "[追加上下文] " + text}``
    block appended after the existing messages, in arrival order, so the
    next provider call sees the supplemental context immediately before
    the assistant's response.

    Non-blocking (``get_nowait`` loop). Returns the original list when
    nothing was queued — callers may rely on identity. Mutates a new
    list copy on the inject path so the caller can safely reuse the
    input elsewhere.
    """
    drained: list[str] = []
    while True:
        try:
            drained.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    if not drained:
        return messages
    out = list(messages)
    for text in drained:
        out.append({"role": "user", "content": f"[追加上下文] {text}"})
    return out


def _inject_attachments(
    messages: list[dict[str, Any]],
    attachments: Sequence[Attachment],
) -> list[dict[str, Any]]:
    """Merge ``attachments`` into the trailing user turn of ``messages``.

    The returned shape follows the OpenAI multimodal content-parts contract
    (``[{"type": "text", "text": ...}, {"type": "image_url", ...}]``).
    Providers translate this into their own vendor blocks — see
    :mod:`corlinman_providers.anthropic_provider` which maps ``image_url``
    parts to Anthropic's ``{"type": "image", "source": {"type": "url", ...}}``
    shape.

    Strategy:
    * no attachments → return ``messages`` unchanged (zero-cost fast path
      preserves every existing test assumption about plain string content);
    * otherwise find the last ``role="user"`` message; if none exists,
      append a new one carrying an empty text prompt;
    * rewrite that message's ``content`` from ``str`` to a content-parts
      list with the original text first, followed by one part per
      attachment. Non-image attachments are forwarded as
      ``{"type": "file", ...}`` so providers that don't support them can
      log-and-skip in one place instead of every channel adapter
      guessing.

    Only the trailing user turn is rewritten: providers treat earlier
    turns as already-normalised history, and reshaping them would diverge
    from what the provider itself returned on a prior round.
    """
    if not attachments:
        return messages

    # Find the last user turn (the one the current channel message is on).
    target_idx: int | None = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            target_idx = i
            break
    if target_idx is None:
        # No user message yet (degenerate — shouldn't happen on the QQ
        # path, but easier to handle than to crash on). Synthesise one.
        messages.append({"role": "user", "content": ""})
        target_idx = len(messages) - 1

    target = dict(messages[target_idx])
    existing = target.get("content", "")
    parts: list[dict[str, Any]]
    if isinstance(existing, list):
        # Already multi-part (prior round). Preserve and append.
        parts = list(existing)
    else:
        text = str(existing) if existing else ""
        parts = [{"type": "text", "text": text}] if text else []

    for att in attachments:
        part = _attachment_to_content_part(att)
        if part is not None:
            parts.append(part)

    if not parts:
        # Attachments couldn't be represented and original content was
        # empty — fall back to an empty-string placeholder so providers
        # don't reject the turn.
        parts = [{"type": "text", "text": ""}]

    target["content"] = parts
    out = list(messages)
    out[target_idx] = target
    return out


def _attachment_to_content_part(att: Attachment) -> dict[str, Any] | None:
    """Convert an :class:`Attachment` into one OpenAI content part.

    Returns ``None`` when neither ``url`` nor ``bytes_`` is populated
    (useless attachment — drop quietly).
    """
    if att.kind == "image":
        if att.url:
            return {"type": "image_url", "image_url": {"url": att.url}}
        if att.bytes_:
            # base64 data URL; providers that prefer raw bytes unwrap.
            import base64
            mime = att.mime or "image/*"
            b64 = base64.b64encode(att.bytes_).decode("ascii")
            return {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            }
        return None
    # Audio / video / file — not universally supported. Forward as a
    # generic "file" part so providers that DO handle them (future
    # Gemini audio, future Claude file API) can opt in; text-only
    # providers will skip with a warn.
    if not att.url and not att.bytes_:
        return None
    return {
        "type": "file",
        "file": {
            "kind": att.kind,
            "url": att.url,
            "mime": att.mime,
            "file_name": att.file_name,
            # bytes deliberately omitted from the part (providers
            # download from url; in-memory bytes stay on the Attachment
            # for providers that introspect).
        },
    }


def _is_awaiting_placeholder(content: str) -> bool:
    """Detect the gateway's M2 ``awaiting_plugin_runtime`` placeholder.

    Prevents the loop from burning rounds asking for a tool that the
    runtime cannot yet execute.
    """
    if "awaiting_plugin_runtime" not in content:
        return False
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return True
    return isinstance(payload, dict) and payload.get("status") == "awaiting_plugin_runtime"
