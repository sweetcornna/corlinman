"""W6 — affect lens server wiring: embed seam, mood updates, injection.

The embed closure reads live state per call (hot-swap safe); the observe
background task nudges the persona mood; reconcile stamps affect on new
items; injection passes mood + weight only when [memory.affect] enables
the lens.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_memory_kernel import MemoryKernel
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


def _toy_embed_factory() -> Any:
    """Sentiment-ish toy embedder over a 3-dim space (see kernel tests)."""
    from corlinman_memory_kernel.affect import ANCHOR_WORDS

    e_pos, e_neg = ANCHOR_WORDS["e"]

    async def _embed(text: str) -> list[float]:
        score = 0.0
        for w in e_pos:
            if w in text:
                score += 1.0
        for w in e_neg:
            if w in text:
                score -= 1.0
        if text in e_pos:
            return [1.0, 0.0, 0.0]
        if text in e_neg:
            return [-1.0, 0.0, 0.0]
        # Anchor words for p/a axes:
        from corlinman_memory_kernel.affect import ANCHOR_WORDS as AW

        if text in AW["p"][0]:
            return [0.0, 1.0, 0.0]
        if text in AW["p"][1]:
            return [0.0, -1.0, 0.0]
        if text in AW["a"][0]:
            return [0.0, 0.0, 1.0]
        if text in AW["a"][1]:
            return [0.0, 0.0, -1.0]
        return [max(-1.0, min(1.0, score)), 0.0, 0.0]

    return _embed


async def test_c2_embed_closure_reads_live_state(
    tmp_path: Path, close_c2_handles: Any
) -> None:
    from corlinman_server.gateway.core.state import AppState
    from corlinman_server.gateway.lifecycle.entrypoint import _wire_c2_handles

    class _Reg:
        def __init__(self) -> None:
            self.calls: list[tuple[str, tuple[str, ...]]] = []

        def get(self, name: str) -> Any:
            reg = self

            class _P:
                async def embed(
                    self, *, model: str, inputs: Any, extra: Any = None
                ) -> list[list[float]]:
                    reg.calls.append((model, tuple(inputs)))
                    return [[0.1, 0.2]]

            return _P()

    state = AppState()
    state.data_dir = tmp_path
    app = SimpleNamespace(state=SimpleNamespace())
    try:
        await _wire_c2_handles(app, state, None, tmp_path, cfg={})
        embed_fn = state.memory_embed_fn
        assert embed_fn is not None

        # Unconfigured → None (no registry / no embedding section).
        assert await embed_fn("hello") is None

        # Configure live — the SAME closure picks it up (hot-swap safe).
        reg = _Reg()
        state.provider_registry = reg
        state.config = {
            "embedding": {"provider": "openai", "model": "emb-3", "enabled": True}
        }
        vec = await embed_fn("hello")
        assert vec == [0.1, 0.2]
        assert reg.calls == [("emb-3", ("hello",))]

        # Disabled section → None again, no provider call.
        state.config = {
            "embedding": {"provider": "openai", "model": "emb-3", "enabled": False}
        }
        assert await embed_fn("hello") is None
    finally:
        await close_c2_handles(state, app)


async def test_observe_updates_persona_mood(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORLINMAN_MEMORY_KERNEL", "shadow")
    kernel = await MemoryKernel.open(tmp_path / "memory.sqlite")
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider())
    servicer.set_app_state(
        SimpleNamespace(
            memory_host=_NullHost(),
            memory_kernel=kernel,
            identity_resolver=None,
            memory_embed_fn=_toy_embed_factory(),
            memory_affect_config={"enabled": True, "alpha": 0.2},
        )
    )
    try:
        from corlinman_agent.reasoning_loop import ChatStart

        start = ChatStart(
            model="m",
            messages=[{"role": "user", "content": "today was wonderful and happy"}],
            session_key="s1",
        )
        start.extra = {
            "binding": {"channel": "qq", "sender": "1"},
            "persona_id": "grantley",
        }
        await servicer._store_memory(
            "s1", "today was wonderful and happy", "great!", start=start
        )
        for _ in range(100):
            mood = await kernel.get_affect_state("grantley")
            if mood[0] > 0:
                break
            await asyncio.sleep(0.01)
        assert mood[0] > 0.0, "positive turn must nudge mood positive"
        assert mood[0] < 0.5, "EMA must nudge, not yank"
    finally:
        await servicer.aclose()
        await kernel.close()


async def test_affect_disabled_means_no_mood_and_classic_ranking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORLINMAN_MEMORY_KERNEL", "shadow")
    kernel = await MemoryKernel.open(tmp_path / "memory.sqlite")
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider())
    servicer.set_app_state(
        SimpleNamespace(
            memory_host=_NullHost(),
            memory_kernel=kernel,
            identity_resolver=None,
            memory_embed_fn=_toy_embed_factory(),
            memory_affect_config={"enabled": False},
        )
    )
    try:
        from corlinman_agent.reasoning_loop import ChatStart

        start = ChatStart(
            model="m",
            messages=[{"role": "user", "content": "today was wonderful and happy"}],
            session_key="s1",
        )
        start.extra = {
            "binding": {"channel": "qq", "sender": "1"},
            "persona_id": "grantley",
        }
        await servicer._store_memory(
            "s1", "today was wonderful and happy", "great!", start=start
        )
        await asyncio.sleep(0.2)
        assert await kernel.get_affect_state("grantley") == (0.0, 0.0, 0.0)
    finally:
        await servicer.aclose()
        await kernel.close()


async def test_reconcile_stamps_embedding_and_affect(tmp_path: Path) -> None:
    import json

    from corlinman_memory_kernel import Observation, now_ms
    from corlinman_server.scheduler.builtins import (
        MEMORY_RECONCILE_BUILTIN_NAME,
        BuiltinContext,
        run_builtin,
    )

    kernel = await MemoryKernel.open(tmp_path / "memory.sqlite")
    try:
        await kernel.observe(
            Observation(
                session_key="s1",
                user_text="yesterday was wonderful, the demo was happy news",
                reply_text="great",
                ts_ms=now_ms(),
                scope_user_id="U1",
            )
        )

        async def runner(prompt: str) -> dict[str, Any]:
            return {
                "ok": True,
                "reply": json.dumps(
                    [
                        {
                            "topic": "demo",
                            "kind": "project_context",
                            "summary": "the demo went wonderful and happy",
                            "confidence": 0.9,
                        }
                    ]
                ),
            }

        ctx = BuiltinContext(
            app_state=SimpleNamespace(
                memory_kernel=kernel,
                agent_runner_fn=runner,
                memory_curator_config={"enabled": True, "dry_run": False},
                memory_embed_fn=_toy_embed_factory(),
                data_dir=tmp_path,
            )
        )
        result = await run_builtin(MEMORY_RECONCILE_BUILTIN_NAME, ctx)
        assert result["added"] == 1

        async with kernel._conn.execute(  # noqa: SLF001
            "SELECT embedding, affect_e, affect_salience FROM mk_items"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["embedding"] is not None
        assert row["affect_e"] > 0.0 and row["affect_salience"] > 0.0
    finally:
        await kernel.close()
