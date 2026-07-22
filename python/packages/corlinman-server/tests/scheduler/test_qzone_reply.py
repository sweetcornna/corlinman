"""PR-B6 — scheduler ``qzone.reply_comments`` builtin contract tests.

Asserts the action drives a fake :class:`ChatService` end-to-end:

* happy path — agent lists its own feed, replies to a fresh comment via
  ``qzone_post_comment``; the audit dict counts ``replies_posted`` /
  ``tids_scanned`` and the seen-sidecar records the ``(tid, uin)`` pair;
* dedup — a pre-seeded sidecar entry surfaces in the system prompt's
  "已回复过" block and counts into ``skipped_seen``;
* failure folding — chat error events / a raising service / a missing
  persona all fold into typed audit envelopes, never exceptions;
* metadata clamps — ``max_replies`` / ``lookback_posts`` are read with
  defaults + clamped into the documented hard ranges;
* sidecar hygiene — per-tid + total-tid caps, slug traversal guard,
  atomic write leaving no tmp file;
* registration — the builtin is wired into the shared registry at
  import time.

The stubs mirror ``test_qzone_daily.py``'s: a scripted ChatService
yields real wire-typed events, and a one-method persona store stands in
for sqlite.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
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
    _qzone_reply_comments_action,
    run_builtin,
)
from corlinman_server.scheduler.builtins.qzone_reply import (
    QZONE_REPLY_BUILTIN_NAME,
    _clamp_int,
    _read_seen,
    _record_seen,
    _seen_block,
    _seen_path,
)

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


@dataclass
class _FakePersona:
    id: str
    system_prompt: str
    display_name: str = "Test Persona"


class _FakePersonaStore:
    def __init__(self, personas: dict[str, _FakePersona]) -> None:
        self._personas = personas
        self.gets: list[str] = []

    async def get(self, persona_id: str) -> _FakePersona | None:
        self.gets.append(persona_id)
        return self._personas.get(persona_id)

    async def close(self) -> None:  # pragma: no cover — never owned
        return None


class _ScriptedChatService:
    def __init__(self, events: list[Any]) -> None:
        self._events = events
        self.requests: list[Any] = []

    def run(self, req: Any, cancel: asyncio.Event):
        self.requests.append(req)
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
    return SimpleNamespace(
        chat=chat,
        persona_store=persona_store,
        persona_asset_store=None,
        qzone_reply_metadata=metadata or {},
    )


def _feed_events(
    *,
    my_uin: str = "1234",
    feed: list[dict[str, Any]] | None = None,
    call_id: str = "f1",
) -> list[Any]:
    """A ``qzone_list_feed`` call + successful result pair."""
    payload = {
        "ok": True,
        "my_uin": my_uin,
        "filter_owner_uin": None,
        "returned": len(feed or []),
        "feed": feed or [],
    }
    return [
        ToolCallEvent(
            plugin="corlinman_agent.qzone",
            tool="qzone_list_feed",
            args_json=b"{}",
            call_id=call_id,
        ),
        ToolResultEvent(
            plugin="corlinman_agent.qzone",
            tool="qzone_list_feed",
            call_id=call_id,
            duration_ms=5,
            payload_json=json.dumps(payload, ensure_ascii=False),
        ),
    ]


def _comment_events(
    *,
    call_id: str = "c1",
    tid: str = "t1",
    reply_to_uin: str = "5555",
    comment_id: str | None = None,
    ok: bool = True,
    is_error: bool = False,
) -> list[Any]:
    """A ``qzone_post_comment`` call + result pair."""
    args = {
        "owner_uin": "1234",
        "tid": tid,
        "content": "谢谢你来看我～",
        "reply_to_uin": reply_to_uin,
        "reply_to_name": "友人",
    }
    if comment_id is not None:
        args["reply_to_comment_id"] = comment_id
    payload: dict[str, Any]
    if ok:
        payload = {
            "ok": True,
            "owner_uin": "1234",
            "tid": tid,
            "is_reply": True,
            "comment_identity": f"id:{comment_id}" if comment_id else "",
            "content_sent": f"@{{uin:{reply_to_uin},nick:友人,who:1}} 谢谢你来看我～",
        }
    else:
        payload = {"ok": False, "error": "qzone_rejected", "message": "denied"}
    return [
        ToolCallEvent(
            plugin="corlinman_agent.qzone",
            tool="qzone_post_comment",
            args_json=json.dumps(args, ensure_ascii=False).encode("utf-8"),
            call_id=call_id,
        ),
        ToolResultEvent(
            plugin="corlinman_agent.qzone",
            tool="qzone_post_comment",
            call_id=call_id,
            duration_ms=5,
            is_error=is_error,
            payload_json=json.dumps(payload, ensure_ascii=False),
        ),
    ]


def _own_post(tid: str, *, uin: str = "1234", comments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "tid": tid,
        "uin": uin,
        "name": "格兰",
        "time": "1小时前",
        "content": f"说说 {tid}",
        "comments": comments or [],
    }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_qzone_reply_is_registered_by_name() -> None:
    assert QZONE_REPLY_BUILTIN_NAME in BUILTIN_ACTIONS
    assert (
        BUILTIN_ACTIONS[QZONE_REPLY_BUILTIN_NAME]
        is _qzone_reply_comments_action
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_happy_path_counts_replies_and_backfills_seen(
    tmp_path: Path,
) -> None:
    """A successful reply turn: audit counts, prompt contract, and the
    seen-sidecar backfill all land."""
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="You are a tiger.")}
    )
    feed = [
        _own_post("t1", comments=[{"id": "9", "uin": "5555", "name": "友人", "content": "好耶"}]),
        _own_post("t2"),
        _own_post("x1", uin="8888"),  # a friend's post — not scanned
    ]
    chat = _ScriptedChatService(
        events=[
            *_feed_events(feed=feed),
            *_comment_events(tid="t1", reply_to_uin="5555"),
            DoneEvent(finish_reason="stop"),
        ],
    )
    app_state = _make_app_state(
        chat=chat,
        persona_store=store,
        metadata={"persona_id": "grantley", "qq_account": "1234"},
    )
    app_state.data_dir = tmp_path
    ctx = BuiltinContext(app_state=app_state, name="grantley.qzone_reply")
    out = await _qzone_reply_comments_action(ctx)

    assert out["ok"] is True, out
    assert out["persona_id"] == "grantley"
    assert out["qq_account"] == "1234"
    assert out["replies_posted"] == 1
    # Two own posts in the feed (t1, t2) — the friend's x1 is excluded.
    assert out["tids_scanned"] == 2
    assert out["skipped_seen"] == 0
    assert "_replies" not in out
    assert any("qzone_post_comment" in name for name in out["tools_called"])

    # Sidecar backfill — v2 stores a stable identity plus timestamp.
    path = tmp_path / "qzone_seen_comments" / "grantley.json"
    assert path.is_file()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 2
    assert list(raw["seen"].keys()) == ["t1"]
    (entry,) = raw["seen"]["t1"]
    assert entry.startswith("uin:5555:")
    assert entry.rsplit(":", 1)[1].isdigit()

    # Prompt contract: persona body + the reply tail with the pinned
    # qq_account, the clamped knobs, and the wind-down rules.
    assert len(chat.requests) == 1
    req = chat.requests[0]
    system_prompt = req.messages[0].content
    assert "You are a tiger." in system_prompt
    assert "scheduler·qzone.reply_comments" in system_prompt
    assert 'owner_uin="1234"' in system_prompt
    assert "最近 5 条" in system_prompt          # lookback default
    assert "最多回复 3 条" in system_prompt       # max_replies default
    assert "不要调用 `qzone_publish`" in system_prompt
    assert req.messages[1].role == "user"
    assert req.session_key.startswith("scheduler:qzone:grantley:")
    assert req.persona_id == "grantley"


async def test_no_new_comments_is_success_with_zero_replies(
    tmp_path: Path,
) -> None:
    """A turn that lists the feed and ends without commenting is a
    success — no error, zero replies, no sidecar file."""
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="x")}
    )
    chat = _ScriptedChatService(
        events=[
            *_feed_events(feed=[_own_post("t1")]),
            DoneEvent(finish_reason="stop"),
        ],
    )
    app_state = _make_app_state(
        chat=chat, persona_store=store, metadata={"persona_id": "grantley"}
    )
    app_state.data_dir = tmp_path
    ctx = BuiltinContext(app_state=app_state, name="grantley.qzone_reply")
    out = await _qzone_reply_comments_action(ctx)
    assert out["ok"] is True, out
    assert out["replies_posted"] == 0
    assert out["tids_scanned"] == 1
    assert not (tmp_path / "qzone_seen_comments").exists()


async def test_shadow_comment_plans_are_not_recorded_as_seen(
    tmp_path: Path,
) -> None:
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="x")}
    )
    chat = _ScriptedChatService(
        events=[
            *_comment_events(
                tid="t1",
                reply_to_uin="5555",
                comment_id="comment-1",
            ),
            DoneEvent(finish_reason="stop"),
        ]
    )
    # Mark the tool envelope as a shadow-planned effect.
    shadow_result = chat._events[1]
    payload = json.loads(shadow_result.payload_json)
    payload["shadow"] = True
    shadow_result = ToolResultEvent(
        plugin=shadow_result.plugin,
        tool=shadow_result.tool,
        call_id=shadow_result.call_id,
        duration_ms=shadow_result.duration_ms,
        is_error=shadow_result.is_error,
        error_summary=shadow_result.error_summary,
        payload_json=json.dumps(payload),
    )
    chat._events[1] = shadow_result
    app_state = _make_app_state(
        chat=chat,
        persona_store=store,
        metadata={"persona_id": "grantley"},
    )
    app_state.data_dir = tmp_path
    out = await _qzone_reply_comments_action(
        BuiltinContext(
            app_state=app_state,
            name="grantley.qzone_reply",
            execution_mode="shadow",
        )
    )
    assert out["ok"] is True
    assert out["shadow"] is True
    assert out["delivery_suppressed"] is True
    assert not (tmp_path / "qzone_seen_comments").exists()
    assert chat.requests[0].scheduler_context["execution_mode"] == "shadow"


async def test_failed_comment_result_not_counted_or_recorded(
    tmp_path: Path,
) -> None:
    """An ``ok=false`` comment envelope (and an ``is_error`` result) must
    not count into ``replies_posted`` nor pollute the sidecar."""
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="x")}
    )
    chat = _ScriptedChatService(
        events=[
            *_comment_events(call_id="c1", tid="t1", reply_to_uin="5555", ok=False),
            *_comment_events(call_id="c2", tid="t2", reply_to_uin="6666", is_error=True),
            DoneEvent(finish_reason="stop"),
        ],
    )
    app_state = _make_app_state(
        chat=chat, persona_store=store, metadata={"persona_id": "grantley"}
    )
    app_state.data_dir = tmp_path
    ctx = BuiltinContext(app_state=app_state, name="grantley.qzone_reply")
    out = await _qzone_reply_comments_action(ctx)
    assert out["ok"] is True
    assert out["replies_posted"] == 0
    assert not (tmp_path / "qzone_seen_comments").exists()


# ---------------------------------------------------------------------------
# Dedup — seen sidecar → prompt block + skipped_seen
# ---------------------------------------------------------------------------


async def test_seen_entries_surface_in_prompt_and_skipped_count(
    tmp_path: Path,
) -> None:
    """Pre-seeded seen entries are injected into the system prompt's
    "已回复过" block and counted into ``skipped_seen``."""
    _record_seen(
        data_dir=tmp_path,
        persona_id="grantley",
        replies=[("t1", "5555"), ("t1", "7777"), ("t2", "8888")],
    )
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="x")}
    )
    chat = _ScriptedChatService(events=[DoneEvent(finish_reason="stop")])
    app_state = _make_app_state(
        chat=chat, persona_store=store, metadata={"persona_id": "grantley"}
    )
    app_state.data_dir = tmp_path
    ctx = BuiltinContext(app_state=app_state, name="grantley.qzone_reply")
    out = await _qzone_reply_comments_action(ctx)
    assert out["ok"] is True
    assert out["skipped_seen"] == 3

    prompt = chat.requests[0].messages[0].content
    assert "已回复过的评论" in prompt
    assert "说说 t1：已回复评论 5555、7777" in prompt
    assert "说说 t2：已回复评论 8888" in prompt
    # The seen block sits between the persona body and the tail.
    assert prompt.index("已回复过的评论") < prompt.index(
        "scheduler·qzone.reply_comments"
    )


async def test_seen_backfill_does_not_duplicate_legacy_uin(tmp_path: Path) -> None:
    """Replying again to a commenter already in the sidecar must not
    append a duplicate ``uin:ts`` entry."""
    _record_seen(
        data_dir=tmp_path, persona_id="grantley", replies=[("t1", "5555")]
    )
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="x")}
    )
    chat = _ScriptedChatService(
        events=[
            *_comment_events(tid="t1", reply_to_uin="5555"),
            DoneEvent(finish_reason="stop"),
        ],
    )
    app_state = _make_app_state(
        chat=chat, persona_store=store, metadata={"persona_id": "grantley"}
    )
    app_state.data_dir = tmp_path
    ctx = BuiltinContext(app_state=app_state, name="grantley.qzone_reply")
    out = await _qzone_reply_comments_action(ctx)
    assert out["ok"] is True
    seen = _read_seen(_seen_path(tmp_path, "grantley"))
    assert len(seen["t1"]) == 1


async def test_later_comment_by_same_person_uses_comment_id(tmp_path: Path) -> None:
    _record_seen(
        data_dir=tmp_path,
        persona_id="grantley",
        replies=[("t1", "id:first")],
    )
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="x")}
    )
    chat = _ScriptedChatService(
        events=[
            *_comment_events(
                tid="t1",
                reply_to_uin="5555",
                comment_id="second",
            ),
            DoneEvent(finish_reason="stop"),
        ],
    )
    app_state = _make_app_state(
        chat=chat,
        persona_store=store,
        metadata={"persona_id": "grantley"},
    )
    app_state.data_dir = tmp_path
    out = await _qzone_reply_comments_action(
        BuiltinContext(app_state=app_state, name="grantley.qzone_reply")
    )
    assert out["ok"] is True
    seen = _read_seen(_seen_path(tmp_path, "grantley"))
    assert {_seen.rsplit(":", 1)[0] for _seen in seen["t1"]} == {
        "id:first",
        "id:second",
    }


# ---------------------------------------------------------------------------
# Failure folding
# ---------------------------------------------------------------------------


async def test_chat_error_event_folds_into_audit() -> None:
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="x")}
    )
    err = InternalChatError(reason="rate_limit", message="too many requests")
    chat = _ScriptedChatService(events=[ErrorEvent(error=err)])
    ctx = BuiltinContext(
        app_state=_make_app_state(
            chat=chat, persona_store=store, metadata={"persona_id": "grantley"}
        ),
        name="grantley.qzone_reply",
    )
    out = await _qzone_reply_comments_action(ctx)
    assert out["ok"] is False
    assert out["error"] == "chat_error"
    assert out["chat_error_reason"] == "rate_limit"
    assert out["chat_error_message"] == "too many requests"
    assert out["replies_posted"] == 0


async def test_chat_service_raising_folds_into_audit() -> None:
    class _Boom:
        def run(self, req: Any, cancel: asyncio.Event):
            raise RuntimeError("backend down")

    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="x")}
    )
    ctx = BuiltinContext(
        app_state=_make_app_state(
            chat=_Boom(), persona_store=store, metadata={"persona_id": "grantley"}
        ),
        name="grantley.qzone_reply",
    )
    out = await _qzone_reply_comments_action(ctx)
    assert out["ok"] is False
    assert out["error"] == "chat_service_failed"
    assert "backend down" in out["message"]


async def test_chat_timeout_folds_into_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NeverEnding:
        def run(self, req: Any, cancel: asyncio.Event):
            async def _gen():
                yield _feed_events()[0]
                await asyncio.Event().wait()
                yield DoneEvent(finish_reason="stop")  # pragma: no cover

            return _gen()

    monkeypatch.setenv("CORLINMAN_QZONE_REPLY_TIMEOUT_SECS", "1")
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="x")}
    )
    ctx = BuiltinContext(
        app_state=_make_app_state(
            chat=_NeverEnding(),
            persona_store=store,
            metadata={"persona_id": "grantley"},
        ),
        name="grantley.qzone_reply",
    )
    out = await _qzone_reply_comments_action(ctx)
    assert out["ok"] is False
    assert out["error"] == "chat_timeout"


async def test_missing_persona_id_returns_typed_envelope() -> None:
    store = _FakePersonaStore({})
    chat = _ScriptedChatService(events=[])
    ctx = BuiltinContext(
        app_state=_make_app_state(chat=chat, persona_store=store, metadata={}),
        name="grantley.qzone_reply",
    )
    out = await _qzone_reply_comments_action(ctx)
    assert out["ok"] is False
    assert out["error"] == "missing_persona_id"
    assert store.gets == []


async def test_persona_not_found_returns_typed_envelope() -> None:
    store = _FakePersonaStore({})
    chat = _ScriptedChatService(events=[])
    ctx = BuiltinContext(
        app_state=_make_app_state(
            chat=chat, persona_store=store, metadata={"persona_id": "ghost"}
        ),
        name="grantley.qzone_reply",
    )
    out = await _qzone_reply_comments_action(ctx)
    assert out["ok"] is False
    assert out["error"] == "persona_not_found"
    assert store.gets == ["ghost"]


async def test_no_chat_service_returns_typed_envelope() -> None:
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="x")}
    )
    ctx = BuiltinContext(
        app_state=_make_app_state(
            chat=None, persona_store=store, metadata={"persona_id": "grantley"}
        ),
        name="grantley.qzone_reply",
    )
    out = await _qzone_reply_comments_action(ctx)
    assert out["ok"] is False
    assert out["error"] == "chat_service_unavailable"


async def test_run_builtin_indirection_passes_through(tmp_path: Path) -> None:
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="x")}
    )
    chat = _ScriptedChatService(
        events=[
            *_comment_events(tid="t1", reply_to_uin="5555"),
            DoneEvent(finish_reason="stop"),
        ],
    )
    app_state = _make_app_state(
        chat=chat, persona_store=store, metadata={"persona_id": "grantley"}
    )
    app_state.data_dir = tmp_path
    ctx = BuiltinContext(app_state=app_state, name="grantley.qzone_reply")
    out = await run_builtin(QZONE_REPLY_BUILTIN_NAME, ctx)
    assert out["ok"] is True
    assert out["replies_posted"] == 1


# ---------------------------------------------------------------------------
# Metadata clamps
# ---------------------------------------------------------------------------


def test_clamp_int_defaults_and_ranges() -> None:
    assert _clamp_int(None, default=3, lo=1, hi=10) == 3
    assert _clamp_int(True, default=3, lo=1, hi=10) == 3   # bool is not 1
    assert _clamp_int("x", default=3, lo=1, hi=10) == 3
    assert _clamp_int(99, default=3, lo=1, hi=10) == 10    # clamp high
    assert _clamp_int(0, default=3, lo=1, hi=10) == 1      # clamp low
    assert _clamp_int(7, default=3, lo=1, hi=10) == 7
    assert _clamp_int("6", default=3, lo=1, hi=10) == 6    # numeric string ok


async def test_metadata_knobs_flow_into_tail_clamped() -> None:
    """max_replies=99 / lookback_posts=99 clamp to 10 / 20 in the tail."""
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="x")}
    )
    chat = _ScriptedChatService(events=[DoneEvent(finish_reason="stop")])
    ctx = BuiltinContext(
        app_state=_make_app_state(
            chat=chat,
            persona_store=store,
            metadata={
                "persona_id": "grantley",
                "max_replies": 99,
                "lookback_posts": 99,
            },
        ),
        name="grantley.qzone_reply",
    )
    out = await _qzone_reply_comments_action(ctx)
    assert out["ok"] is True
    prompt = chat.requests[0].messages[0].content
    assert "最多回复 10 条" in prompt
    assert "最近 20 条" in prompt


async def test_per_job_metadata_table_overrides_direct_seam() -> None:
    """The production per-job metadata map wins over the test seam."""
    store = _FakePersonaStore(
        {"grantley": _FakePersona(id="grantley", system_prompt="x")}
    )
    chat = _ScriptedChatService(events=[DoneEvent(finish_reason="stop")])
    app_state = _make_app_state(chat=chat, persona_store=store, metadata={})
    app_state.scheduler_job_metadata = {
        "grantley.qzone_reply": {"persona_id": "grantley", "qq_account": "9999"}
    }
    ctx = BuiltinContext(app_state=app_state, name="grantley.qzone_reply")
    out = await _qzone_reply_comments_action(ctx)
    assert out["ok"] is True
    assert out["qq_account"] == "9999"


# ---------------------------------------------------------------------------
# Sidecar hygiene — caps / slug guard / atomicity
# ---------------------------------------------------------------------------


def test_seen_per_tid_cap(tmp_path: Path) -> None:
    replies = [("t1", f"{i}") for i in range(205)]
    _record_seen(data_dir=tmp_path, persona_id="grantley", replies=replies)
    seen = _read_seen(_seen_path(tmp_path, "grantley"))
    assert len(seen["t1"]) == 200
    # Newest survive, oldest roll off.
    assert seen["t1"][-1].startswith("204:")
    assert not any(e.startswith("0:") for e in seen["t1"])


def test_seen_total_tids_cap(tmp_path: Path) -> None:
    replies = [(f"t{i}", "5555") for i in range(105)]
    _record_seen(data_dir=tmp_path, persona_id="grantley", replies=replies)
    seen = _read_seen(_seen_path(tmp_path, "grantley"))
    assert len(seen) == 100
    assert "t104" in seen and "t0" not in seen


def test_seen_bad_slug_skips_entirely(tmp_path: Path) -> None:
    assert _seen_path(tmp_path, "../evil") is None
    _record_seen(
        data_dir=tmp_path, persona_id="../evil", replies=[("t1", "5555")]
    )
    seen_dir = tmp_path / "qzone_seen_comments"
    assert not seen_dir.exists() or list(seen_dir.iterdir()) == []


def test_seen_no_data_dir_is_noop() -> None:
    assert _seen_path(None, "grantley") is None
    _record_seen(data_dir=None, persona_id="grantley", replies=[("t", "u")])
    assert _read_seen(None) == {}


def test_seen_atomic_write_leaves_no_tmp(tmp_path: Path) -> None:
    _record_seen(
        data_dir=tmp_path, persona_id="grantley", replies=[("t1", "5555")]
    )
    seen_dir = tmp_path / "qzone_seen_comments"
    assert sorted(p.name for p in seen_dir.iterdir()) == ["grantley.json"]


def test_read_seen_tolerates_corrupt_sidecar(tmp_path: Path) -> None:
    path = tmp_path / "qzone_seen_comments" / "grantley.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert _read_seen(path) == {}
    path.write_text(json.dumps({"version": 1, "seen": "wrong"}), encoding="utf-8")
    assert _read_seen(path) == {}
    path.write_text(
        json.dumps({"version": 1, "seen": {"t1": ["ok:1", 5, ""], "t2": "no"}}),
        encoding="utf-8",
    )
    assert _read_seen(path) == {"t1": ["ok:1"]}


def test_seen_block_renders_uins_once() -> None:
    block = _seen_block({"t1": ["5555:1", "5555:2", "7777:3"], "t2": []})
    assert block is not None
    assert "说说 t1：已回复评论 5555、7777" in block
    assert "t2" not in block
    assert _seen_block({}) is None
