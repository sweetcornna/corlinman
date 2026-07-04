"""MCP client-side ``sampling/createMessage`` responder (Dim 5).

When corlinman connects to an external MCP server as a client, the server
may ask corlinman to run an LLM completion on its behalf
(``sampling/createMessage``). This module implements the responder,
adapted from hermes' ``SamplingHandler`` (mechanism absorbed, not copied):
a **mode gate** (secure-by-default: sampling is off unless opted in), a
per-server **rate limit**, and a **model whitelist**, over an injected
provider-agnostic completer callable.

The package stays provider-agnostic: the actual completion runs through a
``Completer`` the gateway injects (wrapping its provider resolver). Unset
completer → ``sampling_unavailable`` error, so a config written for the
gateway also loads in a context that can't run completions.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .types import error_codes

__all__ = [
    "Completer",
    "SamplingConfig",
    "SamplingRequest",
    "SamplingResponder",
    "SamplingResult",
]

# Valid sampling modes. ``off`` = never advertise the capability and reject
# every request; ``auto`` = allow within whitelist + rate limit; ``ask`` =
# route through the caller's approval hook (which today fail-closes to deny
# until the Dim 3 console resolver lands).
_MODES = ("off", "auto", "ask")

_DEFAULT_RATE_LIMIT_PER_MIN = 10
_DEFAULT_MAX_TOKENS_CAP = 2048


@dataclass
class SamplingConfig:
    """Parsed ``[mcp.sampling]`` config (defensive; total parser)."""

    mode: str = "off"
    allowed_models: list[str] = field(default_factory=list)
    rate_limit_per_min: int = _DEFAULT_RATE_LIMIT_PER_MIN
    max_tokens_cap: int = _DEFAULT_MAX_TOKENS_CAP

    @property
    def enabled(self) -> bool:
        return self.mode in ("auto", "ask")

    @classmethod
    def from_mcp_config(cls, mcp_cfg: Any) -> SamplingConfig:
        """Parse from the ``[mcp]`` config dict's ``sampling`` sub-table.

        Never raises: an unknown mode falls back to ``off`` (secure
        default); bad scalars fall back to their defaults.
        """
        cfg = cls()
        if not isinstance(mcp_cfg, dict):
            return cfg
        raw = mcp_cfg.get("sampling")
        if not isinstance(raw, dict):
            return cfg
        mode = str(raw.get("mode", "off")).strip().lower()
        cfg.mode = mode if mode in _MODES else "off"
        models = raw.get("allowed_models")
        if isinstance(models, (list, tuple)):
            cfg.allowed_models = [str(m).strip() for m in models if str(m).strip()]
        cfg.rate_limit_per_min = _coerce_int(
            raw.get("rate_limit_per_min"), _DEFAULT_RATE_LIMIT_PER_MIN
        )
        cfg.max_tokens_cap = _coerce_int(
            raw.get("max_tokens_cap"), _DEFAULT_MAX_TOKENS_CAP
        )
        return cfg


def _coerce_int(value: Any, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


@dataclass
class SamplingRequest:
    """Normalized completion request handed to the injected completer.

    Provider-agnostic: ``messages`` is a list of ``{"role", "text"}``
    (image parts are rendered to a text placeholder in v1). ``model`` is
    the already-whitelisted alias to run.
    """

    model: str
    messages: list[dict[str, str]]
    max_tokens: int
    system_prompt: str | None = None
    temperature: float | None = None
    stop_sequences: list[str] = field(default_factory=list)


@dataclass
class SamplingResult:
    """Result from the completer, shaped back into an MCP result."""

    text: str
    model: str
    stop_reason: str = "endTurn"


Completer = Callable[[SamplingRequest], Awaitable[SamplingResult]]
#: Approval hook for ``mode = "ask"``: ``(server_name, request) -> bool``.
ApprovalHook = Callable[[str, SamplingRequest], Awaitable[bool]]


class _TokenBucket:
    """Refilling token bucket — ``capacity`` requests, refills over 60s."""

    def __init__(self, capacity: int) -> None:
        self._capacity = float(max(1, capacity))
        self._tokens = self._capacity
        self._refill_per_sec = self._capacity / 60.0
        self._last = time.monotonic()

    def try_acquire(self) -> bool:
        now = time.monotonic()
        self._tokens = min(
            self._capacity, self._tokens + (now - self._last) * self._refill_per_sec
        )
        self._last = now
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False


def _err(code: int, message: str) -> tuple[None, dict[str, Any]]:
    return None, {"code": code, "message": message}


def _translate_messages(raw: Any) -> list[dict[str, str]]:
    """MCP ``messages`` → ``[{"role","text"}]`` (text-only in v1)."""
    out: list[dict[str, str]] = []
    if not isinstance(raw, list):
        return out
    for m in raw:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "user")
        content = m.get("content")
        text = ""
        if isinstance(content, dict):
            if content.get("type") == "text":
                text = str(content.get("text") or "")
            elif content.get("type") == "image":
                text = "[image omitted]"
        elif isinstance(content, str):
            text = content
        out.append({"role": role, "text": text})
    return out


class SamplingResponder:
    """Services ``sampling/createMessage`` under mode/rate/whitelist policy."""

    def __init__(
        self,
        config: SamplingConfig,
        completer: Completer | None = None,
        *,
        approval_hook: ApprovalHook | None = None,
    ) -> None:
        self._config = config
        self._completer = completer
        self._approval_hook = approval_hook
        self._buckets: dict[str, _TokenBucket] = {}

    @property
    def config(self) -> SamplingConfig:
        return self._config

    @property
    def advertises_capability(self) -> bool:
        """Whether the client should send ``capabilities.sampling``.

        Requires a completer wired, an enabled mode, AND a non-empty
        ``allowed_models`` — an empty allow-list rejects every request with
        ``SAMPLING_MODEL_NOT_ALLOWED``, so advertising the capability then
        would tell a server sampling works when no request can ever
        succeed (Codex #110)."""
        return (
            self._config.enabled
            and self._completer is not None
            and bool(self._config.allowed_models)
        )

    def _resolve_model(self, params: dict[str, Any]) -> str | None:
        allowed = self._config.allowed_models
        if not allowed:
            return None
        prefs = params.get("modelPreferences")
        hints = prefs.get("hints") if isinstance(prefs, dict) else None
        names = [
            str(h.get("name")).strip()
            for h in hints
            if isinstance(h, dict) and h.get("name")
        ] if isinstance(hints, list) else []
        for hint in names:
            for model in allowed:
                if model == hint or hint in model:
                    return model
        # No usable hint → default to the first whitelisted model.
        return allowed[0] if not names else None

    async def handle(
        self, server_name: str, params: dict[str, Any]
    ) -> tuple[Any, dict[str, Any] | None]:
        """Return ``(result, error)`` for a ``sampling/createMessage``."""
        cfg = self._config
        if cfg.mode == "off":
            return _err(error_codes.METHOD_NOT_FOUND, "sampling is disabled")
        if self._completer is None:
            return _err(error_codes.SAMPLING_UNAVAILABLE, "no sampling completer wired")

        bucket = self._buckets.setdefault(
            server_name, _TokenBucket(cfg.rate_limit_per_min)
        )
        if not bucket.try_acquire():
            return _err(error_codes.RATE_LIMITED, "sampling rate limit exceeded")

        model = self._resolve_model(params)
        if model is None:
            return _err(
                error_codes.SAMPLING_MODEL_NOT_ALLOWED,
                "no requested model is on the sampling allow-list",
            )

        try:
            max_tokens = min(int(params.get("maxTokens") or cfg.max_tokens_cap), cfg.max_tokens_cap)
        except (TypeError, ValueError):
            max_tokens = cfg.max_tokens_cap
        temperature = params.get("temperature")
        stop = params.get("stopSequences")
        request = SamplingRequest(
            model=model,
            messages=_translate_messages(params.get("messages")),
            max_tokens=max(1, max_tokens),
            system_prompt=str(params["systemPrompt"]) if params.get("systemPrompt") else None,
            temperature=float(temperature) if isinstance(temperature, (int, float)) else None,
            stop_sequences=[str(s) for s in stop] if isinstance(stop, list) else [],
        )

        if cfg.mode == "ask":
            approved = False
            if self._approval_hook is not None:
                try:
                    approved = bool(await self._approval_hook(server_name, request))
                except Exception:  # noqa: BLE001 — approval failure denies
                    approved = False
            if not approved:
                return _err(error_codes.TOOL_NOT_ALLOWED, "sampling request denied")

        try:
            result = await self._completer(request)
        except Exception as exc:  # noqa: BLE001 — completer failure is a clean error
            return _err(error_codes.INTERNAL_ERROR, f"sampling completion failed: {exc}")

        return {
            "role": "assistant",
            "content": {"type": "text", "text": result.text},
            "model": result.model,
            "stopReason": result.stop_reason,
        }, None
