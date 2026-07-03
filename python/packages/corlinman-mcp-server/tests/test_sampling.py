"""MCP client-side sampling/createMessage responder (Dim 5)."""

from __future__ import annotations

import pytest
from corlinman_mcp_server.sampling import (
    SamplingConfig,
    SamplingRequest,
    SamplingResponder,
    SamplingResult,
)
from corlinman_mcp_server.types import error_codes


def _cfg(**kw) -> SamplingConfig:
    base = {"mode": "auto", "allowed_models": ["claude-sonnet-4-5"], "rate_limit_per_min": 10}
    base.update(kw)
    return SamplingConfig(**base)


async def _completer(req: SamplingRequest) -> SamplingResult:
    return SamplingResult(text=f"ran {req.model}", model=req.model, stop_reason="endTurn")


def _params(**kw) -> dict:
    base = {
        "messages": [{"role": "user", "content": {"type": "text", "text": "hi"}}],
        "maxTokens": 100,
        "modelPreferences": {"hints": [{"name": "claude-sonnet-4-5"}]},
    }
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# config parsing
# ---------------------------------------------------------------------------


def test_config_defaults_secure_off():
    cfg = SamplingConfig.from_mcp_config({})
    assert cfg.mode == "off"
    assert cfg.enabled is False


def test_config_unknown_mode_falls_back_off():
    cfg = SamplingConfig.from_mcp_config({"sampling": {"mode": "yolo"}})
    assert cfg.mode == "off"


def test_config_parses_full():
    cfg = SamplingConfig.from_mcp_config(
        {"sampling": {"mode": "auto", "allowed_models": ["a", "b"], "rate_limit_per_min": 3, "max_tokens_cap": 500}}
    )
    assert cfg.mode == "auto"
    assert cfg.allowed_models == ["a", "b"]
    assert cfg.rate_limit_per_min == 3
    assert cfg.max_tokens_cap == 500


def test_config_bad_scalars_default():
    cfg = SamplingConfig.from_mcp_config(
        {"sampling": {"mode": "auto", "rate_limit_per_min": "lots", "max_tokens_cap": -5}}
    )
    assert cfg.rate_limit_per_min == 10
    assert cfg.max_tokens_cap == 2048


# ---------------------------------------------------------------------------
# capability advertisement gate
# ---------------------------------------------------------------------------


def test_advertises_only_when_enabled_and_wired():
    assert SamplingResponder(_cfg(), _completer).advertises_capability is True
    assert SamplingResponder(_cfg(mode="off"), _completer).advertises_capability is False
    assert SamplingResponder(_cfg(), None).advertises_capability is False


# ---------------------------------------------------------------------------
# handle() policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_off_mode_rejects():
    r = SamplingResponder(_cfg(mode="off"), _completer)
    result, error = await r.handle("srv", _params())
    assert result is None
    assert error["code"] == error_codes.METHOD_NOT_FOUND


@pytest.mark.asyncio
async def test_unwired_completer_errors():
    r = SamplingResponder(_cfg(), None)
    _result, error = await r.handle("srv", _params())
    assert error["code"] == error_codes.SAMPLING_UNAVAILABLE


@pytest.mark.asyncio
async def test_auto_mode_runs_completion():
    r = SamplingResponder(_cfg(), _completer)
    result, error = await r.handle("srv", _params())
    assert error is None
    assert result["role"] == "assistant"
    assert result["content"]["text"] == "ran claude-sonnet-4-5"
    assert result["model"] == "claude-sonnet-4-5"
    assert result["stopReason"] == "endTurn"


@pytest.mark.asyncio
async def test_model_not_whitelisted_rejected():
    r = SamplingResponder(_cfg(allowed_models=["only-this"]), _completer)
    _result, error = await r.handle("srv", _params(modelPreferences={"hints": [{"name": "other"}]}))
    assert error["code"] == error_codes.SAMPLING_MODEL_NOT_ALLOWED


@pytest.mark.asyncio
async def test_no_hints_defaults_to_first_allowed():
    r = SamplingResponder(_cfg(allowed_models=["default-model"]), _completer)
    result, error = await r.handle("srv", _params(modelPreferences={}))
    assert error is None
    assert result["model"] == "default-model"


@pytest.mark.asyncio
async def test_empty_allowlist_rejects():
    r = SamplingResponder(_cfg(allowed_models=[]), _completer)
    _result, error = await r.handle("srv", _params())
    assert error["code"] == error_codes.SAMPLING_MODEL_NOT_ALLOWED


@pytest.mark.asyncio
async def test_hint_substring_match():
    r = SamplingResponder(_cfg(allowed_models=["claude-sonnet-4-5"]), _completer)
    result, error = await r.handle("srv", _params(modelPreferences={"hints": [{"name": "sonnet"}]}))
    assert error is None
    assert result["model"] == "claude-sonnet-4-5"


@pytest.mark.asyncio
async def test_rate_limit_rejects_after_capacity():
    r = SamplingResponder(_cfg(rate_limit_per_min=2), _completer)
    ok1, _ = await r.handle("srv", _params())
    ok2, _ = await r.handle("srv", _params())
    _, error3 = await r.handle("srv", _params())
    assert ok1 is not None and ok2 is not None
    assert error3["code"] == error_codes.RATE_LIMITED


@pytest.mark.asyncio
async def test_rate_limit_is_per_server():
    r = SamplingResponder(_cfg(rate_limit_per_min=1), _completer)
    a, _ = await r.handle("srv-a", _params())
    b, _ = await r.handle("srv-b", _params())
    assert a is not None and b is not None  # distinct buckets


@pytest.mark.asyncio
async def test_max_tokens_clamped():
    seen: list[int] = []

    async def capture(req: SamplingRequest) -> SamplingResult:
        seen.append(req.max_tokens)
        return SamplingResult(text="x", model=req.model)

    r = SamplingResponder(_cfg(max_tokens_cap=50), capture)
    await r.handle("srv", _params(maxTokens=9999))
    assert seen == [50]


@pytest.mark.asyncio
async def test_messages_translated_text_and_image():
    seen: list = []

    async def capture(req: SamplingRequest) -> SamplingResult:
        seen.append(req.messages)
        return SamplingResult(text="x", model=req.model)

    r = SamplingResponder(_cfg(), capture)
    params = _params(
        messages=[
            {"role": "user", "content": {"type": "text", "text": "hello"}},
            {"role": "assistant", "content": {"type": "image", "data": "...", "mimeType": "image/png"}},
        ]
    )
    await r.handle("srv", params)
    assert seen[0] == [
        {"role": "user", "text": "hello"},
        {"role": "assistant", "text": "[image omitted]"},
    ]


@pytest.mark.asyncio
async def test_ask_mode_denies_without_approval():
    r = SamplingResponder(_cfg(mode="ask"), _completer)  # no approval hook
    _result, error = await r.handle("srv", _params())
    assert error["code"] == error_codes.TOOL_NOT_ALLOWED


@pytest.mark.asyncio
async def test_ask_mode_allows_with_approval():
    async def approve(server, req):
        return True

    r = SamplingResponder(_cfg(mode="ask"), _completer, approval_hook=approve)
    result, error = await r.handle("srv", _params())
    assert error is None
    assert result["content"]["text"].startswith("ran ")


@pytest.mark.asyncio
async def test_completer_failure_is_clean_error():
    async def boom(req: SamplingRequest) -> SamplingResult:
        raise RuntimeError("provider down")

    r = SamplingResponder(_cfg(), boom)
    _result, error = await r.handle("srv", _params())
    assert error["code"] == error_codes.INTERNAL_ERROR
    assert "provider down" in error["message"]
