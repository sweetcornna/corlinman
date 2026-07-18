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
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_server.gateway_api.types import (
    DoneEvent,
    ErrorEvent,
    InternalChatError,
    ToolCallEvent,
    ToolResultEvent,
)
from corlinman_server.scheduler.builtins import (
    BUILTIN_ACTIONS,
    BuiltinContext,
    _qzone_daily_publish_action,
    run_builtin,
)
from corlinman_server.scheduler.builtins.qzone_daily import (
    QZONE_DAILY_BUILTIN_NAME,
    QZONE_DAILY_DIVERSITY_TAIL,
    _compose_system_prompt,
    _post_log_path,
    _read_post_log,
    _record_post_log,
    _resolve_image_ref_labels,
    _resolve_recent_posts_block,
    _valid_persona_slug,
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


def _qzone_tool_result(
    *,
    call_id: str = "call-1",
    is_error: bool = False,
    payload: dict[str, Any] | None = None,
    error_summary: str = "",
) -> ToolResultEvent:
    """Mint a real :class:`ToolResultEvent` carrying a publish envelope.

    The gateway forwards the tool's parsed result envelope as a JSON
    string on ``ToolResultEvent.payload_json``; the builtin's
    :func:`_harvest_envelope` decodes it to recover ``tid`` /
    ``qzone_url``. We JSON-encode ``payload`` onto that field so the
    test exercises the real wire type + the primary harvest branch.
    """
    import json

    return ToolResultEvent(
        plugin="corlinman_agent.qzone",
        tool="qzone_publish",
        call_id=call_id,
        duration_ms=42,
        is_error=is_error,
        error_summary=error_summary,
        payload_json=json.dumps(payload) if payload is not None else "",
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
    # Published body — falls back to the decoded input args (intent)
    # when the envelope doesn't echo it. Surfaced for post-log.
    assert out["text"] == "today!"
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


async def test_harvest_result_overrides_input_args() -> None:
    """The envelope is the union of the decoded input args and the
    harvested result — result fields win. Here the tool published a
    normalized ``text`` different from the raw input, and returns the
    real ``tid`` / ``qzone_url``; all three come from the result."""
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="x")}
    )
    chat = _ScriptedChatService(
        events=[
            _qzone_tool_call(args={"text": "draft"}),
            _qzone_tool_result(
                payload={
                    "ok": True,
                    "tid": "tid-real",
                    "qzone_url": "https://qzone.test/mood/tid-real",
                    "text": "published body",
                },
            ),
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
    out = await _qzone_daily_publish_action(ctx)
    assert out["ok"] is True, out
    assert out["tid"] == "tid-real"
    assert out["qzone_url"] == "https://qzone.test/mood/tid-real"
    # Result-carried ``text`` overrides the raw input-arg ``text``.
    assert out["text"] == "published body"


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


# ---------------------------------------------------------------------------
# R4 regression: persona_id forwarded into InternalChatRequest (B2 fix)
# ---------------------------------------------------------------------------


async def test_internal_chat_request_carries_persona_id() -> None:
    """scheduler-fired qzone turn must bind persona_id on the request.

    R4 root-cause: ``_build_internal_chat_request`` constructed the
    request without ``persona_id``, so ``ChatStart.extra["persona_id"]``
    was absent — the agent servicer saw no bound persona and
    ``image_with_refs`` / persona-life tools silently fell back to
    requiring an explicit model arg. Regression asserts the field is
    wired through.
    """
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="You are a tiger.")}
    )
    chat = _ScriptedChatService(
        events=[
            _qzone_tool_call(),
            _qzone_tool_result(
                payload={
                    "ok": True,
                    "tid": "tid-r4",
                    "qzone_url": "https://user.qzone.qq.com/1234/mood/tid-r4",
                    "uin": "1234",
                    "images": 0,
                    "generated": False,
                },
            ),
            DoneEvent(finish_reason="stop"),
        ],
    )
    metadata = {
        "persona_id": "grantley",
        "prompt_template": "Post daily update.",
    }
    ctx = BuiltinContext(
        app_state=_make_app_state(
            chat=chat, persona_store=store, metadata=metadata
        ),
        name="grantley.daily_qzone",
    )
    out = await _qzone_daily_publish_action(ctx)

    assert out["ok"] is True, out
    assert len(chat.requests) == 1
    req = chat.requests[0]
    # R4 fix: persona_id must be present on the request so the agent
    # servicer wires ChatStart.extra["persona_id"] for persona binding.
    assert req.persona_id == "grantley", (
        "InternalChatRequest.persona_id must be forwarded from job metadata "
        "(R4 regression: was None before B2 fix)"
    )


# ---------------------------------------------------------------------------
# B2: life-rhythm signals folded into the daily-post system prompt
# ---------------------------------------------------------------------------


class _FakeStateStore:
    """Async stub for the runtime persona-state store (``agent_state.sqlite``).

    ``_resolve_life_block`` strategy 2 calls ``get(persona_id)`` and reads
    ``row.state_json``; a one-method stub returning a fixed row is enough."""

    def __init__(self, state_json: dict[str, Any]) -> None:
        self._row = SimpleNamespace(state_json=state_json)

    async def get(self, persona_id: str) -> Any:
        return self._row


async def test_system_prompt_carries_life_rhythm_nudge() -> None:
    """When the persona's runtime life doc shows it hasn't been out in a
    long while, the composed system prompt carries the "节奏" lines plus the
    priority "生活节奏提示" nudge (B2 — hermes life-signals port)."""
    now = datetime.now(UTC).astimezone()
    # at_academy since 2 days ago; last returned from a mission 20 days ago
    # → days_since_last_outing = 20 ≥ 13 → HIGH go_out nudge.
    life_doc = {
        "life": {
            "current": {
                "state": "at_academy",
                "location": "据点",
                "activity": "训练",
                "since": (now - timedelta(days=2)).isoformat(timespec="seconds"),
            },
            "history": [
                {
                    "ts": (now - timedelta(days=20)).isoformat(timespec="seconds"),
                    "from": {"state": "on_mission"},
                    "to": {"state": "at_academy"},
                    "reason": "回据点",
                }
            ],
        },
        "diary": [],
    }
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="You are a knight.")}
    )
    chat = _ScriptedChatService(
        events=[
            _qzone_tool_call(),
            _qzone_tool_result(payload={"ok": True, "tid": "t", "qzone_url": "u"}),
            DoneEvent(finish_reason="stop"),
        ],
    )
    app_state = _make_app_state(
        chat=chat,
        persona_store=store,
        metadata={"persona_id": "grantley", "prompt_template": "写今天的说说。"},
    )
    # Strategy 2 of _resolve_life_block reads this runtime state store; no
    # persona_resolver is set so strategy 1 is skipped.
    app_state.corlinman_persona_state_store = _FakeStateStore(life_doc)
    ctx = BuiltinContext(app_state=app_state, name="grantley.daily_qzone")

    out = await _qzone_daily_publish_action(ctx)
    assert out["ok"] is True, out

    system_prompt = chat.requests[0].messages[0].content
    # The two rhythm lines + the priority nudge line all landed.
    assert "当前状态已持续：2 天" in system_prompt
    assert "距上次外出：20 天" in system_prompt
    assert "生活节奏提示（优先响应）" in system_prompt
    # And the base persona body is still there.
    assert "You are a knight." in system_prompt


async def test_life_block_absent_when_no_state_store() -> None:
    """No runtime state store + no resolver → the daily post composes from
    the bare persona body, with no '生活节奏' block (best-effort omission)."""
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="You are a knight.")}
    )
    chat = _ScriptedChatService(
        events=[
            _qzone_tool_call(),
            _qzone_tool_result(payload={"ok": True, "tid": "t", "qzone_url": "u"}),
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
    out = await _qzone_daily_publish_action(ctx)
    assert out["ok"] is True
    system_prompt = chat.requests[0].messages[0].content
    assert "生活节奏提示" not in system_prompt
    assert "我最近的生活" not in system_prompt


# ---------------------------------------------------------------------------
# B4: anti-repeat post-log sidecar (4b) — write / cap / slug-guard / read
# ---------------------------------------------------------------------------


def test_post_log_write_then_read_roundtrip(tmp_path: Path) -> None:
    """A successful publish appends one record; the sidecar is the documented
    ``{version, posts:[{ts, job, tid, qzone_url, text}]}`` shape and reads
    back through the module reader."""
    _record_post_log(
        data_dir=tmp_path,
        persona_id="grantley",
        job="grantley.daily_qzone",
        result={"text": "今天去了海边", "tid": "t1", "qzone_url": "u1"},
    )
    path = tmp_path / "qzone_post_log" / "grantley.json"
    assert path.is_file()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert len(raw["posts"]) == 1
    entry = raw["posts"][0]
    assert entry["text"] == "今天去了海边"
    assert entry["tid"] == "t1"
    assert entry["qzone_url"] == "u1"
    assert entry["job"] == "grantley.daily_qzone"
    assert isinstance(entry["ts"], str) and entry["ts"]
    # The module reader (used by the recent-posts block) recovers it.
    posts = _read_post_log(path)
    assert len(posts) == 1 and posts[0]["text"] == "今天去了海边"


def test_post_log_caps_at_30(tmp_path: Path) -> None:
    """Only the most-recent 30 posts survive; older ones roll off."""
    for i in range(35):
        _record_post_log(
            data_dir=tmp_path, persona_id="grantley", job="j",
            result={"text": f"post-{i}"},
        )
    posts = _read_post_log(tmp_path / "qzone_post_log" / "grantley.json")
    assert len(posts) == 30
    assert posts[-1]["text"] == "post-34"  # newest kept
    assert posts[0]["text"] == "post-5"    # posts 0-4 dropped


def test_post_log_text_capped_at_500(tmp_path: Path) -> None:
    """The stored body is truncated to 500 chars."""
    _record_post_log(
        data_dir=tmp_path, persona_id="grantley", job="j",
        result={"text": "x" * 900},
    )
    posts = _read_post_log(tmp_path / "qzone_post_log" / "grantley.json")
    assert len(posts[0]["text"]) == 500


def test_post_log_bad_slug_skips_entirely(tmp_path: Path) -> None:
    """A traversal-y persona_id fails the slug guard → no path, no file, no
    crash (the whole feature is skipped for that persona)."""
    assert not _valid_persona_slug("../evil")
    assert _post_log_path(tmp_path, "../evil") is None
    _record_post_log(
        data_dir=tmp_path, persona_id="../evil", job="j",
        result={"text": "nope"},
    )
    log_dir = tmp_path / "qzone_post_log"
    assert not log_dir.exists() or list(log_dir.iterdir()) == []


def test_post_log_no_data_dir_is_noop() -> None:
    """No data dir wired → the write is a silent no-op (never raises)."""
    assert _post_log_path(None, "grantley") is None
    _record_post_log(
        data_dir=None, persona_id="grantley", job="j", result={"text": "x"}
    )  # must not raise


def test_post_log_atomic_write_leaves_no_tmp(tmp_path: Path) -> None:
    """The atomic ``tmp + replace`` dance leaves only the final file."""
    _record_post_log(
        data_dir=tmp_path, persona_id="grantley", job="j", result={"text": "a"}
    )
    log_dir = tmp_path / "qzone_post_log"
    names = sorted(p.name for p in log_dir.iterdir())
    assert names == ["grantley.json"]  # no leftover .json.new


# ---------------------------------------------------------------------------
# B4: recent-posts prompt block (4b)
# ---------------------------------------------------------------------------


def test_recent_posts_block_lists_excerpts(tmp_path: Path) -> None:
    for txt in ["第一条说说", "第二条说说", "第三条说说"]:
        _record_post_log(
            data_dir=tmp_path, persona_id="grantley", job="j",
            result={"text": txt},
        )
    block = _resolve_recent_posts_block(tmp_path, "grantley", 7)
    assert block is not None
    assert "最近已发过的说说" in block
    assert "禁止重复主题/场景/句式" in block
    assert "第一条说说" in block and "第三条说说" in block


def test_recent_posts_block_honors_n(tmp_path: Path) -> None:
    for i in range(10):
        _record_post_log(
            data_dir=tmp_path, persona_id="grantley", job="j",
            result={"text": f"说说{i}"},
        )
    block = _resolve_recent_posts_block(tmp_path, "grantley", 3)
    assert block is not None
    assert "说说9" in block and "说说8" in block and "说说7" in block
    assert "说说6" not in block  # older than the last 3 → omitted


def test_recent_posts_block_none_when_empty(tmp_path: Path) -> None:
    assert _resolve_recent_posts_block(tmp_path, "grantley", 7) is None
    assert _resolve_recent_posts_block(None, "grantley", 7) is None


# ---------------------------------------------------------------------------
# B4: end-to-end assembly — seed (4a) + recent (4b) + diversity tail (4c)
# ---------------------------------------------------------------------------


async def test_diversity_on_composes_seed_recent_and_tail(tmp_path: Path) -> None:
    """diversity on (the default): the composed system prompt carries the
    inspiration-seed block, the recent-posts block, and the anti-formulaic
    tail — in that assembly order — and a post-log entry is appended."""
    # Seed a prior post so the recent-posts block is non-empty.
    _record_post_log(
        data_dir=tmp_path, persona_id="grantley", job="j",
        result={"text": "昨天在食堂吃饭很无聊"},
    )
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="You are a knight.")}
    )
    chat = _ScriptedChatService(
        events=[
            _qzone_tool_call(),
            _qzone_tool_result(
                payload={"ok": True, "tid": "t", "qzone_url": "u", "text": "今天新说说"}
            ),
            DoneEvent(finish_reason="stop"),
        ],
    )
    app_state = _make_app_state(
        chat=chat, persona_store=store,
        metadata={"persona_id": "grantley", "prompt_template": "写今天的说说。"},
    )
    app_state.data_dir = tmp_path
    ctx = BuiltinContext(app_state=app_state, name="grantley.daily_qzone")
    out = await _qzone_daily_publish_action(ctx)
    assert out["ok"] is True, out

    prompt = chat.requests[0].messages[0].content
    # Seed block (4a) + recent-posts block (4b) both present.
    assert "今日灵感种子" in prompt
    assert "最近已发过的说说" in prompt
    assert "昨天在食堂吃饭很无聊" in prompt
    # Anti-repeat requirement lines from the tail (4c).
    assert "主题、场景、开头句式都必须" in prompt
    assert "至少要换一个新" in prompt
    assert "persona_life_set_state" in prompt
    # Assembly order: persona body → seed → recent → tail.
    body_idx = prompt.index("You are a knight.")
    seed_idx = prompt.index("今日灵感种子")
    recent_idx = prompt.index("最近已发过的说说")
    tail_idx = prompt.index("scheduler·qzone.daily_publish")
    assert body_idx < seed_idx < recent_idx < tail_idx

    # 4b: this run's publish appended a second post-log entry.
    posts = _read_post_log(tmp_path / "qzone_post_log" / "grantley.json")
    assert len(posts) == 2
    assert posts[-1]["text"] == "今天新说说"


async def test_diversity_off_rolls_back_everything(tmp_path: Path) -> None:
    """diversity=False rolls back to pre-B4 behavior: no seed / recent blocks,
    the plain publish tail, and NO post-log write."""
    _record_post_log(
        data_dir=tmp_path, persona_id="grantley", job="j",
        result={"text": "旧说说"},
    )
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="You are a knight.")}
    )
    chat = _ScriptedChatService(
        events=[
            _qzone_tool_call(),
            _qzone_tool_result(
                payload={"ok": True, "tid": "t", "qzone_url": "u", "text": "新说说"}
            ),
            DoneEvent(finish_reason="stop"),
        ],
    )
    app_state = _make_app_state(
        chat=chat, persona_store=store,
        metadata={
            "persona_id": "grantley",
            "prompt_template": "x",
            "diversity": False,
        },
    )
    app_state.data_dir = tmp_path
    ctx = BuiltinContext(app_state=app_state, name="grantley.daily_qzone")
    out = await _qzone_daily_publish_action(ctx)
    assert out["ok"] is True

    prompt = chat.requests[0].messages[0].content
    assert "今日灵感种子" not in prompt
    assert "最近已发过的说说" not in prompt
    assert "主题、场景、开头句式都必须" not in prompt
    # The plain publish tail still ends the turn with qzone_publish.
    assert "qzone_publish" in prompt
    # No post-log write — still just the pre-seeded entry.
    posts = _read_post_log(tmp_path / "qzone_post_log" / "grantley.json")
    assert len(posts) == 1 and posts[0]["text"] == "旧说说"


def test_diversity_tail_full_text_requirements() -> None:
    """The exported diversity tail carries all three B4 requirements + still
    demands the turn end with a qzone_publish call."""
    tail = QZONE_DAILY_DIVERSITY_TAIL
    assert "qzone_publish" in tail
    assert "主题、场景、开头句式都必须和上面『最近已发过的说说』里的任何一条不同" in tail
    assert "至少要换一个新的切入点" in tail
    assert "persona_life_set_state" in tail
    assert "persona_life_event_seed" in tail
    # Deliberately avoids the life-block marker literal so the two don't
    # collide (uses 提醒, not 提示).
    assert "生活节奏提醒" in tail
    assert "生活节奏提示" not in tail


# ---------------------------------------------------------------------------
# B5: task-level image_ref_labels → reference-image system-prompt block
# ---------------------------------------------------------------------------


def test_image_ref_block_rides_after_both_tails() -> None:
    """A job that pinned ``image_ref_labels`` gets a reference-image block
    appended after the tail — for BOTH the diversity and the plain tail, so
    the feature is orthogonal to the diversity toggle. The block names the
    exact labels and steers toward a candid life-slice framing."""
    labels = ["grantley_home", "grantley_casual"]
    for diversity in (True, False):
        prompt = _compose_system_prompt(
            "You are a tiger.",
            diversity=diversity,
            image_ref_labels=labels,
        )
        assert "image_with_refs 的 characters 必须用这些参考图标签" in prompt
        assert "grantley_home" in prompt and "grantley_casual" in prompt
        assert "随手拍的生活切片而非摆拍合影" in prompt
        # The block sits AFTER whichever tail (tail marker precedes it).
        tail_idx = prompt.index("scheduler·qzone.daily_publish 指令")
        block_idx = prompt.index("配图参考图")
        assert tail_idx < block_idx


def test_no_image_ref_block_when_labels_absent_or_empty() -> None:
    """No labels (``None`` or an empty list) → no reference-image block.

    Asserts on the block-specific marker, NOT the bare ``image_with_refs``
    token — the plain tail already mentions ``image_with_refs`` as a配图 hint,
    so only the pinned-labels line must be absent."""
    for labels in (None, []):
        prompt = _compose_system_prompt("x", image_ref_labels=labels)
        assert "必须用这些参考图标签" not in prompt
        assert "配图参考图" not in prompt


def test_resolve_image_ref_labels_is_total_and_defensive() -> None:
    """The metadata reader cleans the list + tolerates junk (never raises)."""
    assert _resolve_image_ref_labels({"image_ref_labels": ["a", "b"]}) == ["a", "b"]
    # Blank / non-string entries are dropped; surrounding whitespace trimmed.
    assert _resolve_image_ref_labels(
        {"image_ref_labels": [" a ", "", 3, None, "b"]}
    ) == ["a", "b"]
    # Absent / wrong-shaped → empty list (block omitted).
    assert _resolve_image_ref_labels({}) == []
    assert _resolve_image_ref_labels({"image_ref_labels": "not-a-list"}) == []
    assert _resolve_image_ref_labels({"image_ref_labels": None}) == []


async def test_builtin_prompt_carries_image_ref_labels_from_metadata() -> None:
    """End-to-end: a job whose metadata pins ``image_ref_labels`` composes a
    system prompt carrying the reference-image block with those exact labels."""
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="You are a tiger.")}
    )
    chat = _ScriptedChatService(
        events=[
            _qzone_tool_call(),
            _qzone_tool_result(payload={"ok": True, "tid": "t", "qzone_url": "u"}),
            DoneEvent(finish_reason="stop"),
        ],
    )
    ctx = BuiltinContext(
        app_state=_make_app_state(
            chat=chat,
            persona_store=store,
            metadata={
                "persona_id": "grantley",
                "prompt_template": "写今天的说说。",
                "diversity": False,
                "image_ref_labels": ["grantley_home", "grantley_casual"],
            },
        ),
        name="grantley.daily_qzone",
    )
    out = await _qzone_daily_publish_action(ctx)
    assert out["ok"] is True, out
    prompt = chat.requests[0].messages[0].content
    assert "image_with_refs 的 characters 必须用这些参考图标签" in prompt
    assert "grantley_home" in prompt and "grantley_casual" in prompt


async def test_builtin_prompt_omits_ref_block_without_labels() -> None:
    """No ``image_ref_labels`` metadata → the daily post composes with no
    reference-image block (best-effort omission)."""
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="You are a tiger.")}
    )
    chat = _ScriptedChatService(
        events=[
            _qzone_tool_call(),
            _qzone_tool_result(payload={"ok": True, "tid": "t", "qzone_url": "u"}),
            DoneEvent(finish_reason="stop"),
        ],
    )
    ctx = BuiltinContext(
        app_state=_make_app_state(
            chat=chat,
            persona_store=store,
            metadata={
                "persona_id": "grantley",
                "prompt_template": "写今天的说说。",
                "diversity": False,
            },
        ),
        name="grantley.daily_qzone",
    )
    out = await _qzone_daily_publish_action(ctx)
    assert out["ok"] is True
    prompt = chat.requests[0].messages[0].content
    assert "必须用这些参考图标签" not in prompt
