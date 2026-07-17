"""Memory W5 — the ``memory.reconcile`` sleep-time builtin.

Drives the full pipeline with a stubbed LLM: observation queue →
extraction → risk/redaction → mem0-style reconcile → core-block rebuild
→ report. Pins the hermes discipline (dry_run writes NOTHING and keeps
the queue intact) and the bi-temporal contract (updates supersede, never
delete).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_memory_kernel import KernelScope, MemoryKernel, Observation, now_ms
from corlinman_server.scheduler.builtins import (
    MEMORY_RECONCILE_BUILTIN_NAME,
    BuiltinContext,
    run_builtin,
)


def _extraction_reply(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"ok": True, "reply": json.dumps(items, ensure_ascii=False)}


def _runner_stub(items: list[dict[str, Any]]) -> Any:
    calls: list[str] = []

    async def _run(prompt: str) -> dict[str, Any]:
        calls.append(prompt)
        return _extraction_reply(items)

    _run.calls = calls  # type: ignore[attr-defined]
    return _run


async def _seed_observation(
    kernel: MemoryKernel, *, user: str = "U1", text: str, reply: str = "ok"
) -> None:
    await kernel.observe(
        Observation(
            session_key="s1",
            user_text=text,
            reply_text=reply,
            ts_ms=now_ms(),
            scope_user_id=user,
            channel="qq",
            channel_user_id="10086",
        )
    )


def _context(kernel: MemoryKernel, runner: Any, tmp_path: Path, **cfg: Any) -> BuiltinContext:
    curator = {"enabled": True, "dry_run": False, **cfg}
    return BuiltinContext(
        app_state=SimpleNamespace(
            memory_kernel=kernel,
            agent_runner_fn=runner,
            memory_curator_config=curator,
            data_dir=tmp_path,
        )
    )


@pytest.fixture
async def kernel(tmp_path: Path) -> Any:
    k = await MemoryKernel.open(tmp_path / "memory.sqlite")
    try:
        yield k
    finally:
        await k.close()


async def test_dry_run_reports_but_writes_nothing(
    kernel: MemoryKernel, tmp_path: Path
) -> None:
    await _seed_observation(kernel, text="我叫小明，家乡是哈尔滨")
    runner = _runner_stub(
        [
            {
                "topic": "user hometown",
                "kind": "user_preference",
                "summary": "用户的家乡是哈尔滨",
                "confidence": 0.9,
            }
        ]
    )
    ctx = _context(kernel, runner, tmp_path, dry_run=True)
    result = await run_builtin(MEMORY_RECONCILE_BUILTIN_NAME, ctx)

    assert result["ok"] is True and result["dry_run"] is True
    assert result["added"] == 1
    stats = await kernel.stats()
    assert stats["items"] == 0, "dry_run must not write items"
    assert stats["observations_pending"] == 1, "dry_run must not drain queue"
    # The anti-pattern preamble reaches the LLM prompt.
    assert "do NOT extract" in runner.calls[0]
    # A report landed on disk.
    reports = list((tmp_path / "reports" / "memory-curator").glob("*.json"))
    assert len(reports) == 1


async def test_live_run_adds_items_and_rebuilds_core_block(
    kernel: MemoryKernel, tmp_path: Path
) -> None:
    await _seed_observation(kernel, text="我叫小明，家乡是哈尔滨")
    runner = _runner_stub(
        [
            {
                "topic": "hometown",
                "kind": "user_preference",
                "summary": "用户的家乡是哈尔滨",
                "confidence": 0.9,
            },
            {
                "topic": "name",
                "kind": "user_preference",
                "summary": "用户的名字叫小明",
                "confidence": 0.8,
            },
        ]
    )
    result = await run_builtin(
        MEMORY_RECONCILE_BUILTIN_NAME, _context(kernel, runner, tmp_path)
    )
    assert result["added"] == 2 and result["ok"] is True

    scope = KernelScope(scope_user_id="U1")
    hits = await kernel.recall(scope, "家乡 哈尔滨")
    assert any("哈尔滨" in h.text for h in hits)
    stats = await kernel.stats()
    assert stats["observations_pending"] == 0, "queue consumed on live run"
    blocks = dict(await kernel.core_blocks(scope))
    assert "哈尔滨" in blocks.get("user_profile", "")
    assert result["core_blocks_rebuilt"] == 1


async def test_update_supersedes_bitemporally(
    kernel: MemoryKernel, tmp_path: Path
) -> None:
    scope = KernelScope(scope_user_id="U1")
    old_id = await kernel.add_item(
        scope,
        text="user works at Initech as engineer",
        kind="fact",
        source="reconcile",
    )
    await _seed_observation(kernel, text="I changed jobs to Globex")
    runner = _runner_stub(
        [
            {
                "topic": "employer",
                "kind": "project_context",
                "summary": "user works at Globex as engineer",
                "confidence": 0.9,
            }
        ]
    )
    result = await run_builtin(
        MEMORY_RECONCILE_BUILTIN_NAME, _context(kernel, runner, tmp_path)
    )
    assert result["updated"] == 1 and result["added"] == 0

    hits = await kernel.recall(scope, "works engineer")
    assert [("Globex" in h.text) for h in hits] == [True]
    stats = await kernel.stats()
    assert stats["items_invalidated"] == 1  # superseded, not deleted
    # A refines edge links new → old.
    async with kernel._conn.execute(  # noqa: SLF001
        "SELECT rel, dst_id FROM mk_edges"
    ) as cur:
        edges = await cur.fetchall()
    assert [(r["rel"], r["dst_id"]) for r in edges] == [("refines", old_id)]


async def test_duplicate_is_noop_and_blocked_never_lands(
    kernel: MemoryKernel, tmp_path: Path
) -> None:
    scope = KernelScope(scope_user_id="U1")
    await kernel.add_item(
        scope, text="用户的家乡是哈尔滨", kind="preference", source="reconcile"
    )
    await _seed_observation(kernel, text="对了我家乡是哈尔滨")
    runner = _runner_stub(
        [
            {
                "topic": "hometown again",
                "kind": "user_preference",
                "summary": "用户的家乡是哈尔滨",
                "confidence": 0.9,
            },
            {
                "topic": "credentials",
                "kind": "concept",
                "summary": "the user password is hunter2",
                "confidence": 0.9,
            },
        ]
    )
    result = await run_builtin(
        MEMORY_RECONCILE_BUILTIN_NAME, _context(kernel, runner, tmp_path)
    )
    assert result["noop"] == 1
    stats = await kernel.stats()
    # Only the pre-existing item; the dup was NOOP'd and the credential
    # candidate was either blocked or redacted — never stored verbatim.
    hits = await kernel.recall(scope, "password hunter2")
    assert all("hunter2" not in h.text for h in hits)
    assert stats["items"] <= 2


async def test_disabled_and_missing_deps_envelopes(
    kernel: MemoryKernel, tmp_path: Path
) -> None:
    runner = _runner_stub([])
    ctx = _context(kernel, runner, tmp_path, enabled=False)
    result = await run_builtin(MEMORY_RECONCILE_BUILTIN_NAME, ctx)
    assert result == {"ok": False, "reason": "disabled"}

    ctx2 = BuiltinContext(
        app_state=SimpleNamespace(
            memory_kernel=None,
            agent_runner_fn=runner,
            memory_curator_config={"enabled": True},
            data_dir=tmp_path,
        )
    )
    result2 = await run_builtin(MEMORY_RECONCILE_BUILTIN_NAME, ctx2)
    assert result2 == {"ok": False, "reason": "memory_kernel_unavailable"}
