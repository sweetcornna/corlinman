"""W7 — the servicer trust loop end-to-end (inject → reply → verdicts)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_memory_kernel import KernelScope, MemoryKernel
from corlinman_server.agent_servicer import CorlinmanAgentServicer


class _FakeProvider:
    def __init__(self) -> None:  # pragma: no cover
        pass


class _NullHost:
    async def recent(self, session_key: str, limit: int) -> list[Any]:
        return []

    async def query(self, req: Any) -> list[Any]:
        return []

    async def upsert(self, doc: Any) -> str:
        return "1"


_SCOPE = KernelScope(scope_user_id="qq:10086")


def _servicer(kernel: MemoryKernel, **extra: Any) -> CorlinmanAgentServicer:
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider())
    servicer.set_app_state(
        SimpleNamespace(
            memory_host=_NullHost(),
            memory_kernel=kernel,
            identity_resolver=None,
            **extra,
        )
    )
    return servicer


def _start() -> Any:
    from corlinman_agent.reasoning_loop import ChatStart

    start = ChatStart(
        model="m",
        messages=[{"role": "user", "content": "what tea does the user like"}],
        session_key="s1",
    )
    start.extra = {"binding": {"channel": "qq", "sender": "10086"}}
    return start


async def _drain(servicer: CorlinmanAgentServicer) -> None:
    for _ in range(200):
        if not servicer._prefetch_tasks:
            return
        await asyncio.sleep(0.01)


async def _run_turn(
    servicer: CorlinmanAgentServicer, reply: str
) -> None:
    start = _start()
    await servicer._recall_memory(start)
    await _drain(servicer)  # injection bookkeeping lands
    await servicer._store_memory("s1", "what tea does the user like", reply, start=start)
    await _drain(servicer)  # trust loop lands


async def test_used_reply_bumps_trust_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORLINMAN_MEMORY_KERNEL", "on")
    kernel = await MemoryKernel.open(tmp_path / "memory.sqlite")
    item = await kernel.add_item(
        _SCOPE, text="user likes oolong tea", kind="preference", source="turn"
    )
    servicer = _servicer(
        kernel, memory_trust_config={"enabled": True, "dry_run": False}
    )
    try:
        await _run_turn(servicer, "the user likes oolong tea, served hot")
        hits = await kernel.recall(_SCOPE, "oolong tea")
        assert hits[0].id == item and hits[0].trust > 0.5
        assert hits[0].utility > 0.5
    finally:
        await servicer.aclose()
        await kernel.close()


async def test_dry_run_records_but_never_moves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORLINMAN_MEMORY_KERNEL", "on")
    kernel = await MemoryKernel.open(tmp_path / "memory.sqlite")
    await kernel.add_item(
        _SCOPE, text="user likes oolong tea", kind="preference", source="turn"
    )
    servicer = _servicer(
        kernel, memory_trust_config={"enabled": True, "dry_run": True}
    )
    try:
        await _run_turn(servicer, "the user likes oolong tea, served hot")
        hits = await kernel.recall(_SCOPE, "oolong tea")
        assert hits[0].trust == 0.5, "dry run must not move trust"
        async with kernel._conn.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM mk_recall_ledger WHERE verdict = 'used'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None and row[0] == 1, "verdict telemetry recorded"
    finally:
        await servicer.aclose()
        await kernel.close()


async def test_disabled_leaves_ledger_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORLINMAN_MEMORY_KERNEL", "on")
    kernel = await MemoryKernel.open(tmp_path / "memory.sqlite")
    await kernel.add_item(
        _SCOPE, text="user likes oolong tea", kind="preference", source="turn"
    )
    servicer = _servicer(kernel)  # no trust config → disabled default
    try:
        await _run_turn(servicer, "the user likes oolong tea")
        async with kernel._conn.execute(  # noqa: SLF001
            "SELECT COUNT(*) FROM mk_recall_ledger WHERE verdict IS NOT NULL"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None and row[0] == 0
    finally:
        await servicer.aclose()
        await kernel.close()


async def test_ambiguous_goes_to_sampled_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORLINMAN_MEMORY_KERNEL", "on")
    kernel = await MemoryKernel.open(tmp_path / "memory.sqlite")
    await kernel.add_item(
        _SCOPE, text="user likes oolong tea", kind="preference", source="turn"
    )
    judge_prompts: list[str] = []

    async def judge(prompt: str) -> dict[str, Any]:
        judge_prompts.append(prompt)
        return {"ok": True, "reply": "contradicted"}

    servicer = _servicer(
        kernel,
        agent_runner_fn=judge,
        memory_trust_config={
            "enabled": True,
            "dry_run": False,
            "judge_sample": 1.0,
        },
    )
    try:
        # Reply engages the memory's content but negates it.
        await _run_turn(
            servicer, "actually the user does not like oolong tea anymore"
        )
        assert judge_prompts, "ambiguous verdict must reach the judge"
        hits = await kernel.recall(_SCOPE, "oolong tea")
        assert hits[0].trust < 0.5, "judge contradiction must cut trust"
        async with kernel._conn.execute(  # noqa: SLF001
            "SELECT verdict_tier FROM mk_recall_ledger WHERE verdict = 'contradicted'"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None and row[0] == 1  # tier-1 verdict
    finally:
        await servicer.aclose()
        await kernel.close()
