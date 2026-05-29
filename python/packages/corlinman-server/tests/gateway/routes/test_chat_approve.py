"""Branch-level tests for ``corlinman_server.gateway.routes.chat_approve``.

The companion ``test_chat_requires_auth.py`` covers the *outer* auth
gate around ``POST /v1/chat/completions/{turn_id}/approve``. This file
covers the handler body itself — five error envelopes plus the happy
path — by mounting :func:`chat_approve.router` directly on a bare
``FastAPI`` app (no middleware) and driving each branch with a tiny
in-memory resolver. Mirrors the byte-for-byte JSON shapes the Rust
implementation returned (TEST-001).
"""

from __future__ import annotations

from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")

from corlinman_server.gateway.routes import chat_approve  # noqa: E402
from corlinman_server.gateway.routes.chat_approve import (  # noqa: E402
    ApprovalDecision,
    ChatApproveState,
    NotFoundError,
)
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# ─── Fixture helpers ─────────────────────────────────────────────────


def _client(state: ChatApproveState | None) -> TestClient:
    """Mount ``chat_approve.router(state)`` on a bare FastAPI app.

    Mirrors the pattern in :mod:`test_health` — no middleware, no
    lifespan, so the handler body is exercised in isolation."""
    app = FastAPI()
    app.include_router(chat_approve.router(state))
    return TestClient(app)


class _RecordingResolver:
    """Tiny in-memory resolver fake.

    * Records every ``(call_id, decision)`` pair the route forwards.
    * Optionally raises a configured exception so a single resolver
      instance can drive the 404 and 500 branches without subclassing.
    """

    def __init__(self, raises: Exception | None = None) -> None:
        self._raises = raises
        self.calls: list[tuple[str, ApprovalDecision]] = []

    async def __call__(self, call_id: str, decision: ApprovalDecision) -> None:
        self.calls.append((call_id, decision))
        if self._raises is not None:
            raise self._raises


# ─── 503: resolver=None ──────────────────────────────────────────────


def test_returns_503_when_resolver_unwired() -> None:
    """``ChatApproveState(resolver=None)`` is the gateway's "approval
    gate not configured" sentinel — every request short-circuits with
    a 503 ``approvals_disabled`` envelope before the body is read."""
    client = _client(ChatApproveState(resolver=None))

    resp = client.post(
        "/v1/chat/completions/turn-abc/approve",
        json={"call_id": "call_abc123", "approved": True},
    )

    assert resp.status_code == 503, resp.text
    body: dict[str, Any] = resp.json()
    assert body == {
        "error": "approvals_disabled",
        "message": "approval gate is not configured on this gateway",
    }


def test_default_state_is_unwired() -> None:
    """``router()`` called with no state must behave the same as
    ``router(ChatApproveState())`` — i.e. resolver=None → 503.

    Regression guard: if someone wires a default resolver into
    :class:`ChatApproveState`, every gateway boot would silently
    auto-approve tool calls."""
    app = FastAPI()
    app.include_router(chat_approve.router())  # no state at all
    client = TestClient(app)

    resp = client.post(
        "/v1/chat/completions/turn-abc/approve",
        json={"call_id": "call_abc123", "approved": True},
    )
    assert resp.status_code == 503
    assert resp.json()["error"] == "approvals_disabled"


# ─── 400: empty call_id ──────────────────────────────────────────────


def test_returns_400_when_call_id_is_blank() -> None:
    """``call_id`` is .strip()ed and must be non-empty. A
    whitespace-only id is the same gap a literal ``""`` would be —
    both must trip ``invalid_request``."""
    resolver = _RecordingResolver()
    client = _client(ChatApproveState(resolver=resolver))

    resp = client.post(
        "/v1/chat/completions/turn-abc/approve",
        json={"call_id": "   ", "approved": True},
    )

    assert resp.status_code == 400, resp.text
    assert resp.json() == {
        "error": "invalid_request",
        "message": "`call_id` is required and must be non-empty",
    }
    # Critical: we must NOT have called the resolver with a blank id.
    assert resolver.calls == []


def test_returns_400_when_call_id_is_empty_string() -> None:
    """Literal empty string variant of the prior test — locks down
    both forms."""
    resolver = _RecordingResolver()
    client = _client(ChatApproveState(resolver=resolver))

    resp = client.post(
        "/v1/chat/completions/turn-abc/approve",
        json={"call_id": "", "approved": True},
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "invalid_request"
    assert resolver.calls == []


# ─── 400: denied without a deny_message ──────────────────────────────


def test_returns_400_when_deny_without_deny_message() -> None:
    """``approved=false`` requires ``deny_message`` (non-blank).
    Missing the field entirely → 400 ``invalid_request``."""
    resolver = _RecordingResolver()
    client = _client(ChatApproveState(resolver=resolver))

    resp = client.post(
        "/v1/chat/completions/turn-abc/approve",
        json={"call_id": "call_abc123", "approved": False},
    )

    assert resp.status_code == 400, resp.text
    assert resp.json() == {
        "error": "invalid_request",
        "message": "`deny_message` is required when approved=false",
    }
    assert resolver.calls == []


def test_returns_400_when_deny_message_is_whitespace_only() -> None:
    """The handler ``.strip()``s ``deny_message`` before checking, so
    a whitespace-only string is treated as missing. Locks that
    behaviour down."""
    resolver = _RecordingResolver()
    client = _client(ChatApproveState(resolver=resolver))

    resp = client.post(
        "/v1/chat/completions/turn-abc/approve",
        json={
            "call_id": "call_abc123",
            "approved": False,
            "deny_message": "   \t  ",
        },
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"] == "invalid_request"
    assert resolver.calls == []


# ─── 404: resolver raises NotFoundError ──────────────────────────────


def test_returns_404_when_resolver_raises_not_found() -> None:
    """``NotFoundError`` from the resolver is the dedicated "unknown
    call_id" signal — the handler converts it into the ``not_found``
    envelope (with ``resource``, ``call_id``, ``turn_id``)."""
    resolver = _RecordingResolver(raises=NotFoundError("no such call"))
    client = _client(ChatApproveState(resolver=resolver))

    resp = client.post(
        "/v1/chat/completions/turn-xyz/approve",
        json={"call_id": "call_missing", "approved": True},
    )

    assert resp.status_code == 404, resp.text
    assert resp.json() == {
        "error": "not_found",
        "resource": "approval",
        "call_id": "call_missing",
        "turn_id": "turn-xyz",
    }
    # The resolver was actually invoked exactly once.
    assert len(resolver.calls) == 1
    forwarded_call_id, forwarded_decision = resolver.calls[0]
    assert forwarded_call_id == "call_missing"
    assert forwarded_decision.kind == "approved"


# ─── 500: resolver raises a generic exception ────────────────────────


def test_returns_500_when_resolver_raises_generic_exception() -> None:
    """Any non-``NotFoundError`` exception from the resolver becomes
    a 500 ``approve_failed`` envelope carrying ``str(exc)`` as the
    message (matches the Rust ``Err(_)`` fallback)."""
    resolver = _RecordingResolver(raises=RuntimeError("queue unreachable"))
    client = _client(ChatApproveState(resolver=resolver))

    resp = client.post(
        "/v1/chat/completions/turn-abc/approve",
        json={"call_id": "call_abc123", "approved": True},
    )

    assert resp.status_code == 500, resp.text
    assert resp.json() == {
        "error": "approve_failed",
        "message": "queue unreachable",
    }


# ─── 200: happy paths ────────────────────────────────────────────────


def test_returns_200_on_approve_and_forwards_decision() -> None:
    """The canonical happy path: a valid approve body returns the
    :class:`ApproveResponse` shape and the resolver receives an
    :class:`ApprovalDecision` whose ``kind`` is ``approved``."""
    resolver = _RecordingResolver()
    client = _client(ChatApproveState(resolver=resolver))

    resp = client.post(
        "/v1/chat/completions/turn-happy/approve",
        json={
            "call_id": "call_abc123",
            "approved": True,
            "scope": "session",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "turn_id": "turn-happy",
        "call_id": "call_abc123",
        "decision": "approved",
        "scope": "session",
    }

    # Resolver got called exactly once with the stripped call_id +
    # an ``approved`` decision (empty reason).
    assert len(resolver.calls) == 1
    forwarded_call_id, forwarded_decision = resolver.calls[0]
    assert forwarded_call_id == "call_abc123"
    assert forwarded_decision == ApprovalDecision(kind="approved", reason="")


def test_returns_200_on_deny_and_forwards_reason() -> None:
    """A valid deny body (``approved=false`` + non-blank
    ``deny_message``) returns ``decision == "denied"`` and the
    resolver receives an :class:`ApprovalDecision` whose ``reason``
    is the supplied ``deny_message``."""
    resolver = _RecordingResolver()
    client = _client(ChatApproveState(resolver=resolver))

    resp = client.post(
        "/v1/chat/completions/turn-deny/approve",
        json={
            "call_id": "call_deny_me",
            "approved": False,
            "deny_message": "policy: tool disabled by operator",
            "scope": "once",
        },
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "turn_id": "turn-deny",
        "call_id": "call_deny_me",
        "decision": "denied",
        "scope": "once",
    }

    assert len(resolver.calls) == 1
    forwarded_call_id, forwarded_decision = resolver.calls[0]
    assert forwarded_call_id == "call_deny_me"
    assert forwarded_decision == ApprovalDecision(
        kind="denied", reason="policy: tool disabled by operator"
    )


def test_call_id_is_stripped_before_forwarding() -> None:
    """The handler ``.strip()``s ``call_id`` before forwarding (and
    before the empty-check). Surrounding whitespace must not change
    routing — the resolver sees the trimmed id and the response
    echoes the trimmed id too."""
    resolver = _RecordingResolver()
    client = _client(ChatApproveState(resolver=resolver))

    resp = client.post(
        "/v1/chat/completions/turn-trim/approve",
        json={"call_id": "  call_trim_me  ", "approved": True},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["call_id"] == "call_trim_me"
    assert resolver.calls[0][0] == "call_trim_me"


def test_scope_omitted_returns_null_scope() -> None:
    """``scope`` is optional in the request and is echoed verbatim
    (``None`` → JSON ``null``) on the response. Guards against
    accidental defaulting (e.g. someone substituting ``"once"``)."""
    resolver = _RecordingResolver()
    client = _client(ChatApproveState(resolver=resolver))

    resp = client.post(
        "/v1/chat/completions/turn-noscope/approve",
        json={"call_id": "call_abc123", "approved": True},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"] is None
