"""W3-3 review fix: only STREAMING chat requests are approval-capable.

A ``stream=false`` caller (curl / an OpenAI SDK pointed at the gateway)
never sees the AwaitingApproval live event and can never answer it —
stamping it capable would turn the previously-instant ask fail-close
into a silent 300s hang. The web chat streams, so it keeps the full
approval flow.
"""

from __future__ import annotations

from corlinman_server.gateway.routes.chat import ChatMessage, ChatRequest, _build_internal_request


def _req(stream: bool) -> ChatRequest:
    return ChatRequest(
        model="m",
        messages=[ChatMessage(role="user", content="hi")],
        stream=stream,
    )


def test_streaming_request_is_approval_capable() -> None:
    internal = _build_internal_request(_req(stream=True), "t::s1")
    assert internal.approval_capable is True


def test_non_streaming_request_fail_closes_instantly() -> None:
    internal = _build_internal_request(_req(stream=False), "t::s1")
    assert internal.approval_capable is False
