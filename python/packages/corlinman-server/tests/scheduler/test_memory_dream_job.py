"""Memory W8 — the ``memory.dream`` nightly cycle.

Drives the full dream with a stubbed LLM: affect-weighted sampling →
reflections (evidence-gated) + diary + mood nudge + gated demotion →
report. Pins the anti-hallucination rail (a reflection citing an unknown
id is rejected) and the hermes dry-run discipline.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_memory_kernel import KernelScope, MemoryKernel
from corlinman_server.scheduler.builtins import (
    MEMORY_DREAM_BUILTIN_NAME,
    BuiltinContext,
    run_builtin,
)


def _runner(dream: dict[str, Any]) -> Any:
    calls: list[str] = []

    async def _run(prompt: str) -> dict[str, Any]:
        calls.append(prompt)
        return {"ok": True, "reply": json.dumps(dream, ensure_ascii=False)}

    _run.calls = calls  # type: ignore[attr-defined]
    return _run


@pytest.fixture
async def kernel(tmp_path: Path) -> Any:
    k = await MemoryKernel.open(tmp_path / "memory.sqlite")
    try:
        yield k
    finally:
        await k.close()


async def _seed(kernel: MemoryKernel, persona: str = "grantley") -> list[str]:
    scope = KernelScope(scope_user_id="u1", persona_id=persona)
    ids = []
    for text, sal in [
        ("学会了做红烧肉", 0.8),
        ("讨论了量子计算", 0.2),
        ("散步看到晚霞", 0.6),
    ]:
        item_id = await kernel.add_item(
            scope, text=text, kind="fact", source="turn", importance=0.6
        )
        await kernel.set_affect(item_id, 0.5, 0.0, 0.3, sal)
        ids.append(item_id)
    return ids


def _ctx(kernel: MemoryKernel, runner: Any, tmp_path: Path, **cfg: Any) -> Any:
    dream = {"enabled": True, "dry_run": False, "persona_id": "grantley", **cfg}
    app_state = SimpleNamespace(
        memory_kernel=kernel,
        agent_runner_fn=runner,
        memory_dream_config=dream,
        corlinman_persona_state_store=SimpleNamespace(),
        data_dir=tmp_path,
    )
    return BuiltinContext(app_state=app_state)


async def test_dream_writes_reflections_diary_mood_demote(
    kernel: MemoryKernel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = await _seed(kernel)
    runner = _runner(
        {
            "reflections": [
                {"text": "我开始享受下厨和散步这些小事", "evidence": [ids[0], ids[2]]}
            ],
            "diary": "今天很充实,学会了红烧肉,傍晚的晚霞很美。",
            "mood_delta": {"e": 0.2, "p": 0.0, "a": 0.1},
            "demote": [ids[1]],  # the quantum-computing chat felt stale
        }
    )
    ctx = _ctx(kernel, runner, tmp_path)
    diary_captured: list[str] = []
    # Patch the persona diary dispatcher to the capture stub.
    import corlinman_server.scheduler.builtins.memory_dream as mod

    async def _write_diary(app_state: Any, persona_id: str, entry: str) -> bool:
        diary_captured.append(entry)
        return True

    monkeypatch.setattr(mod, "_write_diary", _write_diary)

    result = await run_builtin(MEMORY_DREAM_BUILTIN_NAME, ctx)
    assert result["ok"] and result["reflections"] == 1
    assert result["reflections_rejected"] == 0
    assert result["diary_written"] is True
    assert result["demoted"] == 1
    assert result["mood_delta"] == [0.2, 0.0, 0.1]

    # Reflection landed at low trust with derived_from evidence edges,
    # scoped to u1 (all its evidence was u1's — the privacy inheritance).
    # recall() is recency-ordered and ties are unstable, so locate the
    # reflection explicitly rather than assuming hits[0].
    scope = KernelScope(scope_user_id="u1", persona_id="grantley")
    hits = await kernel.recall(scope, "享受 下厨 散步")
    refl = next((h for h in hits if h.kind == "reflection"), None)
    assert refl is not None and refl.trust == 0.4
    async with kernel._conn.execute(  # noqa: SLF001
        "SELECT rel, dst_id FROM mk_edges WHERE src_id = ?", (refl.id,)
    ) as cur:
        edges = await cur.fetchall()
    assert {r["dst_id"] for r in edges} == {ids[0], ids[2]}
    assert all(r["rel"] == "derived_from" for r in edges)

    # Diary written; mood nudged; stale item demoted.
    assert diary_captured and "红烧肉" in diary_captured[0]
    mood = await kernel.get_affect_state("grantley")
    assert mood[0] > 0.0
    async with kernel._conn.execute(  # noqa: SLF001
        "SELECT importance FROM mk_items WHERE id = ?", (ids[1],)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None and row[0] < 0.6  # demoted


async def test_reflection_inherits_single_user_scope(
    kernel: MemoryKernel, tmp_path: Path
) -> None:
    """A reflection drawn only from user A's memories must be scoped to
    A (private), not agent-global where every user of the persona sees
    it. Cross-user evidence stays persona-global."""
    scope_a = KernelScope(scope_user_id="alice", persona_id="grantley")
    scope_b = KernelScope(scope_user_id="bob", persona_id="grantley")
    a1 = await kernel.add_item(scope_a, text="alice 爱爬山", kind="fact", source="turn")
    a2 = await kernel.add_item(scope_a, text="alice 周末去了黄山", kind="fact", source="turn")
    b1 = await kernel.add_item(scope_b, text="bob 爱做饭", kind="fact", source="turn")
    for i in (a1, a2, b1):
        await kernel.set_affect(i, 0.4, 0.0, 0.2, 0.6)

    runner = _runner(
        {
            "reflections": [
                {"text": "alice 热爱户外和登山", "evidence": [a1, a2]},
                {"text": "大家都各有热爱的事", "evidence": [a1, b1]},
            ],
            "diary": "",
            "mood_delta": {},
            "demote": [],
        }
    )
    ctx = _ctx(kernel, runner, tmp_path)
    result = await run_builtin(MEMORY_DREAM_BUILTIN_NAME, ctx)
    assert result["reflections"] == 2

    # alice-derived reflection surfaces for alice, NOT for bob.
    alice_hits = await kernel.recall(scope_a, "热爱 户外 登山")
    assert any("热爱户外" in h.text for h in alice_hits)
    bob_hits = await kernel.recall(scope_b, "热爱 户外 登山")
    assert not any("热爱户外" in h.text for h in bob_hits), "cross-user leak"
    # The cross-user pattern is persona-global (visible to both).
    assert any("各有热爱" in h.text for h in await kernel.recall(scope_b, "各有热爱"))


async def test_mood_nudge_adds_not_replaces(
    kernel: MemoryKernel, tmp_path: Path
) -> None:
    """The morning nudge ADDs its delta to accumulated mood — an
    alpha=1 EMA would wipe the mood down to just the ±0.3 delta."""
    await kernel.update_affect_state("grantley", (0.9, 0.0, 0.0), alpha=1.0)
    await _seed(kernel)
    runner = _runner(
        {
            "reflections": [],
            "diary": "",
            "mood_delta": {"e": 0.2, "p": 0.0, "a": 0.0},
            "demote": [],
        }
    )
    ctx = _ctx(kernel, runner, tmp_path)
    await run_builtin(MEMORY_DREAM_BUILTIN_NAME, ctx)
    mood = await kernel.get_affect_state("grantley")
    # 0.9 + 0.2, clamped to 1.0 — NOT reset to 0.2.
    assert mood[0] > 0.9, f"nudge must add, got {mood[0]}"


async def test_reflection_with_unknown_evidence_is_rejected(
    kernel: MemoryKernel, tmp_path: Path
) -> None:
    ids = await _seed(kernel)
    runner = _runner(
        {
            "reflections": [
                {"text": "凭空捏造的洞察", "evidence": ["NONEXISTENT_ID"]},
                {"text": "有据的洞察", "evidence": [ids[0]]},
            ],
            "diary": "",
            "mood_delta": {},
            "demote": [],
        }
    )
    ctx = _ctx(kernel, runner, tmp_path)
    result = await run_builtin(MEMORY_DREAM_BUILTIN_NAME, ctx)
    assert result["reflections"] == 1  # only the evidenced one
    assert result["reflections_rejected"] == 1  # hallucinated one dropped


async def test_dry_run_writes_nothing(
    kernel: MemoryKernel, tmp_path: Path
) -> None:
    ids = await _seed(kernel)
    runner = _runner(
        {
            "reflections": [{"text": "洞察", "evidence": [ids[0]]}],
            "diary": "日记",
            "mood_delta": {"e": 0.2},
            "demote": [ids[1]],
        }
    )
    ctx = _ctx(kernel, runner, tmp_path, dry_run=True)
    result = await run_builtin(MEMORY_DREAM_BUILTIN_NAME, ctx)
    assert result["reflections"] == 1 and result["dry_run"] is True

    stats = await kernel.stats()
    assert stats["items"] == 3, "dry run must not add reflection items"
    assert await kernel.get_affect_state("grantley") == (0.0, 0.0, 0.0)
    async with kernel._conn.execute(  # noqa: SLF001
        "SELECT importance FROM mk_items WHERE id = ?", (ids[1],)
    ) as cur:
        row = await cur.fetchone()
    assert row is not None and row[0] == 0.6  # not demoted


async def test_disabled_and_no_material_envelopes(
    kernel: MemoryKernel, tmp_path: Path
) -> None:
    runner = _runner({})
    ctx = _ctx(kernel, runner, tmp_path, enabled=False)
    assert await run_builtin(MEMORY_DREAM_BUILTIN_NAME, ctx) == {
        "ok": False,
        "reason": "disabled",
    }
    # Enabled but nothing to dream about.
    ctx2 = _ctx(kernel, runner, tmp_path)
    result = await run_builtin(MEMORY_DREAM_BUILTIN_NAME, ctx2)
    assert result["ok"] and result.get("reason") == "no_material"
