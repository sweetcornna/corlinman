"""Gap-fill (lane-hooks) tests for the decision/discovery hook layer.

Covers gaps ``hooks-no-discovery`` + ``loop-blocking-lifecycle-hooks``:

- :func:`corlinman_hooks.bus.HookBus.emit_collect` aggregates subscriber
  return values (the decision path) without breaking observe-only
  :meth:`emit`.
- module-level :func:`corlinman_hooks.runner.emit_collect` aggregates
  process-global handler returns.
- :meth:`HookRunner.run_pre_tool` deny → ``allow`` False (and the
  tuple-compatible unpack still yields the legacy ``(ok, msg)`` shape).
- file-based discovery loads a temp ``HOOK.yaml`` + ``handler.py``.
- :meth:`HookRunner.run_stop` default allow.
- new lifecycle :class:`HookEvent` variants round-trip through the wire.

Uniquely named so it never collides with the sibling lanes' tests.
"""

from __future__ import annotations

import asyncio

import pytest
from corlinman_hooks import HookBus, HookEvent
from corlinman_hooks.runner import (
    HookDecision,
    HookRunner,
    clear_global_handlers,
    emit_collect,
    register_global_handler,
)


@pytest.fixture(autouse=True)
def _clean_globals():
    clear_global_handlers()
    yield
    clear_global_handlers()


# ---------------------------------------------------------------------------
# emit_collect — bus method aggregates subscriber returns
# ---------------------------------------------------------------------------


def test_gf_hooks_bus_emit_collect_aggregates_returns():
    async def run():
        bus = HookBus()
        bus.subscribe(lambda ev: True, lambda ev: {"allow": False})
        bus.subscribe(lambda ev: True, lambda ev: None)  # abstain → skipped

        async def _async_sub(ev):
            return "decided"

        bus.subscribe(lambda ev: True, _async_sub)
        ev = HookEvent.Stop(session_key_="s1", turn_id=1, rounds=0)
        collected = await bus.emit_collect(ev)
        return collected

    collected = asyncio.run(run())
    assert collected == [{"allow": False}, "decided"]


def test_gf_hooks_bus_emit_still_observe_only():
    """emit_collect must not break fire-and-forget emit."""

    async def run():
        bus = HookBus()
        seen: list[str] = []
        bus.subscribe(lambda ev: True, lambda ev: seen.append(ev.kind()))
        ev = HookEvent.SessionStart(session_key_="s1")
        out = await bus.emit(ev)
        return out, seen

    out, seen = asyncio.run(run())
    assert out is None
    assert seen == ["session_start"]


def test_gf_hooks_bus_emit_collect_isolates_raising_subscriber():
    async def run():
        bus = HookBus()

        def _boom(ev):
            raise RuntimeError("subscriber broke")

        bus.subscribe(lambda ev: True, _boom)
        bus.subscribe(lambda ev: True, lambda ev: "survivor")
        ev = HookEvent.Stop(session_key_="s", turn_id=None, rounds=0)
        return await bus.emit_collect(ev)

    assert asyncio.run(run()) == ["survivor"]


# ---------------------------------------------------------------------------
# module-level emit_collect — process-global handler aggregation
# ---------------------------------------------------------------------------


def test_gf_hooks_module_emit_collect_aggregates():
    register_global_handler("stop", lambda e, p: HookDecision(allow=False, reason="veto"))
    register_global_handler("stop", lambda e, p: None)  # abstain skipped
    register_global_handler("stop", lambda e, p: True)
    res = emit_collect("stop", {"turn_id": 1})
    assert len(res) == 2
    assert isinstance(res[0], HookDecision) and res[0].allow is False
    assert res[1] is True


def test_gf_hooks_module_emit_collect_empty_when_no_handlers():
    assert emit_collect("session_start", {"session_key": "s"}) == []


# ---------------------------------------------------------------------------
# HookRunner.run_pre_tool deny → allow False
# ---------------------------------------------------------------------------


def test_gf_hooks_run_pre_tool_shell_deny_sets_allow_false():
    runner = HookRunner({"hooks": {"pre_tool": "echo no-way; exit 1"}})
    decision = runner.run_pre_tool("run_shell", {"cmd": "rm -rf /"})
    assert decision.allow is False
    assert "no-way" in (decision.reason or "")
    # tuple-compatible unpack still works (back-compat with old callers).
    ok, msg = decision
    assert ok is False
    assert "no-way" in msg


def test_gf_hooks_run_pre_tool_no_hook_allows():
    runner = HookRunner({})
    decision = runner.run_pre_tool("anything", {})
    assert decision.allow is True
    assert decision.reason is None
    ok, msg = decision
    assert ok is True and msg == ""


def test_gf_hooks_discovered_handler_deny():
    runner = HookRunner({})
    runner.register_handler(
        "pre_tool", lambda ev, payload: HookDecision(allow=False, reason="policy")
    )
    decision = runner.run_pre_tool("run_shell", {})
    assert decision.allow is False
    assert decision.reason == "policy"


# ---------------------------------------------------------------------------
# Discovery loads a temp HOOK.yaml + handler.py
# ---------------------------------------------------------------------------


def test_gf_hooks_discovery_loads_hook_yaml(tmp_path):
    hook_dir = tmp_path / "deny-shell"
    hook_dir.mkdir()
    (hook_dir / "HOOK.yaml").write_text(
        "events: [pre_tool, stop]\nhandler: handler.py:run\n", encoding="utf-8"
    )
    (hook_dir / "handler.py").write_text(
        "from corlinman_hooks.runner import HookDecision\n"
        "def run(event, payload):\n"
        "    if event == 'pre_tool' and payload.get('tool') == 'run_shell':\n"
        "        return HookDecision(allow=False, reason='shell blocked by discovered hook')\n"
        "    if event == 'stop':\n"
        "        return HookDecision(allow=False, reason='turn not done', inject_message='keep going')\n"
        "    return None\n",
        encoding="utf-8",
    )
    runner = HookRunner({}, hooks_dir=tmp_path)
    assert runner.discovered_events.get("pre_tool") == 1
    assert runner.discovered_events.get("stop") == 1

    blocked = runner.run_pre_tool("run_shell", {})
    assert blocked.allow is False
    assert "discovered hook" in (blocked.reason or "")

    # Tool the handler abstains on → allowed.
    allowed = runner.run_pre_tool("read_file", {})
    assert allowed.allow is True

    stop = runner.run_stop({})
    assert stop.allow is False
    assert stop.inject_message == "keep going"


def test_gf_hooks_discovery_skips_bad_folders(tmp_path):
    # Missing handler.py → skipped, no raise.
    bad = tmp_path / "broken"
    bad.mkdir()
    (bad / "HOOK.yaml").write_text("events: [pre_tool]\n", encoding="utf-8")
    # Good folder alongside.
    good = tmp_path / "ok"
    good.mkdir()
    (good / "HOOK.yaml").write_text("events: [stop]\nhandler: handler.py:run\n", encoding="utf-8")
    (good / "handler.py").write_text(
        "def run(event, payload):\n    return None\n", encoding="utf-8"
    )
    runner = HookRunner({}, hooks_dir=tmp_path)
    # broken contributed nothing; good registered its stop handler.
    assert runner.discovered_events.get("pre_tool") is None
    assert runner.discovered_events.get("stop") == 1


def test_gf_hooks_discovery_missing_dir_is_noop():
    runner = HookRunner({})
    assert runner.discover("/nonexistent/path/for/hooks") == 0


def test_gf_hooks_flat_yaml_fallback_parser():
    # The built-in fallback parser handles the flat shape PyYAML would.
    parsed = HookRunner._parse_flat_yaml(
        "# comment\nevents: [pre_tool, stop]\nhandler: handler.py:run\n\nname: 'my hook'\n"
    )
    assert parsed["events"] == ["pre_tool", "stop"]
    assert parsed["handler"] == "handler.py:run"
    assert parsed["name"] == "my hook"


# ---------------------------------------------------------------------------
# run_stop default allow
# ---------------------------------------------------------------------------


def test_gf_hooks_run_stop_default_allow():
    runner = HookRunner({})
    decision = runner.run_stop({})
    assert decision.allow is True
    assert decision.stop is False
    assert decision.inject_message is None


def test_gf_hooks_run_stop_handler_veto_injects_message():
    runner = HookRunner({})
    runner.register_handler(
        "stop",
        lambda ev, payload: HookDecision(
            allow=False, reason="not yet", inject_message="finish the task"
        ),
    )
    decision = runner.run_stop({"turn_id": 3})
    assert decision.allow is False
    assert decision.inject_message == "finish the task"


# ---------------------------------------------------------------------------
# run_notification dual call shape (C3 + legacy)
# ---------------------------------------------------------------------------


def test_gf_hooks_run_notification_both_shapes_are_noop_without_hook():
    runner = HookRunner({})
    # C3 shape (event, payload) and legacy (payload) must both no-op.
    runner.run_notification("turn_complete", {"session": "s1"})
    runner.run_notification({"event": "startup"})


# ---------------------------------------------------------------------------
# New lifecycle HookEvent variants round-trip through the wire
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "event",
    [
        HookEvent.SessionStart(session_key_="s1", source="resume"),
        HookEvent.SessionEnd(session_key_="s1", reason="clear"),
        HookEvent.SessionReset(session_key_="s1"),
        HookEvent.PreCompact(session_key_="s1", message_count=10, token_estimate=5000),
        HookEvent.PostCompact(session_key_="s1", messages_before=10, messages_after=3),
        HookEvent.Stop(session_key_="s1", turn_id=4, rounds=2),
        HookEvent.PreToolDecision(tool="run_shell", call_id="c1", args={"cmd": "ls"}, session_key_="s1"),
    ],
)
def test_gf_hooks_lifecycle_events_round_trip(event):
    rehydrated = HookEvent.from_dict(event.to_dict())
    assert rehydrated.session_key() == "s1"
    assert rehydrated.kind() == event.kind()
    # JSON round-trip too.
    assert HookEvent.from_json(event.to_json()).kind() == event.kind()
