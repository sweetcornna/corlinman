"""AWS Bedrock provider adapter tests — offline, httpx MockTransport.

The Bedrock adapter signs an ``InvokeModelWithResponseStream`` request with
SigV4 and parses the AWS event-stream response. We patch
``httpx.AsyncClient`` so a :class:`httpx.MockTransport` answers the call
with a hand-built event-stream body, then assert:

* the request was sent to the regional ``bedrock-runtime`` host with a
  SigV4 ``Authorization`` header and the model id in the path;
* an Anthropic-on-Bedrock streaming response (text + tool-use blocks
  wrapped in event-stream frames) translates to the right
  :class:`ProviderChunk` sequence;
* HTTP error statuses and in-band ``exception`` frames map to the
  :class:`CorlinmanError` taxonomy;
* config gaps (missing region / credentials / non-Anthropic model) fail
  fast and clearly.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
from corlinman_providers import (
    AuthPermanentError,
    BedrockProvider,
    CorlinmanError,
    FormatError,
    ProviderChunk,
    ProviderKind,
    ProviderSpec,
    RateLimitError,
)
from corlinman_providers.failover import TimeoutError as ProviderTimeoutError

from corlinman_providers.bedrock_provider import _build_anthropic_body

from .test_aws_eventstream import encode_message

# --------------------------------------------------------------------------
# Event-stream fixture builders
# --------------------------------------------------------------------------


def _bedrock_chunk(event: dict[str, Any]) -> bytes:
    """Wrap an Anthropic SSE event as a Bedrock ``chunk`` event-stream frame.

    Bedrock payload shape: ``{"bytes": "<base64-of-the-inner-json>"}``.
    """
    inner = json.dumps(event).encode()
    payload = json.dumps({"bytes": base64.b64encode(inner).decode()}).encode()
    return encode_message(
        {":event-type": "chunk", ":message-type": "event"}, payload
    )


def _anthropic_text_stream() -> bytes:
    """A minimal text-only Anthropic-on-Bedrock response stream."""
    return b"".join(
        [
            _bedrock_chunk({"type": "message_start"}),
            _bedrock_chunk(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                }
            ),
            _bedrock_chunk(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Hello "},
                }
            ),
            _bedrock_chunk(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Bedrock"},
                }
            ),
            _bedrock_chunk({"type": "content_block_stop", "index": 0}),
            _bedrock_chunk(
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}
            ),
            _bedrock_chunk({"type": "message_stop"}),
        ]
    )


def _anthropic_tool_stream() -> bytes:
    """An Anthropic-on-Bedrock response that calls one tool."""
    return b"".join(
        [
            _bedrock_chunk({"type": "message_start"}),
            _bedrock_chunk(
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "SearchPlugin",
                    },
                }
            ),
            _bedrock_chunk(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '{"q":'},
                }
            ),
            _bedrock_chunk(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": '"hi"}'},
                }
            ),
            _bedrock_chunk({"type": "content_block_stop", "index": 0}),
            _bedrock_chunk(
                {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}
            ),
        ]
    )


def _patch_httpx(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
    *,
    captured: dict[str, Any] | None = None,
) -> None:
    """Swap ``httpx.AsyncClient`` for one wired to a MockTransport."""

    def _wrapped(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["request"] = request
        return handler(request)

    transport = httpx.MockTransport(_wrapped)
    real_client = httpx.AsyncClient

    def _factory(**kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return real_client(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _factory)


_SPEC = ProviderSpec(
    name="bedrock",
    kind=ProviderKind.BEDROCK,
    api_key="AKIDEXAMPLE:wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
    params={"region": "us-east-1"},
)
_MODEL = "anthropic.claude-3-5-sonnet-20241022-v2:0"


async def _collect(prov: BedrockProvider, **kw: Any) -> list[ProviderChunk]:
    chunks: list[ProviderChunk] = []
    async for c in prov.chat_stream(**kw):
        chunks.append(c)
    return chunks


# --------------------------------------------------------------------------
# Happy-path streaming
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_stream_translates_to_token_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_httpx(
        monkeypatch,
        lambda _req: httpx.Response(200, content=_anthropic_text_stream()),
        captured=captured,
    )
    prov = BedrockProvider.build(_SPEC)
    chunks = await _collect(
        prov, model=_MODEL, messages=[{"role": "user", "content": "hi"}]
    )

    texts = [c.text for c in chunks if c.kind == "token"]
    assert texts == ["Hello ", "Bedrock"]
    assert chunks[-1].kind == "done"
    assert chunks[-1].finish_reason == "stop"

    # SigV4 + endpoint shape on the wire.
    req = captured["request"]
    assert req.url.host == "bedrock-runtime.us-east-1.amazonaws.com"
    assert _MODEL in str(req.url.path) or _MODEL.replace(":", "%3A") in str(req.url)
    assert "invoke-with-response-stream" in str(req.url)
    assert req.headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert "x-amz-date" in req.headers
    body = json.loads(req.content)
    assert body["anthropic_version"] == "bedrock-2023-05-31"
    assert "model" not in body  # model travels in the URL path


@pytest.mark.asyncio
async def test_tool_call_stream_aggregates_to_standard_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_httpx(
        monkeypatch,
        lambda _req: httpx.Response(200, content=_anthropic_tool_stream()),
    )
    prov = BedrockProvider.build(_SPEC)
    chunks = await _collect(
        prov, model=_MODEL, messages=[{"role": "user", "content": "go"}]
    )

    kinds = [c.kind for c in chunks]
    assert kinds[0] == "tool_call_start"
    assert chunks[0].tool_call_id == "toolu_01"
    assert chunks[0].tool_name == "SearchPlugin"

    deltas = [c.arguments_delta for c in chunks if c.kind == "tool_call_delta"]
    assert deltas == ['{"q":', '"hi"}']

    ends = [c for c in chunks if c.kind == "tool_call_end"]
    assert len(ends) == 1 and ends[0].tool_call_id == "toolu_01"

    assert chunks[-1].kind == "done"
    assert chunks[-1].finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_system_message_lifted_and_tools_normalised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    _patch_httpx(
        monkeypatch,
        lambda _req: httpx.Response(200, content=_anthropic_text_stream()),
        captured=captured,
    )
    prov = BedrockProvider.build(_SPEC)
    await _collect(
        prov,
        model=_MODEL,
        messages=[
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "hi"},
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "SearchPlugin",
                    "description": "search",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )
    body = json.loads(captured["request"].content)
    assert body["system"] == "be brief"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    # OpenAI-shape tool spec → Anthropic ``input_schema`` shape.
    assert body["tools"][0]["name"] == "SearchPlugin"
    assert body["tools"][0]["input_schema"] == {
        "type": "object",
        "properties": {},
    }


def test_build_anthropic_body_translates_tool_round_to_vendor_blocks() -> None:
    """An assistant ``tool_calls`` turn + ``role="tool"`` result translate
    into Anthropic ``tool_use`` / ``tool_result`` content blocks (audit B1).

    Before the fix the assistant turn collapsed to
    ``{"role":"assistant","content":""}`` (tool_calls lost) and the tool
    result became a bare ``{"role":"user","content":"res"}`` — which
    Bedrock/Anthropic rejects, breaking every multi-round tool call.
    """
    body = _build_anthropic_body(
        messages=[
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "f", "arguments": '{"x":1}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "res"},
        ],
        tools=None,
        temperature=None,
        max_tokens=None,
        extra=None,
    )
    chat = body["messages"]
    assert [m["role"] for m in chat] == ["user", "assistant", "user"]

    assistant_content = chat[1]["content"]
    assert isinstance(assistant_content, list)
    assert {
        "type": "tool_use",
        "id": "call_1",
        "name": "f",
        "input": {"x": 1},
    } in assistant_content
    assert chat[1]["content"] != ""  # NOT an empty assistant turn

    assert chat[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "call_1", "content": "res"}
    ]


def test_build_anthropic_body_coalesces_parallel_tool_results() -> None:
    """PARALLEL tool calls: an assistant turn with N>1 ``tool_use`` blocks
    must be answered by a SINGLE following user turn carrying ALL N
    ``tool_result`` blocks (Anthropic-on-Bedrock 400s otherwise).

    Before the fix each ``role="tool"`` message produced its OWN user
    turn, so two consecutive tool results became two consecutive user
    turns — ``[...,"assistant","user","user"]`` — which is rejected.
    """
    body = _build_anthropic_body(
        messages=[
            {"role": "user", "content": "go"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_a",
                        "type": "function",
                        "function": {"name": "fa", "arguments": '{"x":1}'},
                    },
                    {
                        "id": "call_b",
                        "type": "function",
                        "function": {"name": "fb", "arguments": '{"y":2}'},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "call_a", "content": "res_a"},
            {"role": "tool", "tool_call_id": "call_b", "content": "res_b"},
        ],
        tools=None,
        temperature=None,
        max_tokens=None,
        extra=None,
    )
    chat = body["messages"]
    assert [m["role"] for m in chat] == ["user", "assistant", "user"]
    assert chat[2]["content"] == [
        {"type": "tool_result", "tool_use_id": "call_a", "content": "res_a"},
        {"type": "tool_result", "tool_use_id": "call_b", "content": "res_b"},
    ]


def test_build_anthropic_body_tool_call_all_nondict_yields_fallback_text() -> None:
    """Zero-block guard: an assistant turn with truthy ``tool_calls`` whose
    entries are all non-dict (filtered out) must NOT emit ``content: []``
    — a fallback text block keeps the Bedrock body valid."""
    body = _build_anthropic_body(
        messages=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": ["not-a-dict", 42],
            },
        ],
        tools=None,
        temperature=None,
        max_tokens=None,
        extra=None,
    )
    chat = body["messages"]
    assert chat[0]["role"] == "assistant"
    assert chat[0]["content"] == [{"type": "text", "text": ""}]
    assert chat[0]["content"] != []


# --------------------------------------------------------------------------
# Error paths
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_403_maps_to_auth_permanent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_httpx(
        monkeypatch,
        lambda _req: httpx.Response(403, text="access denied to model"),
    )
    prov = BedrockProvider.build(_SPEC)
    with pytest.raises(AuthPermanentError):
        await _collect(prov, model=_MODEL, messages=[{"role": "user", "content": "x"}])


@pytest.mark.asyncio
async def test_http_429_maps_to_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_httpx(
        monkeypatch,
        lambda _req: httpx.Response(429, text="ThrottlingException"),
    )
    prov = BedrockProvider.build(_SPEC)
    with pytest.raises(RateLimitError):
        await _collect(prov, model=_MODEL, messages=[{"role": "user", "content": "x"}])


@pytest.mark.asyncio
async def test_inband_exception_frame_maps_to_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 200 response whose event-stream carries a ThrottlingException."""
    exc_frame = encode_message(
        {":message-type": "exception", ":exception-type": "ThrottlingException"},
        b'{"message":"rate exceeded"}',
    )
    _patch_httpx(monkeypatch, lambda _req: httpx.Response(200, content=exc_frame))
    prov = BedrockProvider.build(_SPEC)
    with pytest.raises(RateLimitError):
        await _collect(prov, model=_MODEL, messages=[{"role": "user", "content": "x"}])


@pytest.mark.asyncio
async def test_timeout_maps_to_provider_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(_req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    _patch_httpx(monkeypatch, _raise)
    prov = BedrockProvider.build(_SPEC)
    with pytest.raises(ProviderTimeoutError):
        await _collect(prov, model=_MODEL, messages=[{"role": "user", "content": "x"}])


# --------------------------------------------------------------------------
# Config validation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_region_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    prov = BedrockProvider.build(
        ProviderSpec(
            name="b", kind=ProviderKind.BEDROCK, api_key="AKID:secret", params={}
        )
    )
    with pytest.raises(RuntimeError, match="region"):
        await _collect(prov, model=_MODEL, messages=[])


@pytest.mark.asyncio
async def test_missing_credentials_raises_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    prov = BedrockProvider.build(
        ProviderSpec(
            name="b", kind=ProviderKind.BEDROCK, params={"region": "us-east-1"}
        )
    )
    with pytest.raises(RuntimeError, match="credentials"):
        await _collect(prov, model=_MODEL, messages=[])


@pytest.mark.asyncio
async def test_non_anthropic_model_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prov = BedrockProvider.build(_SPEC)
    with pytest.raises(FormatError, match="Anthropic-on-Bedrock"):
        await _collect(
            prov, model="amazon.titan-text-express-v1", messages=[]
        )


@pytest.mark.asyncio
async def test_corrupted_stream_maps_to_format_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = bytearray(_anthropic_text_stream())
    raw[-1] ^= 0xFF  # corrupt the final message CRC
    _patch_httpx(monkeypatch, lambda _req: httpx.Response(200, content=bytes(raw)))
    prov = BedrockProvider.build(_SPEC)
    with pytest.raises((FormatError, CorlinmanError)):
        await _collect(prov, model=_MODEL, messages=[{"role": "user", "content": "x"}])


def test_supports_never_claims_via_prefix() -> None:
    # Bedrock is alias-addressed only.
    assert not BedrockProvider.supports("anthropic.claude-3-haiku")


def test_base_url_override_changes_endpoint() -> None:
    prov = BedrockProvider.build(
        ProviderSpec(
            name="b",
            kind=ProviderKind.BEDROCK,
            api_key="AKID:secret",
            base_url="https://my-vpce-endpoint.example.com",
            params={"region": "us-east-1"},
        )
    )
    host, scheme = prov._endpoint()
    assert host == "my-vpce-endpoint.example.com"
    assert scheme == "https"
