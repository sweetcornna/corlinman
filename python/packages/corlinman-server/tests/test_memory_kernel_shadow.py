"""Memory W1 — kernel shadow mode in the servicer.

``CORLINMAN_MEMORY_KERNEL`` gates two fire-and-forget lanes: every
completed turn queues an ``mk_observations`` row (with binding/persona
scope fields when the channel provided them), and every recall computes
a shadow kernel recall that is logged but NEVER injected into the
prompt. ``off`` disables both; the legacy lanes are untouched in every
mode.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_memory_kernel import KernelScope, MemoryKernel
from corlinman_server.agent_servicer import CorlinmanAgentServicer


class _FakeProvider:
    def __init__(self) -> None:  # pragma: no cover — never streamed here
        pass


class _NullHost:
    """Legacy host stub: recall lanes no-op, store succeeds."""

    async def recent(self, session_key: str, limit: int) -> list[Any]:
        return []

    async def upsert(self, doc: Any) -> str:
        return "1"


def _servicer(kernel: MemoryKernel) -> CorlinmanAgentServicer:
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider())
    servicer.set_app_state(
        SimpleNamespace(memory_host=_NullHost(), memory_kernel=kernel)
    )
    return servicer


def _start(session: str = "s1", *, extra: dict[str, Any] | None = None) -> Any:
    from corlinman_agent.reasoning_loop import ChatStart

    start = ChatStart(
        model="m",
        messages=[{"role": "user", "content": "my hometown is Harbin"}],
        session_key=session,
    )
    if extra is not None:
        start.extra = extra
    return start


async def _drain_tasks(servicer: CorlinmanAgentServicer) -> None:
    for _ in range(100):
        if not servicer._prefetch_tasks:
            return
        await asyncio.sleep(0.01)


async def test_store_memory_queues_kernel_observation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORLINMAN_MEMORY_KERNEL", "shadow")
    kernel = await MemoryKernel.open(tmp_path / "memory.sqlite")
    servicer = _servicer(kernel)
    try:
        start = _start(
            extra={
                "binding": {"channel": "qq", "sender": "10086"},
                "persona_id": "grantley",
            }
        )
        await servicer._store_memory("s1", "my hometown is Harbin", "nice", start=start)
        await _drain_tasks(servicer)

        (obs,) = await kernel.pending_observations()
        assert obs.session_key == "s1"
        assert (obs.channel, obs.channel_user_id, obs.persona_id) == (
            "qq",
            "10086",
            "grantley",
        )
    finally:
        await servicer.aclose()
        await kernel.close()


async def test_store_memory_off_mode_skips_kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORLINMAN_MEMORY_KERNEL", "off")
    kernel = await MemoryKernel.open(tmp_path / "memory.sqlite")
    servicer = _servicer(kernel)
    try:
        await servicer._store_memory("s1", "hello", "hi", start=_start())
        await _drain_tasks(servicer)
        assert await kernel.pending_observations() == []
    finally:
        await servicer.aclose()
        await kernel.close()


async def test_store_memory_without_start_is_legacy_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Callers that don't thread ``start`` (older call shapes) keep the
    legacy behaviour with no kernel writes and no errors."""
    monkeypatch.setenv("CORLINMAN_MEMORY_KERNEL", "shadow")
    kernel = await MemoryKernel.open(tmp_path / "memory.sqlite")
    servicer = _servicer(kernel)
    try:
        await servicer._store_memory("s1", "hello", "hi")
        await _drain_tasks(servicer)
        assert await kernel.pending_observations() == []
    finally:
        await servicer.aclose()
        await kernel.close()


async def test_shadow_recall_logs_but_never_injects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORLINMAN_MEMORY_KERNEL", "shadow")
    kernel = await MemoryKernel.open(tmp_path / "memory.sqlite")
    # Seed a kernel item that WOULD match the user text.
    await kernel.add_item(
        KernelScope(scope_user_id="10086"),
        text="hometown is Harbin",
        kind="fact",
        source="turn",
    )
    servicer = _servicer(kernel)
    try:
        start = _start(extra={"binding": {"channel": "qq", "sender": "10086"}})
        before = [dict(m) for m in start.messages]
        await servicer._recall_memory(start)
        await _drain_tasks(servicer)

        # Legacy host returned nothing and the kernel lane is shadow-only,
        # so the prompt must be byte-identical.
        assert [dict(m) for m in start.messages] == before
    finally:
        await servicer.aclose()
        await kernel.close()


def test_kernel_mode_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env var (ops kill-switch) > [memory.kernel] TOML mode > shadow."""
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider())
    monkeypatch.delenv("CORLINMAN_MEMORY_KERNEL", raising=False)

    assert servicer._memory_kernel_mode() == "shadow"  # no config at all

    servicer.set_app_state(SimpleNamespace(memory_kernel_config={"mode": "off"}))
    assert servicer._memory_kernel_mode() == "off"  # TOML applies

    servicer.set_app_state(SimpleNamespace(memory_kernel_config={"mode": "bogus"}))
    assert servicer._memory_kernel_mode() == "shadow"  # typo → safe default

    monkeypatch.setenv("CORLINMAN_MEMORY_KERNEL", "on")
    servicer.set_app_state(SimpleNamespace(memory_kernel_config={"mode": "off"}))
    assert servicer._memory_kernel_mode() == "on"  # env wins


async def test_scope_fields_tolerate_missing_binding() -> None:
    fields = CorlinmanAgentServicer._kernel_scope_fields(SimpleNamespace())
    assert fields == {
        "channel": None,
        "channel_user_id": None,
        "persona_id": "",
    }
    fields = CorlinmanAgentServicer._kernel_scope_fields(
        SimpleNamespace(extra={"binding": "not-a-dict", "persona_id": 42})
    )
    assert fields == {
        "channel": None,
        "channel_user_id": None,
        "persona_id": "",
    }
