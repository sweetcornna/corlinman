"""W6 — scheduler ``qzone.daily_publish`` builtin contract tests.

Asserts the action drives a fake :class:`ChatService` end-to-end:

* happy path — agent calls ``qzone_publish``, the audit dict carries
  ``tid`` + ``qzone_url`` harvested from the tool result envelope;
* negative path — agent finishes without calling ``qzone_publish`` →
  ``error="qzone_not_called"`` and the ``tools_called`` list shows
  what the model did instead;
* explicit failure envelope — ``qzone_publish`` ran but returned
  ``ok=false`` → ``error="qzone_failed"``;
* persona resolution failures (no store, missing id) bubble out as
  typed envelopes rather than raising;
* registration — the builtin is wired into the shared registry at
  import time, so the scheduler tick loop can resolve it by name.

The test fixtures stub ``PersonaStore`` + a minimal chat backend so
nothing touches sqlite / the network. The fake ``ChatService`` yields
events of the same shape :class:`InternalChatEvent` would have on the
wire — the action reads them via the same kind-discriminator the
production code uses.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_server.gateway_api.types import (
    DoneEvent,
    ErrorEvent,
    InternalChatError,
    ToolCallEvent,
)
from corlinman_server.scheduler.builtins import (
    BUILTIN_ACTIONS,
    BuiltinContext,
    _qzone_daily_publish_action,
    run_builtin,
)
from corlinman_server.scheduler.builtins.qzone_daily import (
    QZONE_DAILY_BUILTIN_NAME,
)

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class _FakePersona:
    """Minimal :class:`Persona` stand-in. The builtin only reads
    :attr:`system_prompt`, so a one-field stub is plenty."""

    id: str
    system_prompt: str
    display_name: str = "Test Persona"
    short_summary: str = ""
    is_builtin: bool = False
    created_at_ms: int = 0
    updated_at_ms: int = 0


class _FakePersonaStore:
    """Async stub matching :meth:`PersonaStore.get`."""

    def __init__(self, personas: dict[str, _FakePersona]) -> None:
        self._personas = personas
        self.gets: list[str] = []

    async def get(self, persona_id: str) -> _FakePersona | None:
        self.gets.append(persona_id)
        return self._personas.get(persona_id)

    async def close(self) -> None:  # pragma: no cover — never owned
        return None


class _ScriptedChatService:
    """A :class:`ChatService` stub that yields a pre-recorded event list.

    Each entry is the raw event object — :class:`ToolCallEvent`,
    :class:`ToolResultEvent`, :class:`DoneEvent` etc. The builtin
    reads them via :func:`_event_kind` / :func:`_event_field` so the
    real wire types work directly.

    The stub records the request passed to :meth:`run` so a test can
    assert on the composed system prompt + session key shape.
    """

    def __init__(self, events: list[Any]) -> None:
        self._events = events
        self.requests: list[Any] = []
        self.cancels: list[asyncio.Event] = []

    def run(self, req: Any, cancel: asyncio.Event):
        self.requests.append(req)
        self.cancels.append(cancel)
        events = list(self._events)

        async def _gen():
            for ev in events:
                yield ev

        return _gen()


def _make_app_state(
    *,
    chat: Any | None,
    persona_store: Any | None,
    metadata: dict[str, Any] | None = None,
) -> SimpleNamespace:
    """Build the AppState bundle the builtin probes.

    The builtin reaches for ``chat`` / ``persona_store`` /
    ``qzone_daily_metadata`` directly so a SimpleNamespace is enough.
    """
    return SimpleNamespace(
        chat=chat,
        persona_store=persona_store,
        persona_asset_store=None,
        qzone_daily_metadata=metadata or {},
    )


def _qzone_tool_call(
    *, call_id: str = "call-1", args: dict[str, Any] | None = None
) -> ToolCallEvent:
    """Mint a :class:`ToolCallEvent` for ``qzone_publish``."""
    import json

    payload = args or {"text": "today!"}
    return ToolCallEvent(
        plugin="corlinman_agent.qzone",
        tool="qzone_publish",
        args_json=json.dumps(payload).encode("utf-8"),
        call_id=call_id,
    )


class _ToolResultWithPayload(SimpleNamespace):
    """Duck-typed stand-in for :class:`ToolResultEvent` that carries a
    sidecar ``payload`` attribute.

    The real :class:`ToolResultEvent` is a frozen + slotted dataclass,
    so test code can't stamp ad-hoc attributes onto it. The builtin's
    :func:`_harvest_envelope` helper reads the event via plain
    :func:`getattr` (it tolerates dataclass / pydantic / dict / stub
    shapes interchangeably) so a :class:`SimpleNamespace` with the
    right fields is wire-equivalent for the test.
    """


def _qzone_tool_result(
    *,
    call_id: str = "call-1",
    is_error: bool = False,
    payload: dict[str, Any] | None = None,
    error_summary: str = "",
) -> Any:
    """Mint a tool-result event carrying a publish envelope.

    The harvester probes a small set of sidecar attribute names
    (``payload`` / ``payload_json`` / ``result`` / ``envelope``). We
    park the dict on ``payload`` so the happy path exercises the
    primary harvest branch.
    """
    return _ToolResultWithPayload(
        kind="tool_result",
        plugin="corlinman_agent.qzone",
        tool="qzone_publish",
        call_id=call_id,
        duration_ms=42,
        is_error=is_error,
        error_summary=error_summary,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_qzone_daily_is_registered_by_name() -> None:
    """Importing the builtins package registers ``qzone.daily_publish``
    so the scheduler tick loop can resolve it from the registry."""
    assert QZONE_DAILY_BUILTIN_NAME in BUILTIN_ACTIONS
    assert BUILTIN_ACTIONS[QZONE_DAILY_BUILTIN_NAME] is _qzone_daily_publish_action


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_happy_path_records_tid_and_qzone_url() -> None:
    """When the agent calls ``qzone_publish`` and the tool result carries
    a publish envelope, the audit dict surfaces ``tid`` + ``qzone_url``."""
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="You are a tiger.")}
    )
    chat = _ScriptedChatService(
        events=[
            _qzone_tool_call(),
            _qzone_tool_result(
                payload={
                    "ok": True,
                    "tid": "tid-abc",
                    "qzone_url": "https://user.qzone.qq.com/1234/mood/tid-abc",
                    "uin": "1234",
                    "images": 1,
                    "generated": False,
                },
            ),
            DoneEvent(finish_reason="stop"),
        ],
    )
    metadata = {
        "persona_id": "grantley",
        "prompt_template": "Write today's update.",
        "qq_account": "1234",
    }
    ctx = BuiltinContext(
        app_state=_make_app_state(
            chat=chat, persona_store=store, metadata=metadata
        ),
        name="grantley.daily_qzone",
    )
    out = await _qzone_daily_publish_action(ctx)

    assert out["ok"] is True, out
    assert out["tid"] == "tid-abc"
    assert out["qzone_url"] == "https://user.qzone.qq.com/1234/mood/tid-abc"
    assert out["uin"] == "1234"
    assert out["images"] == 1
    assert out["generated"] is False
    assert out["persona_id"] == "grantley"
    assert out["qq_account"] == "1234"
    assert any("qzone_publish" in name for name in out["tools_called"])
    # Composed request: system prompt has the persona body + the
    # scheduler tail, session key carries the scheduler scope.
    assert len(chat.requests) == 1
    req = chat.requests[0]
    messages = req.messages
    assert messages[0].role == "system"
    assert "You are a tiger." in messages[0].content
    assert "qzone_publish" in messages[0].content
    assert messages[1].role == "user"
    assert messages[1].content == "Write today's update."
    assert req.session_key.startswith("scheduler:qzone:grantley:")


# ---------------------------------------------------------------------------
# Negative paths
# ---------------------------------------------------------------------------


async def test_agent_never_calls_qzone_returns_typed_error() -> None:
    """If the agent finishes its turn without calling ``qzone_publish``
    the audit dict surfaces ``error="qzone_not_called"`` and records
    whatever tools the agent *did* call so the operator can debug."""
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="You are a tiger.")}
    )
    # Agent calls ``ask_user`` instead of ``qzone_publish`` — common
    # failure mode where the model second-guesses the prompt.
    other_call = ToolCallEvent(
        plugin="builtin",
        tool="ask_user",
        args_json=b'{"question":"who?"}',
        call_id="call-9",
    )
    chat = _ScriptedChatService(
        events=[other_call, DoneEvent(finish_reason="stop")]
    )
    metadata = {
        "persona_id": "grantley",
        "prompt_template": "Write today's update.",
    }
    ctx = BuiltinContext(
        app_state=_make_app_state(
            chat=chat, persona_store=store, metadata=metadata
        ),
        name="grantley.daily_qzone",
    )
    out = await _qzone_daily_publish_action(ctx)

    assert out["ok"] is False
    assert out["error"] == "qzone_not_called"
    assert out["tools_called"] == ["builtin.ask_user"]
    assert out["finish_reason"] == "stop"
    assert out["persona_id"] == "grantley"


async def test_qzone_failed_envelope_surfaces_inner_error() -> None:
    """When ``qzone_publish`` returns ``ok=false`` the audit dict
    folds the inner error code + message into the operator-visible
    fields rather than masquerading as a success."""
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="be a tiger.")}
    )
    chat = _ScriptedChatService(
        events=[
            _qzone_tool_call(),
            _qzone_tool_result(
                payload={
                    "ok": False,
                    "error": "qzone_cookie_stale",
                    "message": "p_skey not found",
                },
                # ToolResultEvent.is_error stays False — the dispatcher
                # returns ``ok=false`` in the envelope, not via the
                # event-level flag; the action must read both.
            ),
            DoneEvent(finish_reason="stop"),
        ],
    )
    metadata = {
        "persona_id": "grantley",
        "prompt_template": "Write today's update.",
    }
    ctx = BuiltinContext(
        app_state=_make_app_state(
            chat=chat, persona_store=store, metadata=metadata
        ),
        name="grantley.daily_qzone",
    )
    out = await _qzone_daily_publish_action(ctx)
    assert out["ok"] is False
    assert out["error"] == "qzone_failed"
    assert out["inner_error"] == "qzone_cookie_stale"
    assert out["inner_message"] == "p_skey not found"


async def test_chat_service_error_event_bubbles_into_audit() -> None:
    """An :class:`ErrorEvent` off the chat stream must not raise. The
    action wraps the reason + message into the audit dict."""
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="be a tiger.")}
    )
    err = InternalChatError(reason="rate_limit", message="too many requests")
    chat = _ScriptedChatService(events=[ErrorEvent(error=err)])
    metadata = {
        "persona_id": "grantley",
        "prompt_template": "x",
    }
    ctx = BuiltinContext(
        app_state=_make_app_state(
            chat=chat, persona_store=store, metadata=metadata
        ),
        name="grantley.daily_qzone",
    )
    out = await _qzone_daily_publish_action(ctx)
    assert out["ok"] is False
    assert out["error"] == "chat_error"
    assert out["chat_error_reason"] == "rate_limit"
    assert out["chat_error_message"] == "too many requests"


async def test_missing_persona_id_returns_typed_envelope() -> None:
    """A job metadata block that forgot ``persona_id`` surfaces a clear
    operator error without touching the persona store."""
    store = _FakePersonaStore({})
    chat = _ScriptedChatService(events=[])
    ctx = BuiltinContext(
        app_state=_make_app_state(
            chat=chat,
            persona_store=store,
            metadata={"prompt_template": "x"},
        ),
        name="grantley.daily_qzone",
    )
    out = await _qzone_daily_publish_action(ctx)
    assert out == {
        "persona_id": "",
        "qq_account": None,
        "ok": False,
        "error": "missing_persona_id",
    }
    # The store is never queried when persona_id is missing.
    assert store.gets == []


async def test_missing_prompt_template_returns_typed_envelope() -> None:
    store = _FakePersonaStore({})
    chat = _ScriptedChatService(events=[])
    ctx = BuiltinContext(
        app_state=_make_app_state(
            chat=chat,
            persona_store=store,
            metadata={"persona_id": "grantley"},
        ),
        name="grantley.daily_qzone",
    )
    out = await _qzone_daily_publish_action(ctx)
    assert out["ok"] is False
    assert out["error"] == "missing_prompt_template"


async def test_persona_not_found_returns_typed_envelope() -> None:
    store = _FakePersonaStore({})  # empty
    chat = _ScriptedChatService(events=[])
    ctx = BuiltinContext(
        app_state=_make_app_state(
            chat=chat,
            persona_store=store,
            metadata={"persona_id": "ghost", "prompt_template": "x"},
        ),
        name="grantley.daily_qzone",
    )
    out = await _qzone_daily_publish_action(ctx)
    assert out["ok"] is False
    assert out["error"] == "persona_not_found"
    assert store.gets == ["ghost"]


async def test_no_chat_service_returns_typed_envelope() -> None:
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="x")}
    )
    ctx = BuiltinContext(
        app_state=_make_app_state(
            chat=None,
            persona_store=store,
            metadata={"persona_id": "grantley", "prompt_template": "x"},
        ),
        name="grantley.daily_qzone",
    )
    out = await _qzone_daily_publish_action(ctx)
    assert out["ok"] is False
    assert out["error"] == "chat_service_unavailable"


# ---------------------------------------------------------------------------
# run_builtin indirection
# ---------------------------------------------------------------------------


async def test_run_builtin_indirection_passes_through() -> None:
    """End-to-end through the registry entry point — the same shape the
    scheduler dispatcher will see in production."""
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="x")}
    )
    chat = _ScriptedChatService(
        events=[
            _qzone_tool_call(),
            _qzone_tool_result(payload={"ok": True, "tid": "t1", "qzone_url": "u1"}),
            DoneEvent(finish_reason="stop"),
        ],
    )
    ctx = BuiltinContext(
        app_state=_make_app_state(
            chat=chat,
            persona_store=store,
            metadata={"persona_id": "grantley", "prompt_template": "x"},
        ),
        name="grantley.daily_qzone",
    )
    out = await run_builtin(QZONE_DAILY_BUILTIN_NAME, ctx)
    assert out["ok"] is True
    assert out["tid"] == "t1"
    assert out["qzone_url"] == "u1"


# ---------------------------------------------------------------------------
# Per-job metadata table resolution
# ---------------------------------------------------------------------------


async def test_per_job_metadata_table_overrides_default() -> None:
    """When ``app_state.scheduler_job_metadata[name]`` is present the
    action prefers it over the bare ``qzone_daily_metadata`` slot. This
    is the production wire-up the admin routes use to support multiple
    qzone jobs at once."""
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="x")}
    )
    chat = _ScriptedChatService(
        events=[
            _qzone_tool_call(),
            _qzone_tool_result(payload={"ok": True, "tid": "t9", "qzone_url": "u9"}),
            DoneEvent(finish_reason="stop"),
        ],
    )
    app_state = _make_app_state(chat=chat, persona_store=store, metadata={})
    app_state.scheduler_job_metadata = {
        "grantley.daily_qzone": {
            "persona_id": "grantley",
            "prompt_template": "scoped prompt",
            "qq_account": "9999",
        }
    }
    ctx = BuiltinContext(
        app_state=app_state, name="grantley.daily_qzone"
    )
    out = await _qzone_daily_publish_action(ctx)
    assert out["ok"] is True
    assert out["qq_account"] == "9999"
    # The user-turn should match the per-job metadata, not the global slot.
    assert chat.requests[0].messages[1].content == "scoped prompt"


# ---------------------------------------------------------------------------
# Timeout — bound the chat-drive so a wedged backend can't park the loop
# ---------------------------------------------------------------------------


async def test_chat_timeout_returns_typed_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stream that never emits a terminal event must time out and
    return a clean error envelope rather than hang forever."""

    class _NeverEnding:
        def run(self, req: Any, cancel: asyncio.Event):
            async def _gen():
                # First event so the loop has something to consume,
                # then sleep indefinitely.
                yield _qzone_tool_call()
                await asyncio.Event().wait()
                # Unreachable.
                yield DoneEvent(finish_reason="stop")  # pragma: no cover

            return _gen()

    monkeypatch.setenv("CORLINMAN_QZONE_DAILY_TIMEOUT_SECS", "1")

    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="x")}
    )
    chat = _NeverEnding()
    ctx = BuiltinContext(
        app_state=_make_app_state(
            chat=chat,
            persona_store=store,
            metadata={"persona_id": "grantley", "prompt_template": "x"},
        ),
        name="grantley.daily_qzone",
    )
    out = await _qzone_daily_publish_action(ctx)
    assert out["ok"] is False
    assert out["error"] == "chat_timeout"
    assert any("qzone_publish" in name for name in out["tools_called"])
