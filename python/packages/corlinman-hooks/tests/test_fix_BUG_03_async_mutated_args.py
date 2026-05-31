"""Repro for BUG-03: async pre-tool path drops a tool-specific handler's
mutated_args (diverges from the sync path).

Register a ``pre_read_file`` handler that mutates args, with NO generic
``pre_tool`` handler. The sync path merges the specific tier; the async
path must do the same. Before the fix, ``run_pre_tool_async`` returns the
generic tier's allow-all decision (mutated_args=None) on the allow path,
silently discarding the specific tier's mutation.
"""

from __future__ import annotations

import asyncio

from corlinman_hooks.runner import HookDecision, HookRunner


def _mutating_read_file_handler(event, payload):
    return HookDecision(allow=True, mutated_args={"path": "/safe"})


def test_async_pre_tool_carries_specific_mutated_args():
    runner = HookRunner()
    runner.register_handler("pre_read_file", _mutating_read_file_handler)

    decision = asyncio.run(
        runner.run_pre_tool_async("read_file", {"path": "/x"})
    )

    assert decision.allow is True
    # The acceptance criterion: specific-tier mutation must survive.
    assert decision.mutated_args == {"path": "/safe"}


def test_async_matches_sync_for_specific_only():
    """The async path must agree with the sync path for a specific-only
    mutation (parity guard so the two methods stay in lockstep)."""
    runner = HookRunner()
    runner.register_handler("pre_read_file", _mutating_read_file_handler)

    sync_decision = runner.run_pre_tool("read_file", {"path": "/x"})
    async_decision = asyncio.run(
        runner.run_pre_tool_async("read_file", {"path": "/x"})
    )

    assert async_decision.allow == sync_decision.allow
    assert async_decision.mutated_args == sync_decision.mutated_args


def test_async_merges_specific_then_generic():
    """Both tiers contribute: generic mutation overrides specific (last
    write wins, specific-then-generic order), inject_message from specific
    survives, stop OR-folds."""
    runner = HookRunner()
    runner.register_handler(
        "pre_read_file",
        lambda e, p: HookDecision(
            allow=True, mutated_args={"path": "/safe"}, inject_message="from-specific"
        ),
    )
    runner.register_handler(
        "pre_tool",
        lambda e, p: HookDecision(allow=True, mutated_args={"path": "/generic"}, stop=True),
    )

    decision = asyncio.run(runner.run_pre_tool_async("read_file", {"path": "/x"}))

    assert decision.allow is True
    assert decision.mutated_args == {"path": "/generic"}
    assert decision.inject_message == "from-specific"
    assert decision.stop is True
