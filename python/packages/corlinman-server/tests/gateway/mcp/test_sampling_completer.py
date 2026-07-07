"""Dim 5 — provider-backed MCP sampling completer (production wiring).

Pins the gap-close: ``state.extras["mcp_sampling_completer"]`` finally
has a writer. The completer must resolve through the LIVE
``state.provider_registry`` handle (read per call — the providers
bootstrap runs after the MCP block, and hot-reload swaps the handle),
fold streamed token deltas into one result, drop reasoning deltas, and
degrade to a clean exception (→ responder ``INTERNAL_ERROR``) when the
registry is not wired yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_mcp_server.sampling import (
    SamplingConfig,
    SamplingRequest,
    SamplingResponder,
)
from corlinman_server.gateway.mcp.sampling_completer import (
    build_sampling_completer,
)

pytestmark = pytest.mark.asyncio


@dataclass
class _Chunk:
    kind: str
    text: str | None = None
    finish_reason: str | None = None
    is_reasoning: bool = False


@dataclass
class _FakeProvider:
    chunks: list[_Chunk]
    calls: list[dict[str, Any]] = field(default_factory=list)

    def chat_stream(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)

        async def _gen() -> Any:
            for c in self.chunks:
                yield c

        return _gen()


class _FakeRegistry:
    def __init__(self, provider: _FakeProvider) -> None:
        self.provider = provider
        self.resolved: list[str] = []

    def resolve(
        self, alias_or_model: str, *, aliases: Any = None, **_: Any
    ) -> tuple[Any, str, dict[str, Any]]:
        self.resolved.append(alias_or_model)
        return self.provider, f"upstream/{alias_or_model}", {}


def _state(registry: Any) -> SimpleNamespace:
    return SimpleNamespace(
        provider_registry=registry, config={"models": {}}, extras={}
    )


async def test_completer_folds_stream_and_maps_stop_reason() -> None:
    provider = _FakeProvider(
        [
            _Chunk(kind="token", text="think…", is_reasoning=True),
            _Chunk(kind="token", text="Hello "),
            _Chunk(kind="token", text="world"),
            _Chunk(kind="done", finish_reason="length"),
        ]
    )
    completer = build_sampling_completer(_state(_FakeRegistry(provider)))

    result = await completer(
        SamplingRequest(
            model="haiku",
            messages=[{"role": "user", "text": "hi"}],
            max_tokens=64,
            system_prompt="be brief",
        )
    )

    # Reasoning deltas never leak to the requesting MCP server.
    assert result.text == "Hello world"
    assert result.model == "haiku"
    assert result.stop_reason == "maxTokens"
    # System prompt travels as the leading system message.
    sent = provider.calls[0]["messages"]
    assert (sent[0].role, sent[0].content) == ("system", "be brief")
    assert (sent[1].role, sent[1].content) == ("user", "hi")
    assert provider.calls[0]["model"] == "upstream/haiku"
    assert provider.calls[0]["max_tokens"] == 64


async def test_completer_reads_registry_per_call() -> None:
    """The registry is looked up at call time, so a registry that
    appears AFTER the completer was built (providers bootstrap ordering)
    — or is swapped by hot-reload — is picked up."""
    state = _state(None)
    completer = build_sampling_completer(state)

    with pytest.raises(RuntimeError):
        await completer(
            SamplingRequest(model="m", messages=[], max_tokens=8)
        )

    provider = _FakeProvider([_Chunk(kind="token", text="ok"), _Chunk(kind="done")])
    state.provider_registry = _FakeRegistry(provider)
    result = await completer(
        SamplingRequest(model="m", messages=[], max_tokens=8)
    )
    assert result.text == "ok"
    assert result.stop_reason == "endTurn"


async def test_responder_with_wired_completer_advertises_and_answers() -> None:
    """End-to-end through the responder: with the completer wired and
    an opted-in config, the capability advertises and a
    ``sampling/createMessage`` returns assistant text."""
    provider = _FakeProvider(
        [_Chunk(kind="token", text="42"), _Chunk(kind="done", finish_reason="stop")]
    )
    completer = build_sampling_completer(_state(_FakeRegistry(provider)))
    responder = SamplingResponder(
        SamplingConfig.from_mcp_config(
            {"sampling": {"mode": "auto", "allowed_models": ["haiku"]}}
        ),
        completer,
    )

    assert responder.advertises_capability is True
    result, error = await responder.handle(
        "some-server",
        {
            "messages": [
                {"role": "user", "content": {"type": "text", "text": "6*7?"}}
            ],
            "maxTokens": 16,
        },
    )
    assert error is None
    assert result["content"]["text"] == "42"
    assert result["stopReason"] == "endTurn"


async def test_responder_maps_completer_failure_to_internal_error() -> None:
    """Registry missing at call time → the responder answers with a
    clean INTERNAL_ERROR instead of crashing the peer loop."""
    completer = build_sampling_completer(_state(None))
    responder = SamplingResponder(
        SamplingConfig.from_mcp_config(
            {"sampling": {"mode": "auto", "allowed_models": ["haiku"]}}
        ),
        completer,
    )

    result, error = await responder.handle(
        "some-server",
        {
            "messages": [
                {"role": "user", "content": {"type": "text", "text": "hi"}}
            ]
        },
    )
    assert result is None
    assert error is not None
    assert "sampling completion failed" in str(error.get("message", error))
