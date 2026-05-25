"""W2.2 — scheduler ``system.update_check`` builtin contract tests.

Asserts the three-branch behaviour spelled out in
``docs/PLAN_AUTO_UPDATE.md`` §2 Wave 2/W2.2:

* No live checker on ``context.app_state`` → ``{ok: False, reason: "checker_unavailable"}``
* Live checker → returns the right dict shape from ``UpdateStatus``
* Checker raises → ``{ok: False, reason: "poll_failed: ..."}`` (never raises out)
"""

from __future__ import annotations

from types import SimpleNamespace

from corlinman_server.scheduler.builtins import (
    BUILTIN_ACTIONS,
    BuiltinContext,
    _system_update_check_action,
    run_builtin,
)
from corlinman_server.system import UpdateStatus


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _StubChecker:
    """Minimal :class:`UpdateChecker` stub.

    Exposes only :meth:`poll` (async) — the builtin doesn't touch any
    other surface of the real class, so a duck-typed stub is enough."""

    def __init__(self, status: UpdateStatus | None = None, raises: BaseException | None = None) -> None:
        self._status = status
        self._raises = raises
        self.calls: list[bool] = []

    async def poll(self, force: bool = False) -> UpdateStatus:
        self.calls.append(force)
        if self._raises is not None:
            raise self._raises
        assert self._status is not None, "test must seed either status or raises"
        return self._status


def _fake_status() -> UpdateStatus:
    return UpdateStatus(
        current="1.1.1",
        latest="1.2.0",
        available=True,
        release_url="https://github.com/ymylive/corlinman/releases/tag/v1.2.0",
        release_notes_md="## Changes\n\n* Did a thing",
        published_at=1716000000000,
        last_checked_at=1716000099000,
        prerelease_seen=[],
    )


# ---------------------------------------------------------------------------
# Registry surface
# ---------------------------------------------------------------------------


def test_system_update_check_is_registered_by_name() -> None:
    """Importing the builtins module registers ``system.update_check``
    so the scheduler-runtime hook (or a future ``dispatch_run_tool``
    path) can resolve it without re-importing every callsite."""
    assert "system.update_check" in BUILTIN_ACTIONS
    assert BUILTIN_ACTIONS["system.update_check"] is _system_update_check_action


# ---------------------------------------------------------------------------
# Direct callable surface
# ---------------------------------------------------------------------------


async def test_checker_unavailable_returns_typed_envelope() -> None:
    """When ``context.app_state`` has no ``corlinman_update_checker``
    slot the builtin must surface a typed ``checker_unavailable``
    envelope rather than raising / 500ing."""
    context = BuiltinContext(app_state=SimpleNamespace())
    out = await _system_update_check_action(context)
    assert out == {"ok": False, "reason": "checker_unavailable"}


async def test_none_app_state_returns_checker_unavailable() -> None:
    """``app_state=None`` (degraded boot before lifespan attaches the
    state bundle) is the same shape as a missing slot — the builtin
    short-circuits before touching the resolver chain."""
    context = BuiltinContext(app_state=None)
    out = await _system_update_check_action(context)
    assert out == {"ok": False, "reason": "checker_unavailable"}


async def test_happy_path_returns_status_dict() -> None:
    """Live checker on ``app_state.corlinman_update_checker`` →
    builtin pulls the status and returns the five-field report shape
    the scheduler history reads."""
    status = _fake_status()
    checker = _StubChecker(status=status)
    app_state = SimpleNamespace(corlinman_update_checker=checker)
    context = BuiltinContext(app_state=app_state)

    out = await _system_update_check_action(context)

    assert out == {
        "ok": True,
        "current": "1.1.1",
        "latest": "1.2.0",
        "available": True,
        "last_checked_at": 1716000099000,
    }
    # Cron-driven poll honours the checker's TTL — force must be False.
    assert checker.calls == [False]


async def test_poll_raises_caught_and_wrapped() -> None:
    """An exception out of ``poll()`` must NOT propagate. The scheduler
    tick loop is long-lived and a stray ``httpx.ConnectError`` would
    otherwise kill the whole gateway job. The wrapped envelope keeps
    the operator informed via scheduler history."""
    boom = RuntimeError("boom!")
    checker = _StubChecker(raises=boom)
    app_state = SimpleNamespace(corlinman_update_checker=checker)
    context = BuiltinContext(app_state=app_state)

    out = await _system_update_check_action(context)

    assert out["ok"] is False
    reason = out["reason"]
    assert isinstance(reason, str)
    assert reason.startswith("poll_failed: ")
    assert "boom" in reason


async def test_admin_state_fallback_when_app_state_misses() -> None:
    """When the AppState bundle doesn't carry the checker but the
    admin_b state does (test surface, partial wiring), the resolver
    falls through and the poll still runs. Covers the resolver chain
    in :func:`_resolve_update_checker`."""
    status = _fake_status()
    checker = _StubChecker(status=status)
    # Empty app_state (no corlinman_update_checker / update_checker),
    # but admin_state carries the handle.
    app_state = SimpleNamespace()
    admin_state = SimpleNamespace(update_checker=checker)
    context = BuiltinContext(app_state=app_state, admin_state=admin_state)

    out = await _system_update_check_action(context)

    assert out["ok"] is True
    assert out["latest"] == "1.2.0"


# ---------------------------------------------------------------------------
# Dispatcher (run_builtin) surface — exercised so a future scheduler
# integration that goes through the public entry point still sees the
# same typed envelopes.
# ---------------------------------------------------------------------------


async def test_run_builtin_unknown_name_returns_typed_envelope() -> None:
    out = await run_builtin("does.not.exist", BuiltinContext(app_state=None))
    assert out["ok"] is False
    assert isinstance(out["reason"], str)
    assert out["reason"].startswith("unknown_builtin: ")


async def test_run_builtin_passes_through_system_update_check_success() -> None:
    """End-to-end: ``run_builtin("system.update_check", ctx)`` returns
    the same dict the action would have returned directly. Confirms
    the registry indirection is transparent."""
    status = _fake_status()
    checker = _StubChecker(status=status)
    context = BuiltinContext(
        app_state=SimpleNamespace(corlinman_update_checker=checker),
    )

    out = await run_builtin("system.update_check", context)

    assert out["ok"] is True
    assert out["current"] == "1.1.1"
    assert out["latest"] == "1.2.0"
    assert out["available"] is True


async def test_run_builtin_wraps_a_raising_builtin() -> None:
    """When an arbitrary registered builtin raises, ``run_builtin``
    must wrap into ``builtin_raised: ...`` — same defensive shape as
    the inline ``poll_failed`` branch. Confirms the boundary catch is
    actually wired (rather than just relying on the action's own
    try/except)."""

    async def _boom(_ctx: BuiltinContext) -> dict:
        raise ValueError("nope")

    from corlinman_server.scheduler.builtins import register_builtin

    register_builtin("test.boom", _boom)
    try:
        out = await run_builtin("test.boom", BuiltinContext())
    finally:
        BUILTIN_ACTIONS.pop("test.boom", None)

    assert out["ok"] is False
    assert isinstance(out["reason"], str)
    assert out["reason"].startswith("builtin_raised: ")
    assert "nope" in out["reason"]
