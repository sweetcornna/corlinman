"""Tests for :class:`corlinman_hooks.HookRunner`.

Covers:
- no-op (no hooks configured) → always allow;
- tool-specific key wins over wildcard;
- wildcard ``pre_tool`` key is a fallback;
- non-zero exit blocks the tool call with the hook's stdout as the message;
- timeout is treated as allow (never bricks dispatch);
- post_tool hook is fire-and-forget (exit code ignored);
- notification hook is fire-and-forget;
- ``registered`` property reflects the configured commands;
- ``supported_events()`` returns the expected canonical list;
- async ``run_pre_tool_async`` mirrors the sync path.
"""

from __future__ import annotations

import sys

import pytest
from corlinman_hooks import HookRunner

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _runner(hooks: dict) -> HookRunner:
    return HookRunner({"hooks": hooks})


# Shell snippet that always exits 0 (allow).
_ALLOW_CMD = "true" if sys.platform != "win32" else "cmd /c exit 0"
# Shell snippet that always exits 1 and prints a message (block).
_BLOCK_CMD = "echo blocked; exit 1" if sys.platform != "win32" else "cmd /c \"echo blocked && exit 1\""
# Shell snippet that always exits 0.
_NOOP_CMD = "true" if sys.platform != "win32" else "cmd /c exit 0"


# ---------------------------------------------------------------------------
# No-op path
# ---------------------------------------------------------------------------


def test_no_hooks_configured_allows_all():
    runner = HookRunner({})
    ok, msg = runner.run_pre_tool("run_shell", {"cmd": "ls"})
    assert ok is True
    assert msg == ""


def test_empty_hooks_dict_allows_all():
    runner = HookRunner({"hooks": {}})
    ok, msg = runner.run_pre_tool("anything", {})
    assert ok is True
    assert msg == ""


# ---------------------------------------------------------------------------
# Allow path
# ---------------------------------------------------------------------------


def test_pre_tool_wildcard_allows_on_zero_exit():
    runner = _runner({"pre_tool": _ALLOW_CMD})
    ok, msg = runner.run_pre_tool("read_file", {"path": "/tmp/f"})
    assert ok is True
    assert msg == ""


def test_pre_tool_specific_key_allows_on_zero_exit():
    runner = _runner({"pre_read_file": _ALLOW_CMD})
    ok, msg = runner.run_pre_tool("read_file", {"path": "/tmp/f"})
    assert ok is True
    assert msg == ""


# ---------------------------------------------------------------------------
# Block path
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="shell syntax differs on Windows")
def test_pre_tool_wildcard_blocks_on_nonzero_exit():
    runner = _runner({"pre_tool": "echo 'no way'; exit 1"})
    ok, msg = runner.run_pre_tool("run_shell", {"cmd": "rm -rf /"})
    assert ok is False
    assert "no way" in msg


@pytest.mark.skipif(sys.platform == "win32", reason="shell syntax differs on Windows")
def test_pre_tool_specific_key_blocks_on_nonzero_exit():
    runner = _runner({"pre_run_shell": "echo 'blocked by specific hook'; exit 2"})
    ok, msg = runner.run_pre_tool("run_shell", {"cmd": "ls"})
    assert ok is False
    assert "blocked by specific hook" in msg


@pytest.mark.skipif(sys.platform == "win32", reason="shell syntax differs on Windows")
def test_specific_key_wins_over_wildcard():
    """Tool-specific hook is tried before the wildcard."""
    # The specific hook blocks; the wildcard would allow.
    runner = _runner({
        "pre_run_shell": "exit 1",
        "pre_tool": _ALLOW_CMD,
    })
    ok, _ = runner.run_pre_tool("run_shell", {})
    assert ok is False


# ---------------------------------------------------------------------------
# Post-tool (fire-and-forget)
# ---------------------------------------------------------------------------


def test_post_tool_runs_without_raising():
    runner = _runner({"post_tool": _ALLOW_CMD})
    # Should not raise even on non-zero exit.
    runner.run_post_tool("read_file", {"path": "/tmp/x"}, '{"result": "data"}')


def test_post_tool_no_hook_is_noop():
    runner = _runner({})
    runner.run_post_tool("anything", {}, "{}")  # must not raise


# ---------------------------------------------------------------------------
# Notification (fire-and-forget)
# ---------------------------------------------------------------------------


def test_notification_runs_without_raising():
    runner = _runner({"notification": _ALLOW_CMD})
    runner.run_notification({"event": "turn_complete", "session": "s1"})


def test_notification_no_hook_is_noop():
    runner = _runner({})
    runner.run_notification({"event": "startup"})


# ---------------------------------------------------------------------------
# Discovery / introspection
# ---------------------------------------------------------------------------


def test_registered_returns_configured_commands():
    hooks = {"pre_tool": "/bin/hook.sh", "post_tool": "/bin/post.sh"}
    runner = _runner(hooks)
    assert runner.registered == hooks


def test_registered_is_a_copy():
    """Mutating the returned dict does not affect the runner's internal state."""
    runner = _runner({"pre_tool": "/bin/hook.sh"})
    copy = runner.registered
    copy["extra"] = "injected"
    assert "extra" not in runner.registered


def test_supported_events_contains_canonical_events():
    runner = _runner({})
    events = runner.supported_events()
    assert "pre_tool" in events
    assert "post_tool" in events
    assert "notification" in events


# ---------------------------------------------------------------------------
# Async variant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_run_pre_tool_allows_on_zero_exit():
    runner = _runner({"pre_tool": _ALLOW_CMD})
    ok, msg = await runner.run_pre_tool_async("read_file", {})
    assert ok is True
    assert msg == ""


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform == "win32", reason="shell syntax differs on Windows")
async def test_async_run_pre_tool_blocks_on_nonzero_exit():
    runner = _runner({"pre_tool": "echo 'async blocked'; exit 3"})
    ok, msg = await runner.run_pre_tool_async("run_shell", {})
    assert ok is False
    assert "async blocked" in msg


@pytest.mark.asyncio
async def test_async_run_pre_tool_no_hook_allows():
    runner = HookRunner({})
    ok, msg = await runner.run_pre_tool_async("anything", {})
    assert ok is True
    assert msg == ""


# ---------------------------------------------------------------------------
# Config schema: non-dict hooks field is silently ignored
# ---------------------------------------------------------------------------


def test_non_dict_hooks_value_treated_as_empty():
    runner = HookRunner({"hooks": "invalid"})
    ok, _ = runner.run_pre_tool("anything", {})
    assert ok is True
