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
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import structlog

from corlinman_agent.events import (
    BlockStart,
    BlockStop,
    Cancelling,
    EventEmitter,
    EventEnvelope,
    ReasoningDelta,
    TextDelta,
    ToolInputDelta,
    ToolStateCompleted,
    ToolStateRunning,
    TurnComplete,
    TurnErrored,
    TurnStart,
)
from corlinman_agent.events import Event as TypedEvent

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# WP9: Per-model USD cost coefficients ($ per 1M tokens, ~2026 pricing).
# Covers the Claude family; unknown models fall through to zero cost.
# ---------------------------------------------------------------------------

# Each entry is a 4-tuple of $/1M-token rates:
#   (input, output, cache_read, cache_creation)
# Anthropic prices cache writes (cache_creation) at ~1.25x base input;
# cache reads at ~0.1x. Both are charged here so the per-turn USD math
# matches the vendor invoice (gap ``cost-no-usd-math`` + the new-gap
# "USD cost ignores cache_creation").
_MODEL_COSTS: dict[str, tuple[float, float, float, float]] = {
    # (input, output, cache_read, cache_creation) $/1M tokens
    "claude-opus-4": (15.0, 75.0, 1.5, 18.75),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30, 3.75),
    "claude-3-7-sonnet": (3.0, 15.0, 0.30, 3.75),
    "claude-3-5-sonnet": (3.0, 15.0, 0.30, 3.75),
    "claude-3-5-haiku": (0.8, 4.0, 0.08, 1.0),
    "claude-3-haiku": (0.25, 1.25, 0.03, 0.30),
    "claude-haiku-4-5": (0.8, 4.0, 0.08, 1.0),
}

_INTERNAL_CHAT_EXTRA_KEYS: frozenset[str] = frozenset({
    "persona_id",
    "binding",
    "provider_hint",
})
_CODEX_ONLY_CHAT_EXTRA_KEYS: frozenset[str] = frozenset({
    "prompt_cache_key",
})


def _provider_kind_value(provider: Any) -> str:
    kind = getattr(provider, "kind", None)
    value = getattr(kind, "value", kind)
    return str(value or "").lower()


#: ``(provider_name, joined_keys)`` pairs already warned about by
#: :func:`_warn_undeclared_extra_keys` — extra-key passthrough is decided
#: per alias config, not per turn, so one warning per unique combination
#: is signal and one per round is spam. Mirrors the ``_NEWAPI_WARNED``
#: dedupe pattern in ``corlinman_providers.specs``.
_UNDECLARED_EXTRA_WARNED: set[tuple[str, str]] = set()


def _declared_provider_param_properties(
    provider: Any,
) -> dict[str, Any] | None:
    schema_fn = getattr(provider, "params_schema", None)
    if not callable(schema_fn):
        return None
    try:
        schema = schema_fn()
    except Exception:  # noqa: BLE001 — advisory probe must not break chat
        return None
    if not isinstance(schema, dict):
        return None
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return None
    return properties


def _provider_accepts_extra_value(
    properties: dict[str, Any],
    key: str,
    value: Any,
) -> bool:
    prop = properties.get(key)
    if prop is None:
        return False
    enum = prop.get("enum") if isinstance(prop, dict) else None
    return not isinstance(enum, list) or value in enum


def _warn_undeclared_extra_keys(
    provider: Any,
    undeclared: list[str],
) -> None:
    """Warn once per provider+key-set about dropped undeclared extra keys.

    Alias/provider params flow into the vendor SDK call body verbatim
    (``kwargs.update(extra)``), so a typo'd knob (``temprature``) or a
    knob from the wrong vendor family fails silently or 400s downstream.
    The provider adapters publish their accepted surface as a JSON
    Schema via the ``params_schema`` classmethod; adapters without one are
    skipped — no schema, no basis for filtering. Best-effort by design: a
    malformed schema must never break a turn.
    """
    if not undeclared:
        return
    provider_name = str(getattr(provider, "name", "") or "unknown")
    dedupe_key = (provider_name, ",".join(undeclared))
    if dedupe_key in _UNDECLARED_EXTRA_WARNED:
        return
    _UNDECLARED_EXTRA_WARNED.add(dedupe_key)
    logger.warning(
        "provider.extra_keys_undeclared",
        provider=provider_name,
        keys=undeclared,
    )


def _provider_chat_extra(
    provider: Any, extra: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Return the provider-visible subset of ``ChatStart.extra``.

    ``ChatStart.extra`` also carries turn metadata used by the servicer for
    tool dispatch. Keep that metadata on the original start object, but never
    let it become vendor SDK kwargs.
    """
    if not extra:
        return None

    blocked = set(_INTERNAL_CHAT_EXTRA_KEYS)
    if _provider_kind_value(provider) != "codex":
        blocked.update(_CODEX_ONLY_CHAT_EXTRA_KEYS)

    filtered = {key: value for key, value in extra.items() if key not in blocked}
    if filtered:
        properties = _declared_provider_param_properties(provider)
        if properties is not None:
            dropped = sorted(
                key
                for key, value in filtered.items()
                if not _provider_accepts_extra_value(properties, key, value)
            )
            if dropped:
                _warn_undeclared_extra_keys(provider, dropped)
                filtered = {
                    key: value
                    for key, value in filtered.items()
                    if _provider_accepts_extra_value(properties, key, value)
                }
    return filtered or None


def _estimate_turn_cost_usd(model: str, usage: dict[str, int]) -> float:
    """Return estimated USD cost for one provider call.

    Looks up the model prefix in ``_MODEL_COSTS``; falls back to zero for
    unknown models. Charges four token classes — input, output,
    cache-read, and cache-creation (the write that populates a prompt
    cache, priced higher than a plain read). Pure / no-I/O.
    """
    # Match by prefix so minor version suffixes don't break lookups.
    rates: tuple[float, float, float, float] | None = None
    for key, vals in _MODEL_COSTS.items():
        if model.startswith(key) or key in model:
            rates = vals
            break
    if rates is None:
        return 0.0
    in_rate, out_rate, cache_read_rate, cache_create_rate = rates
    input_tok = usage.get("input_tokens", 0)
    output_tok = usage.get("output_tokens", 0)
    cache_read_tok = usage.get("cache_read_input_tokens", 0)
    # Anthropic reports the cache-write count under
    # ``cache_creation_input_tokens``; some shims fold it into a
    # ``cache_creation`` key — honour both.
    cache_create_tok = usage.get(
        "cache_creation_input_tokens", usage.get("cache_creation", 0)
    )
    return (
        input_tok * in_rate / 1_000_000
        + output_tok * out_rate / 1_000_000
        + cache_read_tok * cache_read_rate / 1_000_000
        + cache_create_tok * cache_create_rate / 1_000_000
    )


# ---------------------------------------------------------------------------
# WP7: Retry classifier for the reasoning-loop model call.
# Delegates to the provider error taxonomy defined in corlinman_providers.
# ---------------------------------------------------------------------------


def _loop_retryable(exc: BaseException) -> float | None:
    """Classify a provider exception for loop-level retry.

    Returns a non-negative float (delay hint in seconds, ``0.0`` = use
    exponential backoff) when the error is transient; ``None`` when it is
    permanent and should propagate immediately.

    This classifier is intentionally conservative: only clear HTTP-level
    transient errors are retried so mid-stream duplicates are impossible
    (the provider only enters the retry window before the first chunk).
    """
    from corlinman_providers.failover import (
        ContextOverflowError,
        ModelNotFoundError,
        OverloadedError,
        RateLimitError,
    )

    # Never retry context overflow or model-not-found here — those have
    # dedicated handling (WP10, WP11) upstream of this classifier.
    if isinstance(exc, (ContextOverflowError, ModelNotFoundError)):
        return None
    if isinstance(exc, RateLimitError):
        ms = getattr(exc, "retry_after_ms", None)
        if isinstance(ms, int) and ms > 0:
            return ms / 1000.0
        # The anthropic adapter parses ``anthropic-ratelimit-unified-reset``
        # into ``reset_at_ms`` (an epoch-ms wall-clock instant). Honour it
        # when present — sleep until the reset, clamped to a sane ceiling.
        reset_at_ms = getattr(exc, "reset_at_ms", None)
        if isinstance(reset_at_ms, int) and reset_at_ms > 0:
            delay = (reset_at_ms - time.time_ns() // 1_000_000) / 1000.0
            if delay > 0:
                return min(delay, 60.0)
        return 0.0
    if isinstance(exc, OverloadedError):
        return 0.0
    # Generic HTTP 429/500/502/503/504 in the exception string (openai SDK etc.)
    msg = str(exc)
    for code in ("429", "500", "502", "503", "504"):
        if code in msg:
            return 0.0
    return None


# Cap on `result_summary` in :class:`ToolStateCompleted`. Plan §1.1
# constrains this to <= 4 KB — anything larger gets head/tail truncated
# the same way :func:`_truncate_tool_result` handles the in-history
# payload, but with a tighter ceiling so the live event stream stays
# cheap to serialise per envelope. Full results remain available via the
# `result_json_ref` pointer (W1.2 will land the table).
_RESULT_SUMMARY_CAP: int = 4_000


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
    ``[models.aliases.<alias>].params``. It may also carry internal turn
    metadata used by the servicer; the loop filters those keys before
    calling :meth:`CorlinmanProvider.chat_stream`.
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
    # gap cost-no-usd-math: accumulated per-turn USD cost (input + output
    # + cache_read + cache_creation across every round of THIS turn).
    # ``None`` when the model is unpriced or no usage was reported. The
    # servicer persists this via ``journal.update_turn_cost`` — see the
    # lane-reasoning-loop wire_contract.
    usd_cost: float | None = None


@dataclass(slots=True)
class ErrorEvent:
    """Terminal error event."""

    message: str
    reason: str = "unknown"


@dataclass(slots=True)
class ToolResult:
    """Tool-execution result pushed back into the loop by the caller.

    ``content`` is the result payload that becomes the ``content`` of the
    ``role="tool"`` message appended to the chat history on the next provider
    call.  Two shapes are supported:

    * ``str`` — the plain-text / JSON-envelope path used by all existing tools.
    * ``list[dict[str, Any]]`` — a multimodal content-block list (e.g.
      ``[{"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}]``).
      These are forwarded verbatim to the provider without any truncation so the
      model can see image output inline in the tool-result turn.
    """

    call_id: str
    content: str | list[dict[str, Any]]
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
# Flat fallback budget when neither an operator override nor a
# model-declared context window is available.
_CONTEXT_BUDGET_DEFAULT = 120_000

# Operator override. When ``$CORLINMAN_CONTEXT_BUDGET`` is set it PINS the
# budget for every model (model-aware sizing is skipped); unset → derive
# per-model from the provider's declared context window (see
# :func:`_resolve_context_budget`).
_CONTEXT_BUDGET_ENV_RAW = os.environ.get("CORLINMAN_CONTEXT_BUDGET")
try:
    _CONTEXT_BUDGET_OVERRIDE: int | None = (
        max(8_000, int(_CONTEXT_BUDGET_ENV_RAW))
        if _CONTEXT_BUDGET_ENV_RAW is not None
        else None
    )
except ValueError:
    _CONTEXT_BUDGET_OVERRIDE = None

# Back-compat alias: the resolved flat budget (override if set, else the
# default). Kept because tests and other modules import this symbol; the
# live loop now sizes per-model via :func:`_resolve_context_budget`.
_CONTEXT_BUDGET = _CONTEXT_BUDGET_OVERRIDE or _CONTEXT_BUDGET_DEFAULT

# When deriving the budget from a model's full context window, reserve a
# slice for the response + safety margin (a fraction, capped in absolute
# terms so a 1M window doesn't reserve an absurd amount).
_CONTEXT_OUTPUT_RESERVE_FRACTION = 0.15
_CONTEXT_OUTPUT_RESERVE_CAP = 48_000


def _resolve_context_budget(provider: Any, model: str | None) -> int:
    """Pick the per-round compaction budget (estimated tokens) for ``model``.

    Precedence:

    1. ``$CORLINMAN_CONTEXT_BUDGET`` operator override — pins every model;
    2. the provider's declared context window for ``model`` minus a
       reserved-output margin (model-aware sizing — a 1M-token model no
       longer compacts at a flat 120k, and a 32k model no longer overflows);
    3. the flat :data:`_CONTEXT_BUDGET_DEFAULT`.

    Best-effort and never raises: a provider with no ``context_window``
    accessor, or one returning a non-positive / non-int value, falls
    through to the default.
    """
    if _CONTEXT_BUDGET_OVERRIDE is not None:
        return _CONTEXT_BUDGET_OVERRIDE
    accessor = getattr(provider, "context_window", None)
    if accessor is not None and model:
        try:
            window = accessor(model)
        except Exception:  # noqa: BLE001 — best-effort; never break the loop
            window = None
        if isinstance(window, int) and window > 0:
            reserve = min(
                int(window * _CONTEXT_OUTPUT_RESERVE_FRACTION),
                _CONTEXT_OUTPUT_RESERVE_CAP,
            )
            return max(8_000, window - reserve)
    return _CONTEXT_BUDGET_DEFAULT

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


# Per-non-text-block token charge for the heuristic estimator. An image
# tile costs vendors ~1-1.6k tokens depending on resolution; a forwarded
# file part (PDF / audio handle) is similar order-of-magnitude. The
# estimator charges a flat ~1.5k tokens per image / file block so the
# context budget reflects multimodal payloads instead of treating them
# as free (gap ``chars-div-4-token-estimate``). Expressed in *chars*
# because ``_estimate_chars`` returns chars and the //4 happens once at
# the top of ``_estimate_tokens`` — 1500 tokens ≈ 6000 chars.
_IMAGE_BLOCK_TOKEN_CHARGE = 1_500
_FILE_BLOCK_TOKEN_CHARGE = 1_500
_NONTEXT_BLOCK_CHAR_CHARGE = _IMAGE_BLOCK_TOKEN_CHARGE * 4


def _has_cjk(text: str) -> bool:
    """Return True if ``text`` contains any CJK Unified Ideograph.

    WP20: CJK characters are encoded in 1-4 bytes but each ideograph is
    typically 1-2 LLM tokens, not the ~0.25 tokens/char the ASCII heuristic
    assumes. Detecting CJK presence lets :func:`_estimate_chars` scale up
    the char count so the token budget is not systematically under-estimated
    for Chinese/Japanese/Korean content.

    Only checks the Basic CJK Unified Ideographs block (U+4E00-U+9FFF) —
    the superset of characters that accounts for >99% of everyday CJK text.
    """
    for ch in text:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF:
            return True
    return False


def _cjk_adjusted_chars(text: str) -> int:
    """Return the CJK-adjusted character weight of ``text``.

    WP20: When ``text`` contains CJK ideographs, multiply the raw character
    count by 1.5 (conservative correction — each ideograph is ~1-2 tokens vs.
    the ``chars // 4`` ASCII baseline which would under-count by ~6x).

    For ASCII-only text this returns ``len(text)`` unchanged so existing
    token estimates are not perturbed.
    """
    if not text:
        return 0
    if _has_cjk(text):
        return int(len(text) * 1.5)
    return len(text)


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

    WP20: CJK text is adjusted via :func:`_cjk_adjusted_chars` so that
    Chinese / Japanese / Korean ideographs are weighted ~1.5x heavier than
    the flat ``chars // 4`` ASCII heuristic (a CJK char is 1-2 tokens, not
    0.25 tokens).
    """
    total_chars = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            total_chars += _cjk_adjusted_chars(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        total_chars += _cjk_adjusted_chars(text)
                        continue
                    # Non-text multimodal block (image_url / image / file /
                    # input_image / input_audio). The flat ``chars // 4``
                    # heuristic treats these as free; charge a fixed
                    # per-block weight instead so the context budget
                    # reflects image/file payloads (gap
                    # ``chars-div-4-token-estimate``).
                    ptype = part.get("type")
                    if ptype in (
                        "image_url",
                        "image",
                        "input_image",
                    ):
                        total_chars += _NONTEXT_BLOCK_CHAR_CHARGE
                    elif ptype in ("file", "input_file", "input_audio", "audio"):
                        total_chars += _NONTEXT_BLOCK_CHAR_CHARGE
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function")
                if isinstance(fn, dict):
                    args = fn.get("arguments")
                    if isinstance(args, str):
                        total_chars += _cjk_adjusted_chars(args)
    return total_chars


def _estimate_tokens(messages: Sequence[dict[str, Any]]) -> int:
    """Cheap ``chars // 4`` token estimator over a message list.

    Returns ``_estimate_chars(messages) // 4``. Pure / no-I/O — the
    :func:`_compact_history` budget check calls this every round.

    Multimodal-aware: image / file content blocks are charged a flat
    per-block weight (~1.5k tokens each) by :func:`_estimate_chars`, and
    CJK text is up-weighted ~1.5x — so an image-heavy or Chinese-heavy
    turn no longer reads as near-zero tokens (gap
    ``chars-div-4-token-estimate``).
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


# Tool-result spill threshold. A single result larger than this gets
# written to a temp file and replaced in-history with a short handle +
# head/tail preview, so a runaway ``read_file`` / ``run_shell`` blob
# never sits verbatim in the context window across every subsequent
# round. Distinct from ``_TOOL_RESULT_CAP``: the cap head/tail-truncates
# in-place; the spill offloads the FULL payload to disk and leaves a
# pointer. Override with ``$CORLINMAN_TOOL_RESULT_SPILL``.
try:
    _TOOL_RESULT_SPILL_CAP = max(
        16_000, int(os.environ.get("CORLINMAN_TOOL_RESULT_SPILL", "65536"))
    )
except ValueError:
    _TOOL_RESULT_SPILL_CAP = 65_536

# Per-turn cumulative tool-output budget (chars). Once a single turn's
# tool results cross this, every subsequent oversized result spills
# regardless of its individual size, protecting against many medium
# results that each clear the per-result truncate cap but sum to a
# context blowout. Override with ``$CORLINMAN_TURN_OUTPUT_BUDGET``.
try:
    _TURN_OUTPUT_BUDGET = max(
        50_000, int(os.environ.get("CORLINMAN_TURN_OUTPUT_BUDGET", "400000"))
    )
except ValueError:
    _TURN_OUTPUT_BUDGET = 400_000

# Preview kept inline when a result is spilled to disk.
_SPILL_PREVIEW_HEAD = 1_500
_SPILL_PREVIEW_TAIL = 1_500


def _spill_tool_result(content: str, call_id: str) -> str:
    """Write an oversized ``content`` to a temp file; return a handle + preview.

    Best-effort: on any I/O failure we degrade to the in-place head/tail
    truncation so a disk problem never bricks the turn. The returned
    string carries a ``[tool result spilled to <path>]`` marker plus a
    head/tail preview so the model still sees the gist and can re-read the
    file via a tool if it needs the full payload.
    """
    n = len(content)
    try:
        import tempfile

        fd, path = tempfile.mkstemp(
            prefix=f"corlinman-tool-{call_id[:16]}-", suffix=".txt"
        )
        with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as fh:
            fh.write(content)
    except OSError as exc:
        logger.warning(
            "reasoning_loop.tool_spill_failed", call_id=call_id, error=str(exc)
        )
        return _truncate_tool_result(content)
    head = content[:_SPILL_PREVIEW_HEAD]
    tail = content[-_SPILL_PREVIEW_TAIL:]
    logger.info(
        "reasoning_loop.tool_result_spilled",
        call_id=call_id,
        chars=n,
        path=path,
    )
    return (
        f"[tool result spilled to {path} — {n} chars total; "
        "re-read that file for the full output]\n"
        f"{head}\n…[middle elided]…\n{tail}"
    )


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

    def __init__(
        self,
        provider: Any,
        *,
        tool_result_timeout: float = 0.05,
        event_emitter: EventEmitter | None = None,
        fallback_models: list[str] | None = None,
        hook_runner: Any | None = None,
        autonomous: bool = False,
        turn_token_budget: int | None = None,
    ) -> None:
        """``provider`` must implement :class:`corlinman_providers.base.CorlinmanProvider`.

        ``event_emitter`` is the typed observability sink defined in
        :mod:`corlinman_agent.events`. When ``None`` (the default) the
        loop emits nothing — preserving the legacy yield-only behaviour
        that the M2 channels still consume. When wired the loop tees
        every legacy yield through a corresponding
        :class:`EventEnvelope` so live SSE / journal consumers see the
        same stream the channel adapter sees.

        ``fallback_models`` (WP11) is an ordered list of model ids to try
        when the primary model returns ``ModelNotFoundError`` or a quota
        error. Defaults to ``["claude-sonnet-4-6", "claude-haiku-4-5"]``
        when ``None``.
        """
        self._provider = provider
        self._tool_result_timeout = tool_result_timeout
        self._event_emitter = event_emitter
        # WP11: fallback model chain. Tried in order when the primary model
        # returns ModelNotFoundError or BillingError on the first attempt.
        self._fallback_models: list[str] = fallback_models if fallback_models is not None else [
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
        ]
        # C3: optional hook runner (corlinman_hooks.HookRunner). When
        # present the loop calls ``run_stop(ctx)`` at the no-tool-calls
        # turn-end and honours a returned veto / inject_message. None →
        # the loop no-ops the hook call entirely (defensive: all call
        # sites guard on this).
        self._hook_runner = hook_runner
        # gap loop-token-budget-continuation: when ``autonomous`` is set
        # (scheduled / background runs) and a ``turn_token_budget`` is
        # configured, the loop injects a 'continue' nudge at the
        # no-tool-calls turn-end while the turn is well under budget and
        # progress isn't diminishing. OFF by default — interactive turns
        # must end exactly when the model stops.
        self._autonomous = bool(autonomous)
        self._turn_token_budget = turn_token_budget
        # Number of auto-continue nudges already injected this turn, plus
        # the previous round's reply length for the diminishing-returns
        # check. Reset per ``run()``.
        self._auto_continue_count = 0
        self._auto_continue_last_len = 0
        # WP9: accumulated session cost in USD across all turns.
        self._session_cost_usd: float = 0.0
        # Per-turn correlation id + monotonic sequence counter. Reset at
        # the top of every :meth:`run` invocation.
        self._turn_id: str = ""
        # One-shot caller-pinned id consumed by the next ``run()`` (see
        # :meth:`pin_turn_id`).
        self._pinned_turn_id: str = ""
        self._sequence: int = 0
        # ``time.monotonic_ns()`` reference for elapsed-ms math; the
        # wall-clock ``timestamp_ms`` on each envelope is sourced
        # separately from :func:`time.time_ns`.
        self._turn_started_ns: int = 0
        self._tool_results: asyncio.Queue[ToolResult] = asyncio.Queue()
        self._cancelled = asyncio.Event()
        self._cancel_reason: str = ""
        # Strong references to fire-and-forget tasks (e.g. the best-effort
        # ``Cancelling`` emit scheduled from :meth:`cancel`). Without this
        # the event loop only holds a weak reference and the task can be
        # garbage-collected mid-flight (the R2-003 footgun). Tasks
        # self-remove via ``add_done_callback`` once complete.
        self._pending_tasks: set[asyncio.Task[None]] = set()
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

    @property
    def turn_id(self) -> str:
        """Current turn correlation id (empty before the first ``run()``).

        Read-only window into the per-turn UUID assigned at the top of
        :meth:`run`. The tool dispatcher (W3.1) reads this so its
        :class:`ToolStateRunning` / :class:`ToolStateHeartbeat` /
        :class:`ToolStateCompleted` envelopes correlate with the
        reasoning loop's own ``BlockStart`` / ``BlockStop`` / etc.
        """
        return self._turn_id

    def pin_turn_id(self, turn_id: str) -> None:
        """Pre-assign the correlation id the NEXT :meth:`run` will use.

        One identity per turn, everywhere: the agent servicer journals
        the turn row (``turns`` / ``turn_messages``) under the journal's
        own ``begin_turn()`` id, while the loop's envelopes
        (``turn_events`` / live SSE) used to carry an unrelated
        ``uuid4().hex`` — so "latest turn for this session → its events"
        joins never matched and every replay/catch-up surface read
        empty. Callers that own a journal turn id pin it here before
        driving :meth:`run`; one-shot, consumed by the next run only.
        """
        self._pinned_turn_id = str(turn_id) if turn_id else ""

    @property
    def session_key(self) -> str:
        """Session key carried by the in-flight turn (empty before
        ``run()``). Pair with :attr:`turn_id` for observability emit.
        """
        return self._session_key

    @property
    def session_cost_usd(self) -> float:
        """WP9: Accumulated estimated USD cost across all turns in this session.

        Updated after each provider round that returns usage data. Zero
        when the model is not in ``_MODEL_COSTS`` or no usage has been
        reported yet. Read-only.
        """
        return self._session_cost_usd

    async def _emit(self, event: TypedEvent) -> None:
        """Wrap ``event`` in an :class:`EventEnvelope` and forward it.

        No-op when no emitter is wired (the M2-channels backwards-compat
        path). Increments the monotonic per-turn ``sequence`` on every
        call so the journal / SSE consumer can order strictly.
        """
        emitter = self._event_emitter
        if emitter is None:
            return
        envelope = EventEnvelope(
            turn_id=self._turn_id,
            session_key=self._session_key,
            sequence=self._sequence,
            timestamp_ms=time.time_ns() // 1_000_000,
            event=event,
        )
        self._sequence += 1
        try:
            await emitter.emit(envelope)
        except Exception as exc:  # noqa: BLE001 — observability sink must not break the loop
            logger.warning(
                "reasoning_loop.emitter_error",
                error=str(exc),
                event_type=type(event).__name__,
            )

    def _elapsed_ms(self) -> int:
        """Milliseconds since the current turn began.

        Uses :func:`time.monotonic_ns` so leapseconds / wall-clock skew
        don't surface negative deltas; callers should still treat the
        result as a "best-effort" duration.
        """
        if self._turn_started_ns == 0:
            return 0
        return (time.monotonic_ns() - self._turn_started_ns) // 1_000_000

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

        W3.1: in addition to setting the cancel event, this method
        schedules an immediate :class:`Cancelling` envelope via the
        emitter (if wired) so the SSE / channel adapter spinner can
        flip to ``⏹ 正在取消…`` within milliseconds rather than waiting
        for the next round boundary. Failure to schedule (e.g. no
        running event loop) is swallowed — the legacy
        cancel-at-next-round path still fires the terminal
        :class:`TurnErrored` so consumers never miss the signal.
        """
        if self._cancelled.is_set():
            return
        self._cancel_reason = reason or "user_abort"
        self._cancelled.set()
        # Best-effort immediate Cancelling emit. We need an async-aware
        # path to call ``emitter.emit_event`` — schedule a task on the
        # running loop. ``cancel`` is sync (callable from any thread /
        # context), so we discover the loop ourselves.
        emitter = self._event_emitter
        if emitter is None or not self._turn_id:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop in this thread; the next round boundary
            # will still emit ``TurnErrored(reason='cancelled')`` so the
            # consumer ultimately sees the signal — just not within 50ms.
            return
        try:
            task = loop.create_task(
                emitter.emit_event(
                    self._turn_id,
                    self._session_key,
                    Cancelling(reason=self._cancel_reason),
                ),
                name="reasoning_loop.cancelling_emit",
            )
        except RuntimeError:
            # Loop is closed / closing — same fallback as no-loop above.
            return
        # Hold a strong reference so the task is not GC'd before it runs.
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

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
        # W1.1: reset per-turn correlation state before any emit. A
        # caller-pinned id (the journal turn id) wins so envelopes and
        # journal rows share one identity; see :meth:`pin_turn_id`.
        self._turn_id = self._pinned_turn_id or uuid.uuid4().hex
        self._pinned_turn_id = ""
        self._sequence = 0
        self._turn_started_ns = time.monotonic_ns()
        # Reset per-turn auto-continue accounting (gated; see __init__).
        self._auto_continue_count = 0
        self._auto_continue_last_len = 0
        # W1.1: emit a TurnStart envelope. ``user_text_preview`` /
        # ``system_message_preview`` are cheap excerpts (first 200 chars)
        # so the SSE consumer can render a turn header without pulling
        # the full message list from the journal.
        await self._emit(
            TurnStart(
                model=start.model,
                user_text_preview=_preview_user_text(start.messages),
                system_message_preview=_preview_system_text(start.messages),
            )
        )
        messages: list[dict[str, Any]] = _inject_attachments(
            list(start.messages), start.attachments
        )
        rounds = 0
        # Per-model compaction budget: a 1M-token model no longer
        # compacts at a flat 120k, and a small model no longer overflows.
        # Stable for the turn (provider + model don't change mid-turn).
        context_budget = _resolve_context_budget(self._provider, start.model)
        # T1.4: most-recently-seen provider usage across rounds. The
        # outer DoneEvent carries the LAST round's value (the cost meter
        # is called once per turn and pricing-by-final-round matches the
        # provider's own report).
        last_usage: dict[str, int] | None = None
        # WP9: per-turn accumulated cost (reset each turn so TurnComplete
        # reflects only this turn's cost; _session_cost_usd accumulates across
        # all turns on the instance).
        turn_cost_usd: float = 0.0
        # WP11: effective model — may switch to a fallback on ModelNotFoundError.
        effective_model: str = start.model
        # Cumulative chars of tool output appended to history this turn.
        # Crossing ``_TURN_OUTPUT_BUDGET`` flips subsequent oversized
        # results to spill-to-disk (see ``_extend_with_tool_round``).
        turn_output_spent: int = 0

        while rounds < _MAX_ROUNDS:
            if self._cancelled.is_set():
                await self._emit(
                    TurnErrored(
                        reason="cancelled",
                        message=self._cancel_reason or "cancelled",
                        elapsed_ms=self._elapsed_ms(),
                    )
                )
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
            # gap no-history-dedup: collapse consecutive verbatim
            # user/assistant turns (double-sent prompts, echoed turns)
            # before the budget check, preserving tool_call/tool_result
            # pairing. Identity-preserving when nothing is dropped, so
            # the token cache stays valid in the common case.
            messages_before_dedup = messages
            messages = _dedup_consecutive_turns(messages)
            if messages is not messages_before_dedup:
                self._invalidate_token_cache()
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
                budget=context_budget,
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

            # WP10/WP11: allow one context-overflow shrink-and-retry and one
            # model-fallback retry per round. These flags ensure we never loop
            # infinitely on edge cases.
            _overflow_retried = False
            _fallback_idx = 0  # next index into self._fallback_models

            # WP7/WP10/WP11: retry state for this round.
            # Retry is only attempted BEFORE any event has been yielded;
            # once we start streaming tokens we cannot retry without
            # duplicating output, so any mid-stream exception falls
            # through to the normal error path.
            _retry_attempt = 0
            _max_retry_attempts = 3
            _streaming_started = False

            while True:
                # WP11: rebuild ChatStart with the effective model (may have
                # changed via fallback on a prior pass of this inner while).
                _round_start = start if effective_model == start.model else (
                    ChatStart(
                        model=effective_model,
                        messages=start.messages,
                        tools=start.tools,
                        session_key=start.session_key,
                        temperature=start.temperature,
                        max_tokens=start.max_tokens,
                        attachments=start.attachments,
                        extra=start.extra,
                    )
                )
                try:
                    async for event in self._run_one_round(_round_start, messages):
                        # Once the first event arrives, mark streaming started.
                        # Any subsequent exception cannot be retried safely.
                        _streaming_started = True
                        if isinstance(event, ToolCallEvent):
                            tool_calls_this_round.append(event)
                            yield event
                        elif isinstance(event, DoneEvent):
                            finish_reason = event.finish_reason
                            if event.usage is not None:
                                last_usage = event.usage
                                # WP9: accumulate cost on every round with usage.
                                _round_cost = _estimate_turn_cost_usd(
                                    effective_model, event.usage
                                )
                                turn_cost_usd += _round_cost
                                self._session_cost_usd += _round_cost
                        elif isinstance(event, ErrorEvent):
                            await self._emit(
                                TurnErrored(
                                    reason=event.reason,
                                    message=event.message,
                                    elapsed_ms=self._elapsed_ms(),
                                )
                            )
                            yield event
                            return
                        else:
                            yield event
                    break  # round completed successfully

                except Exception as exc:
                    from corlinman_providers.failover import (
                        BillingError,
                        ContextOverflowError,
                        ModelNotFoundError,
                        OverloadedError,
                    )

                    # WP10: context overflow → shrink and retry once
                    # (only safe before streaming started).
                    _overflow = (
                        isinstance(exc, ContextOverflowError)
                        or "context_length_exceeded" in str(exc)
                        or "context window" in str(exc).lower()
                        or "maximum context" in str(exc).lower()
                    )
                    if _overflow and not _overflow_retried and not _streaming_started:
                        _overflow_retried = True
                        # When the provider tells us the actual window
                        # (``ContextOverflowError.limit``), size the new
                        # budget as ``limit - estimated_input - buffer`` so
                        # the retry is just under the real ceiling instead
                        # of a blind 20% haircut. Fall back to the haircut
                        # when no limit is reported.
                        _limit = getattr(exc, "limit", None)
                        if isinstance(_limit, int) and _limit > 0:
                            _input_est = self.messages_total_token_estimate(messages)
                            _buffer = max(
                                int(_limit * _CONTEXT_OUTPUT_RESERVE_FRACTION),
                                2_000,
                            )
                            tighter_budget = max(8_000, _limit - _buffer)
                            # Guard against a limit that's already below our
                            # estimate doing nothing — also apply the 20%
                            # haircut floor so we always shrink.
                            tighter_budget = min(
                                tighter_budget, int(context_budget * 0.80)
                            )
                        else:
                            tighter_budget = int(context_budget * 0.80)
                        logger.warning(
                            "reasoning_loop.context_overflow_shrink",
                            original_budget=context_budget,
                            tighter_budget=tighter_budget,
                            reported_limit=_limit,
                            model=effective_model,
                        )
                        context_budget = tighter_budget
                        messages = await _compact_history(
                            messages,
                            budget=context_budget,
                            provider=self._provider,
                            model=effective_model,
                            fast_path_only=True,
                        )
                        self._invalidate_token_cache()
                        tool_calls_this_round = []
                        _streaming_started = False
                        continue  # retry with smaller context

                    # WP11: model not found / quota → try next fallback model
                    # (only safe before streaming started).
                    _model_fail = isinstance(exc, (ModelNotFoundError, BillingError)) or (
                        "model_not_found" in str(exc)
                        or "insufficient_quota" in str(exc)
                    )
                    if (
                        _model_fail
                        and not _streaming_started
                        and _fallback_idx < len(self._fallback_models)
                    ):
                        _old_model = effective_model
                        effective_model = self._fallback_models[_fallback_idx]
                        _fallback_idx += 1
                        logger.warning(
                            "reasoning_loop.model_fallback",
                            from_model=_old_model,
                            to_model=effective_model,
                            error=str(exc),
                        )
                        tool_calls_this_round = []
                        _streaming_started = False
                        continue  # retry with fallback model

                    # WP7: transient 429/5xx retry before streaming started.
                    _delay = _loop_retryable(exc)
                    if (
                        _delay is not None
                        and not _streaming_started
                        and _retry_attempt < _max_retry_attempts - 1
                    ):
                        _retry_attempt += 1
                        _backoff = (
                            _delay if _delay > 0
                            else min(0.5 * float(2 ** (_retry_attempt - 1)), 16.0)
                        )
                        logger.warning(
                            "reasoning_loop.retry",
                            attempt=_retry_attempt,
                            delay=_backoff,
                            error=str(exc),
                            model=effective_model,
                        )
                        await asyncio.sleep(_backoff)
                        tool_calls_this_round = []
                        continue  # retry after backoff

                    # gap loop-cross-model-fallback / no-model-fallback-chain:
                    # SUSTAINED overload — the WP7 retry budget above is
                    # exhausted (``_retry_attempt`` has reached its cap) and
                    # the error is still an overload / sustained 429/529. If
                    # a fallback model is configured, swap to it and replay
                    # the turn (bounded by the chain length). Only safe
                    # before streaming started.
                    _sustained_overload = (
                        isinstance(exc, OverloadedError)
                        or "overloaded" in str(exc).lower()
                        or "529" in str(exc)
                    )
                    if (
                        _sustained_overload
                        and not _streaming_started
                        and _fallback_idx < len(self._fallback_models)
                    ):
                        _old_model = effective_model
                        effective_model = self._fallback_models[_fallback_idx]
                        _fallback_idx += 1
                        # Reset the retry budget so the fallback model gets a
                        # fresh set of transient retries before it too is
                        # escalated to the next link in the chain.
                        _retry_attempt = 0
                        logger.warning(
                            "reasoning_loop.model_fallback",
                            from_model=_old_model,
                            to_model=effective_model,
                            error=str(exc),
                            cause="sustained_overload",
                        )
                        tool_calls_this_round = []
                        _streaming_started = False
                        continue  # replay turn on fallback model

                    # Unrecoverable — surface the error.
                    logger.warning("reasoning_loop.error", error=str(exc))
                    reason = getattr(exc, "reason", "unknown")
                    await self._emit(
                        TurnErrored(
                            reason=reason,
                            message=str(exc),
                            elapsed_ms=self._elapsed_ms(),
                        )
                    )
                    yield ErrorEvent(message=str(exc), reason=reason)
                    return

            if self._cancelled.is_set():
                await self._emit(
                    TurnErrored(
                        reason="cancelled",
                        message=self._cancel_reason or "cancelled",
                        elapsed_ms=self._elapsed_ms(),
                    )
                )
                yield ErrorEvent(
                    message=self._cancel_reason or "cancelled",
                    reason="cancelled",
                )
                return

            # No tool calls → we're done; emit the terminal Done and exit.
            if not tool_calls_this_round:
                # C3: give a Stop hook the chance to veto turn-end and/or
                # inject a follow-up message. Defensive — no-ops entirely
                # when no hook runner is wired. A returned ``stop=True``
                # forces the turn to end even if the hook also injected;
                # otherwise an ``inject_message`` re-opens the loop.
                _stop_inject = await self._maybe_run_stop_hook(finish_reason)
                if _stop_inject is not None:
                    messages = _append_user_turn(messages, _stop_inject)
                    self._invalidate_token_cache()
                    continue  # re-run with the injected follow-up

                # gap loop-token-budget-continuation: optional, gated
                # auto-continue for autonomous/scheduled runs. Injects a
                # 'continue' nudge while the turn is well under budget and
                # progress isn't diminishing. No-op for interactive turns.
                _nudge = self._maybe_auto_continue(messages, last_usage)
                if _nudge is not None:
                    messages = _append_user_turn(messages, _nudge)
                    self._invalidate_token_cache()
                    continue  # re-run with the auto-continue nudge

                await self._emit(
                    TurnComplete(
                        finish_reason=finish_reason,
                        usage=last_usage or {},
                        elapsed_ms=self._elapsed_ms(),
                        estimated_cost_usd=turn_cost_usd if turn_cost_usd > 0 else None,
                        cost_status="estimated" if turn_cost_usd > 0 else None,
                    )
                )
                yield DoneEvent(
                    finish_reason=finish_reason,
                    usage=last_usage,
                    usd_cost=turn_cost_usd if turn_cost_usd > 0 else None,
                )
                return

            # W1.1: emit ToolStateRunning for each pending call BEFORE
            # awaiting results. The runner pool (W3.1) may re-emit a
            # richer ToolStateRunning at dispatch time — for now this
            # marks the moment the model finished assembling args. The
            # ``started_at_ms`` is wall-clock so SSE consumers can show
            # absolute timestamps.
            wallclock_ms = time.time_ns() // 1_000_000
            for call in tool_calls_this_round:
                await self._emit(
                    ToolStateRunning(
                        tool_call_id=call.call_id,
                        tool_name=call.tool,
                        args_json=call.args_json.decode("utf-8", errors="replace"),
                        started_at_ms=wallclock_ms,
                    )
                )
            tool_started_ns = time.monotonic_ns()

            # Tool calls were emitted. If the caller hasn't wired the
            # feedback channel, we can't make progress; end the loop with
            # the provider's finish_reason (typically "tool_calls") so the
            # gateway sees the terminal frame and the pipeline drains.
            results = await self._collect_results(tool_calls_this_round)
            if self._cancelled.is_set():
                await self._emit(
                    TurnErrored(
                        reason="cancelled",
                        message=self._cancel_reason or "cancelled",
                        elapsed_ms=self._elapsed_ms(),
                    )
                )
                yield ErrorEvent(
                    message=self._cancel_reason or "cancelled",
                    reason="cancelled",
                )
                return
            if results is None:
                await self._emit(
                    TurnComplete(
                        finish_reason=finish_reason,
                        usage=last_usage or {},
                        elapsed_ms=self._elapsed_ms(),
                        estimated_cost_usd=turn_cost_usd if turn_cost_usd > 0 else None,
                        cost_status="estimated" if turn_cost_usd > 0 else None,
                    )
                )
                yield DoneEvent(
                    finish_reason=finish_reason,
                    usage=last_usage,
                    usd_cost=turn_cost_usd if turn_cost_usd > 0 else None,
                )
                return

            # W1.1: emit ToolStateCompleted for each result. We treat
            # all results sharing a single ``tool_started_ns`` for
            # ``elapsed_ms``; per-call timing belongs to W3.1 (the
            # runner pool knows per-tool dispatch start).
            tool_elapsed_ms = (time.monotonic_ns() - tool_started_ns) // 1_000_000
            for result in results:
                summary = result.content
                if isinstance(summary, str) and len(summary) > _RESULT_SUMMARY_CAP:
                    head = summary[: _RESULT_SUMMARY_CAP // 2]
                    tail = summary[-(_RESULT_SUMMARY_CAP // 2):]
                    summary = f"{head}\n…[truncated]…\n{tail}"
                await self._emit(
                    ToolStateCompleted(
                        tool_call_id=result.call_id,
                        result_summary=summary if isinstance(summary, str) else "",
                        elapsed_ms=tool_elapsed_ms,
                        is_error=result.is_error,
                    )
                )

            # Otherwise, append an assistant message recording the calls
            # followed by one tool message per result and keep looping.
            # Thread the per-turn tool-output budget so very large results
            # spill to disk once the turn's cumulative output is high.
            messages = _extend_with_tool_round(
                messages,
                tool_calls_this_round,
                results,
                turn_output_spent=turn_output_spent,
            )
            # Track the per-turn cumulative tool-output chars (string
            # results only — multimodal lists are forwarded verbatim).
            turn_output_spent += sum(
                len(r.content) for r in results if isinstance(r.content, str)
            )
            if any(
                isinstance(r.content, str) and _is_awaiting_placeholder(r.content)
                for r in results
            ):
                # Prevent a doom loop: if every result is a placeholder, the
                # next round will ask for the same tool again.
                await self._emit(
                    TurnComplete(
                        finish_reason=finish_reason,
                        usage=last_usage or {},
                        elapsed_ms=self._elapsed_ms(),
                        estimated_cost_usd=turn_cost_usd if turn_cost_usd > 0 else None,
                        cost_status="estimated" if turn_cost_usd > 0 else None,
                    )
                )
                yield DoneEvent(
                    finish_reason=finish_reason,
                    usage=last_usage,
                    usd_cost=turn_cost_usd if turn_cost_usd > 0 else None,
                )
                return

        # Rounds exhausted — surface a terminal Done with "length" so the
        # caller can tell this wasn't a clean end.
        await self._emit(
            TurnComplete(
                finish_reason="length",
                usage=last_usage or {},
                elapsed_ms=self._elapsed_ms(),
                estimated_cost_usd=turn_cost_usd if turn_cost_usd > 0 else None,
                cost_status="estimated" if turn_cost_usd > 0 else None,
            )
        )
        yield DoneEvent(
            finish_reason="length",
            usage=last_usage,
            usd_cost=turn_cost_usd if turn_cost_usd > 0 else None,
        )

    async def _run_one_round(
        self, start: ChatStart, messages: Sequence[dict[str, Any]]
    ) -> AsyncIterator[Event]:
        """Drive a single provider call, aggregating tool-call fragments.

        W1.1: in addition to the legacy ``TokenEvent`` / ``ToolCallEvent``
        / ``DoneEvent`` yields (kept for backwards-compat with the M2
        channel adapters), this method now tees a typed
        :class:`EventEnvelope` stream through ``self._event_emitter``.

        Block accounting:

        * the first ``token`` chunk opens a ``text`` block — subsequent
          tokens stream as :class:`TextDelta`; a non-text chunk closes
          it with :class:`BlockStop`;
        * each ``tool_call_start`` chunk opens a ``tool_use`` block —
          ``tool_call_delta`` chunks stream as :class:`ToolInputDelta`;
          ``tool_call_end`` closes it;
        * any chunk with ``is_reasoning=True`` (duck-typed — providers
          opt in by setting the attribute on :class:`ProviderChunk`)
          opens / extends a ``reasoning`` block. Reasoning blocks live
          on their own ``index`` distinct from the text/tool indices so
          the UI can render them as a separate widget.

        ``index`` is a monotonic counter across blocks within the round.
        """
        # call_id → (plugin/tool name, args fragments list).
        open_calls: dict[str, list[str]] = {}
        open_names: dict[str, str] = {}
        # call_id → block index (for the ``tool_use`` block we opened).
        tool_block_index: dict[str, int] = {}
        # call_id → ``time.monotonic_ns`` at BlockStart, for ``elapsed_ms``
        # on the BlockStop.
        tool_block_started_ns: dict[str, int] = {}
        finish_reason = "stop"

        # Block accounting for the current round.
        next_block_index: int = 0
        # The open text/reasoning block (if any). Tool blocks are tracked
        # per-call_id in ``tool_block_index`` because the provider can
        # interleave multiple parallel tool calls.
        open_text_index: int | None = None
        open_text_started_ns: int = 0
        open_text_cumulative: int = 0
        open_reasoning_index: int | None = None
        open_reasoning_started_ns: int = 0

        async def _close_text_block() -> None:
            nonlocal open_text_index, open_text_started_ns, open_text_cumulative
            if open_text_index is None:
                return
            elapsed_ms = (
                time.monotonic_ns() - open_text_started_ns
            ) // 1_000_000
            await self._emit(BlockStop(index=open_text_index, elapsed_ms=elapsed_ms))
            open_text_index = None
            open_text_started_ns = 0
            open_text_cumulative = 0

        async def _close_reasoning_block() -> None:
            nonlocal open_reasoning_index, open_reasoning_started_ns
            if open_reasoning_index is None:
                return
            elapsed_ms = (
                time.monotonic_ns() - open_reasoning_started_ns
            ) // 1_000_000
            await self._emit(
                BlockStop(index=open_reasoning_index, elapsed_ms=elapsed_ms)
            )
            open_reasoning_index = None
            open_reasoning_started_ns = 0

        stream = self._provider.chat_stream(
            model=start.model,
            messages=messages,
            tools=start.tools or None,
            temperature=start.temperature,
            max_tokens=start.max_tokens,
            extra=_provider_chat_extra(self._provider, start.extra),
        )
        async for chunk in stream:
            kind = chunk.kind
            # Reasoning is duck-typed on the chunk: when a provider
            # adapter opts in, it sets ``is_reasoning=True`` on the
            # ``token`` chunk (and optionally a ``signature`` attribute
            # for Anthropic-style attestation). Pull both via getattr so
            # the existing ProviderChunk Literal type stays untouched.
            is_reasoning = bool(getattr(chunk, "is_reasoning", False))
            reasoning_signature: str | None = getattr(chunk, "signature", None)
            if kind == "token" and chunk.text:
                if is_reasoning:
                    # Switch into reasoning block — close any open text block.
                    if open_text_index is not None:
                        await _close_text_block()
                    if open_reasoning_index is None:
                        open_reasoning_index = next_block_index
                        next_block_index += 1
                        open_reasoning_started_ns = time.monotonic_ns()
                        await self._emit(
                            BlockStart(
                                index=open_reasoning_index,
                                block_type="reasoning",
                            )
                        )
                    await self._emit(
                        ReasoningDelta(
                            index=open_reasoning_index,
                            text=chunk.text,
                            signature=reasoning_signature,
                        )
                    )
                    yield TokenEvent(text=chunk.text, is_reasoning=True)
                    continue
                # Plain text token — close any open reasoning block first.
                if open_reasoning_index is not None:
                    await _close_reasoning_block()
                if open_text_index is None:
                    open_text_index = next_block_index
                    next_block_index += 1
                    open_text_started_ns = time.monotonic_ns()
                    await self._emit(
                        BlockStart(
                            index=open_text_index,
                            block_type="text",
                        )
                    )
                open_text_cumulative += len(chunk.text)
                await self._emit(
                    TextDelta(
                        index=open_text_index,
                        text=chunk.text,
                        cumulative_len=open_text_cumulative,
                    )
                )
                yield TokenEvent(text=chunk.text)
            elif kind == "tool_call_start":
                call_id = chunk.tool_call_id or ""
                if not call_id:
                    continue
                # A tool block opening closes any in-flight text /
                # reasoning block (the model switched mode).
                if open_text_index is not None:
                    await _close_text_block()
                if open_reasoning_index is not None:
                    await _close_reasoning_block()
                open_calls[call_id] = []
                open_names[call_id] = chunk.tool_name or ""
                idx = next_block_index
                next_block_index += 1
                tool_block_index[call_id] = idx
                tool_block_started_ns[call_id] = time.monotonic_ns()
                await self._emit(
                    BlockStart(
                        index=idx,
                        block_type="tool_use",
                        tool_name=chunk.tool_name or "",
                        tool_call_id=call_id,
                    )
                )
            elif kind == "tool_call_delta":
                call_id = chunk.tool_call_id or ""
                frag = chunk.arguments_delta or ""
                if call_id in open_calls and frag:
                    open_calls[call_id].append(frag)
                    delta_idx: int | None = tool_block_index.get(call_id)
                    if delta_idx is not None:
                        await self._emit(
                            ToolInputDelta(index=delta_idx, partial_json=frag)
                        )
            elif kind == "tool_call_end":
                call_id = chunk.tool_call_id or ""
                ev = _finalise_tool_call(call_id, open_calls, open_names)
                if ev is not None:
                    end_idx: int | None = tool_block_index.pop(call_id, None)
                    end_started: int = tool_block_started_ns.pop(call_id, 0)
                    if end_idx is not None:
                        elapsed_ms = (
                            time.monotonic_ns() - end_started
                        ) // 1_000_000
                        await self._emit(
                            BlockStop(index=end_idx, elapsed_ms=elapsed_ms)
                        )
                    yield ev
            elif kind == "done":
                finish_reason = chunk.finish_reason or "stop"
                # Close any still-open text / reasoning blocks first so
                # the BlockStop precedes the final tool_use stops below.
                if open_text_index is not None:
                    await _close_text_block()
                if open_reasoning_index is not None:
                    await _close_reasoning_block()
                # Close any still-open calls the provider forgot to terminate.
                for call_id in list(open_calls.keys()):
                    ev = _finalise_tool_call(call_id, open_calls, open_names)
                    if ev is not None:
                        done_idx: int | None = tool_block_index.pop(
                            call_id, None
                        )
                        done_started: int = tool_block_started_ns.pop(
                            call_id, 0
                        )
                        if done_idx is not None:
                            elapsed_ms = (
                                time.monotonic_ns() - done_started
                            ) // 1_000_000
                            await self._emit(
                                BlockStop(
                                    index=done_idx, elapsed_ms=elapsed_ms
                                )
                            )
                        yield ev
                # T1.4: forward provider-reported token usage onto the
                # per-round DoneEvent. ``run()`` then bubbles the LAST
                # seen value onto the outer terminal Done.
                yield DoneEvent(finish_reason=finish_reason, usage=chunk.usage)
                return
        # Provider closed without an explicit `done` chunk — treat as stop.
        if open_text_index is not None:
            await _close_text_block()
        if open_reasoning_index is not None:
            await _close_reasoning_block()
        for call_id in list(open_calls.keys()):
            ev = _finalise_tool_call(call_id, open_calls, open_names)
            if ev is not None:
                tail_idx: int | None = tool_block_index.pop(call_id, None)
                tail_started: int = tool_block_started_ns.pop(call_id, 0)
                if tail_idx is not None:
                    elapsed_ms = (
                        time.monotonic_ns() - tail_started
                    ) // 1_000_000
                    await self._emit(
                        BlockStop(index=tail_idx, elapsed_ms=elapsed_ms)
                    )
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

    async def _maybe_run_stop_hook(self, finish_reason: str) -> str | None:
        """C3: invoke the Stop hook at turn-end, if a hook runner is wired.

        Returns the message to inject (re-opening the loop) when the hook
        either vetoes the stop or supplies an ``inject_message`` — and the
        hook did NOT also force ``stop=True``. Returns ``None`` to let the
        turn end normally.

        Defensive on every axis: no hook runner → ``None``; a hook runner
        without a ``run_stop`` coroutine → ``None``; any exception inside
        the hook → logged and treated as ``None`` so a broken hook can
        never wedge the loop.
        """
        runner = getattr(self, "_hook_runner", None)
        if runner is None:
            return None
        run_stop = getattr(runner, "run_stop", None)
        if run_stop is None:
            return None
        ctx = {
            "session_key": self._session_key,
            "turn_id": self._turn_id,
            "finish_reason": finish_reason,
        }
        try:
            decision = run_stop(ctx)
            if asyncio.iscoroutine(decision):
                decision = await decision
        except Exception as exc:  # noqa: BLE001 — hook must never break the loop
            logger.warning("reasoning_loop.stop_hook_error", error=str(exc))
            return None
        if decision is None:
            return None
        # ``stop=True`` is an explicit "end now" — honour it even if a
        # message was also supplied (the message is dropped).
        if bool(getattr(decision, "stop", False)):
            return None
        inject = getattr(decision, "inject_message", None)
        allow = bool(getattr(decision, "allow", True))
        if isinstance(inject, str) and inject.strip():
            logger.info(
                "reasoning_loop.stop_hook_continue",
                session=self._session_key,
                vetoed=not allow,
            )
            return inject
        # The hook vetoed stop (allow=False) but gave no message — nudge
        # the model to keep going with a minimal continuation prompt.
        if not allow:
            logger.info(
                "reasoning_loop.stop_hook_veto", session=self._session_key
            )
            return "Please continue."
        return None

    def _maybe_auto_continue(
        self,
        messages: Sequence[dict[str, Any]],
        last_usage: dict[str, int] | None,
    ) -> str | None:
        """gap loop-token-budget-continuation: gated auto-continue nudge.

        Returns a 'continue' nudge string to inject when ALL hold:

        * ``self._autonomous`` is set (scheduled / background run);
        * a ``self._turn_token_budget`` is configured;
        * the turn's estimated tokens are below ``budget * 0.9``;
        * progress is not diminishing — the last round produced more than
          a trivial amount of output relative to the prior round;
        * we have not exceeded a small hard cap on nudges per turn.

        Returns ``None`` (turn ends normally) for every interactive turn
        and whenever any guard fails. Off by default.
        """
        if not self._autonomous or not self._turn_token_budget:
            return None
        # Hard cap so a stuck model can't be nudged forever.
        if self._auto_continue_count >= 3:
            return None
        used = self.messages_total_token_estimate(messages)
        if used >= int(self._turn_token_budget * 0.9):
            return None
        # Diminishing-returns guard: estimate this round's output size.
        # Prefer the provider-reported output_tokens; else fall back to a
        # cheap measure (the trailing assistant message length / 4).
        cur_len = 0
        if last_usage is not None:
            cur_len = int(last_usage.get("output_tokens", 0) or 0)
        if cur_len == 0:
            for m in reversed(messages):
                if m.get("role") == "assistant":
                    cur_len = len(_message_text(m)) // 4
                    break
        # If the previous nudge produced << the round before it, the model
        # is winding down — stop nudging.
        if (
            self._auto_continue_count > 0
            and self._auto_continue_last_len > 0
            and cur_len < self._auto_continue_last_len * 0.25
        ):
            return None
        self._auto_continue_count += 1
        self._auto_continue_last_len = cur_len
        logger.info(
            "reasoning_loop.auto_continue",
            session=self._session_key,
            count=self._auto_continue_count,
            tokens_used=used,
            budget=self._turn_token_budget,
        )
        return (
            "Continue working toward the goal. Budget remaining — if the "
            "task is genuinely complete, say so explicitly and stop; "
            "otherwise proceed with the next concrete step."
        )


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


def _dedup_tool_results(
    results: list[ToolResult],
    messages: Sequence[dict[str, Any]],
    calls: list[ToolCallEvent],
) -> list[ToolResult]:
    """WP19: Drop tool results whose (tool_name, args_json, content) triple
    was already seen in the conversation history.

    Prevents context bloat when the model repeatedly calls the same tool
    with the same arguments and receives identical output — a common pattern
    in ill-specified tasks where the agent's context window fills up with
    exact duplicates before it finally gives up.

    Strategy
    --------
    1. Build a set of ``(tool_name, args_json_str, content_str)`` tuples
       from every prior ``role="tool"`` message that has a matching
       ``role="assistant"`` tool_call entry in history.
    2. For each new result, compute its triple.  If already seen, replace
       its content with a short ``"[duplicate tool result — already in history]"``
       sentinel so the tool-call chain stays structurally valid (orphan
       tool_call_id would confuse providers) but no new tokens are consumed.

    Only string content is checked for duplicates; multimodal list content is
    always forwarded verbatim.
    """
    if not results or not messages:
        return results

    # Build a lookup: tool_call_id → (tool_name, args_json_str) from prior
    # assistant messages in history.
    prior_call_map: dict[str, tuple[str, str]] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = fn.get("name", "")
            args = fn.get("arguments", "")
            cid = tc.get("id", "")
            if cid:
                prior_call_map[cid] = (name, args)

    # Collect (tool_name, args_str, content_str) triples from prior tool messages.
    seen: set[tuple[str, str, str]] = set()
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        cid = msg.get("tool_call_id", "")
        content = msg.get("content")
        if not isinstance(content, str):
            continue  # skip multimodal — never dedup
        name_args = prior_call_map.get(cid, ("", ""))
        seen.add((name_args[0], name_args[1], content))

    # Build the call map for the CURRENT batch.
    current_call_map: dict[str, tuple[str, str]] = {
        c.call_id: (c.tool, c.args_json.decode("utf-8", errors="replace"))
        for c in calls
    }

    deduped: list[ToolResult] = []
    for r in results:
        if isinstance(r.content, list):
            # Multimodal — never dedup.
            deduped.append(r)
            continue
        name_args = current_call_map.get(r.call_id, ("", ""))
        triple = (name_args[0], name_args[1], r.content)
        if triple in seen:
            deduped.append(
                ToolResult(
                    call_id=r.call_id,
                    content="[duplicate tool result — already in history]",
                    is_error=False,
                )
            )
        else:
            seen.add(triple)
            deduped.append(r)
    return deduped


def _content_hash(content: Any) -> str:
    """Stable content fingerprint for turn-dedup.

    Hashes the user-visible text of a message ``content`` (string or
    multimodal list). Non-text parts contribute their ``type`` marker so
    two image-bearing turns aren't collapsed just because their text is
    equal. Returns a hex digest; pure / no-I/O.
    """
    import hashlib

    if isinstance(content, str):
        material = content
    elif isinstance(content, list):
        bits: list[str] = []
        for p in content:
            if isinstance(p, dict):
                t = p.get("text")
                if isinstance(t, str):
                    bits.append(t)
                else:
                    bits.append(f"\x00{p.get('type', '?')}")
        material = "\x01".join(bits)
    else:
        material = repr(content)
    return hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()


def _dedup_consecutive_turns(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop a turn that exactly repeats the immediately-preceding turn.

    Gap ``no-history-dedup``: providers and channels occasionally re-send
    an identical user prompt (double-tap, retry, group-chat echo) or the
    loop re-appends an identical plain-assistant turn. A content-hash
    pass collapses *consecutive* duplicates of the SAME role so the
    context window isn't padded with verbatim repeats.

    Pairing safety — never collapses a turn that participates in the
    tool-call protocol:

    * an ``assistant`` turn carrying ``tool_calls`` (its shell must stay
      to match the following ``role="tool"`` results);
    * any ``role="tool"`` message (orphaning a ``tool_call_id`` breaks
      the transcript);
    * ``system`` messages (seed prompt / summaries).

    Only plain ``user`` / ``assistant`` text turns are eligible. Returns
    the input list unchanged (identity preserved) when nothing was
    dropped so callers can rely on it.
    """
    if len(messages) < 2:
        return messages

    out: list[dict[str, Any]] = []
    dropped = 0
    prev_role: str | None = None
    prev_hash: str | None = None
    for msg in messages:
        role = msg.get("role")
        has_tool_calls = bool(msg.get("tool_calls"))
        eligible = role in ("user", "assistant") and not has_tool_calls
        if eligible:
            h = _content_hash(msg.get("content"))
            if role == prev_role and h == prev_hash:
                dropped += 1
                continue
            prev_role = role
            prev_hash = h
        else:
            # A non-eligible message breaks the consecutive run so a
            # later identical eligible turn is not mistakenly collapsed
            # across an intervening tool round.
            prev_role = None
            prev_hash = None
        out.append(msg)

    if not dropped:
        return messages
    logger.info("reasoning_loop.history_deduped", dropped=dropped)
    return out


def _extend_with_tool_round(
    messages: Sequence[dict[str, Any]],
    calls: list[ToolCallEvent],
    results: list[ToolResult],
    *,
    turn_output_spent: int = 0,
) -> list[dict[str, Any]]:
    """Return ``messages`` extended with the assistant tool_calls message
    and one ``role="tool"`` message per result.

    WP19: Deduplicates results whose (tool_name, args, content) triple was
    already seen in ``messages`` to prevent context bloat from repeated
    identical tool calls.

    Spill (gap-fill): a result larger than ``_TOOL_RESULT_SPILL_CAP`` — or
    any result once the turn's cumulative tool output (``turn_output_spent``,
    supplied by the caller) crosses ``_TURN_OUTPUT_BUDGET`` — is written to
    a temp file and replaced in-history with a short handle + preview,
    instead of the per-result head/tail truncation. ``turn_output_spent``
    defaults to ``0`` so legacy call sites (and direct unit tests) keep the
    plain-truncation behaviour and the historical list return type.
    """
    # WP19: dedup before extending history.
    results = _dedup_tool_results(results, messages, calls)

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
        # Multimodal content-block lists (e.g. image_url parts) are
        # forwarded verbatim — _truncate_tool_result only handles str.
        if isinstance(r.content, list):
            tool_content: str | list[dict[str, Any]] = r.content
        else:
            _running_spent = turn_output_spent + len(r.content)
            turn_output_spent = _running_spent
            # Spill when this single result is huge, OR the turn's
            # cumulative tool output has crossed the budget and this
            # result is itself non-trivial.
            if len(r.content) >= _TOOL_RESULT_SPILL_CAP or (
                _running_spent >= _TURN_OUTPUT_BUDGET
                and len(r.content) > _TOOL_RESULT_CAP
            ):
                tool_content = _spill_tool_result(r.content, r.call_id)
            else:
                tool_content = _truncate_tool_result(r.content)
        tool_msg: dict[str, Any] = {
            "role": "tool",
            "tool_call_id": r.call_id,
            "content": tool_content,
        }
        # C4: mark error results so the provider's message translation can
        # emit ``is_error: true`` on the Anthropic tool_result block. The
        # flag rides on the message dict under the ``_is_error`` key and is
        # only set when the result actually errored (absent == not an
        # error, so existing providers that ignore the key are unaffected).
        if r.is_error:
            tool_msg["_is_error"] = True
        extended.append(tool_msg)
    return extended


def _append_user_turn(
    messages: Sequence[dict[str, Any]], text: str
) -> list[dict[str, Any]]:
    """Return a NEW list with one ``{"role": "user", "content": text}`` turn
    appended. Used by the C3 Stop-hook continue path and the gated
    auto-continue nudge to re-open the loop with a follow-up instruction.
    """
    out = list(messages)
    out.append({"role": "user", "content": text})
    return out


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


def _message_text(msg: dict[str, Any]) -> str:
    """Pull a single text excerpt out of an OpenAI-shape message.

    Handles both the simple ``content: str`` shape and the multimodal
    ``content: list[ContentPart]`` shape (concatenating the ``text``
    fields). Used by :func:`_preview_user_text` /
    :func:`_preview_system_text` to build the ``user_text_preview`` /
    ``system_message_preview`` fields of :class:`TurnStart`.
    """
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, dict):
                txt = p.get("text")
                if isinstance(txt, str):
                    parts.append(txt)
        return " ".join(parts).strip()
    return ""


def _preview_user_text(messages: Sequence[dict[str, Any]], cap: int = 200) -> str:
    """Return the **last** user message's text, capped at ``cap`` chars."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            text = _message_text(msg).strip()
            return text[:cap]
    return ""


def _preview_system_text(messages: Sequence[dict[str, Any]], cap: int = 200) -> str:
    """Return the FIRST system message's text, capped at ``cap`` chars."""
    for msg in messages:
        if msg.get("role") == "system":
            text = _message_text(msg).strip()
            return text[:cap]
    return ""


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
        # bytes win over url when both are present: bytes are the
        # already-fetched authoritative payload, while the url may be
        # private to the gateway origin (e.g. ``/v1/files/{id}`` for a
        # web-chat upload) and unreachable from the model provider. The
        # gateway keeps that slim url on the attachment purely so the
        # journal records a re-fetchable reference instead of base64.
        if att.bytes_:
            # base64 data URL; providers that prefer raw bytes unwrap.
            import base64
            mime = att.mime or "image/*"
            b64 = base64.b64encode(att.bytes_).decode("ascii")
            return {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            }
        if att.url:
            return {"type": "image_url", "image_url": {"url": att.url}}
        return None
    # Audio / video / file — not universally supported. Forward as a
    # generic "file" part so providers that DO handle them (future
    # Gemini audio, future Claude file API) can opt in; text-only
    # providers will skip with a warn.
    if not att.url and not att.bytes_:
        return None
    part_file: dict[str, Any] = {
        "kind": att.kind,
        "url": att.url,
        "mime": att.mime,
        "file_name": att.file_name,
    }
    if att.bytes_:
        # A gateway-private url (``/v1/files/{id}`` — web-chat uploads)
        # is unreachable from the provider, so when the payload itself
        # was resolved ship it as an OpenAI-style ``file_data`` base64
        # data URL. Providers that consume file parts read it directly;
        # url-only attachments (channel CDNs) keep the slim form.
        import base64

        mime = att.mime or "application/octet-stream"
        b64 = base64.b64encode(att.bytes_).decode("ascii")
        part_file["file_data"] = f"data:{mime};base64,{b64}"
    return {"type": "file", "file": part_file}


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
