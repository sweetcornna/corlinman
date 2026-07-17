"""Memory W2 — per-user identity-unified scoping (the privacy fix).

Durable notes move from one global shared ``agent_notes`` namespace to
``facts/{tenant}/{user}/{persona}`` keyed by the cross-channel identity
resolver. The hard requirements pinned here:

- **Scope-leak = 0**: two senders' notes are mutually invisible.
- Same human on two channels shares one scope once the resolver links
  the aliases.
- The model cannot escape the jail with an explicit ``namespace`` arg.
- Unscoped turns (no binding — API-key/SDK callers) keep the legacy
  shared behaviour.
- Legacy pre-scoping notes stay readable via the transition fallback.
- ``merge_users`` re-homes kernel scope rows + legacy note namespaces.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from corlinman_agent.memory.tools import (
    dispatch_memory_read,
    dispatch_memory_search,
    dispatch_memory_write,
)
from corlinman_memory_host import LocalSqliteHost
from corlinman_server.agent_servicer import CorlinmanAgentServicer


class _FakeProvider:
    def __init__(self) -> None:  # pragma: no cover — never streamed here
        pass


class _StaticResolver:
    """Identity-resolver stub: fixed (channel, sender) → user map."""

    def __init__(self, mapping: dict[tuple[str, str], str]) -> None:
        self.mapping = mapping
        self.calls: list[tuple[str, str]] = []

    async def resolve(self, channel: str, sender: str) -> str:
        self.calls.append((channel, sender))
        try:
            return self.mapping[(channel, sender)]
        except KeyError as exc:  # pragma: no cover — test wiring error
            raise RuntimeError("unknown alias") from exc


def _servicer(
    host: Any, resolver: Any | None = None, **state_extra: Any
) -> CorlinmanAgentServicer:
    servicer = CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider())
    servicer.set_app_state(
        SimpleNamespace(
            memory_host=host, identity_resolver=resolver, **state_extra
        )
    )
    return servicer


def _start(channel: str, sender: str, persona: str = "") -> Any:
    from corlinman_agent.reasoning_loop import ChatStart

    start = ChatStart(
        model="m",
        messages=[{"role": "user", "content": "hi"}],
        session_key="s1",
    )
    extra: dict[str, Any] = {"binding": {"channel": channel, "sender": sender}}
    if persona:
        extra["persona_id"] = persona
    start.extra = extra
    return start


@pytest.fixture
async def host(tmp_path: Path) -> Any:
    h = await LocalSqliteHost.open("local", tmp_path / "memory.sqlite")
    try:
        yield h
    finally:
        await h.close()


# ---- scope resolution --------------------------------------------------


async def test_memory_scope_uses_canonical_identity(host: Any) -> None:
    resolver = _StaticResolver({("qq", "10086"): "U1", ("telegram", "77"): "U1"})
    servicer = _servicer(host, resolver)
    try:
        scope_qq = await servicer._memory_scope(_start("qq", "10086"))
        scope_tg = await servicer._memory_scope(_start("telegram", "77"))
        assert scope_qq is not None and scope_tg is not None
        # Same human on two channels → one namespace.
        assert scope_qq["namespace"] == scope_tg["namespace"] == "facts/default/U1/_"
        # Successful resolves are cached — second call hits the LRU.
        await servicer._memory_scope(_start("qq", "10086"))
        assert resolver.calls.count(("qq", "10086")) == 1
    finally:
        await servicer.aclose()


async def test_memory_scope_fails_open_and_does_not_cache_failures(
    host: Any,
) -> None:
    class _Broken:
        calls = 0

        async def resolve(self, channel: str, sender: str) -> str:
            type(self).calls += 1
            raise RuntimeError("identity db locked")

    servicer = _servicer(host, _Broken())
    try:
        scope = await servicer._memory_scope(_start("qq", "10086"))
        assert scope is not None
        # Channel-qualified raw fallback (bare ids collide across channels).
        assert scope["namespace"] == "facts/default/qq:10086/_"
        await servicer._memory_scope(_start("qq", "10086"))
        assert _Broken.calls == 2, "failures must not be cached"
    finally:
        await servicer.aclose()


async def test_memory_scope_none_without_binding_or_when_disabled(
    host: Any,
) -> None:
    from corlinman_agent.reasoning_loop import ChatStart

    servicer = _servicer(host, None)
    try:
        bare = ChatStart(model="m", messages=[], session_key="s1")
        assert await servicer._memory_scope(bare) is None
    finally:
        await servicer.aclose()

    servicer = _servicer(host, None, memory_scope_config={"per_user": False})
    try:
        assert await servicer._memory_scope(_start("qq", "1")) is None
    finally:
        await servicer.aclose()


# ---- tool-level isolation (scope-leak = 0) ------------------------------


async def test_notes_are_isolated_per_user_and_jailed(host: Any) -> None:
    ns_alice = "facts/default/alice/_"
    ns_bob = "facts/default/bob/_"

    out = await dispatch_memory_write(
        json.dumps({"content": "alice likes oolong tea"}).encode(),
        memory_host=host,
        default_namespace=ns_alice,
    )
    assert json.loads(out)["namespace"] == ns_alice

    # Explicit namespace arg cannot escape the jail — including an
    # attempt to name another user's scope outright.
    out = await dispatch_memory_write(
        json.dumps(
            {"content": "malicious note", "namespace": ns_bob}
        ).encode(),
        memory_host=host,
        default_namespace=ns_alice,
    )
    assert json.loads(out)["namespace"] == f"{ns_alice}/{ns_bob}"

    # Bob's scoped search must see NOTHING of alice's notes.
    out = await dispatch_memory_search(
        json.dumps({"query": "oolong tea"}).encode(),
        memory_host=host,
        default_namespace=ns_bob,
    )
    assert json.loads(out)["total"] == 0, "cross-user scope leak"

    # Alice sees her own note.
    out = await dispatch_memory_search(
        json.dumps({"query": "oolong tea"}).encode(),
        memory_host=host,
        default_namespace=ns_alice,
    )
    assert json.loads(out)["total"] == 1


async def test_unscoped_callers_keep_legacy_behaviour(host: Any) -> None:
    out = await dispatch_memory_write(
        json.dumps({"content": "standalone note"}).encode(),
        memory_host=host,
    )
    assert json.loads(out)["namespace"] == "agent_notes"

    # Unscoped search stays global (legacy contract).
    out = await dispatch_memory_search(
        json.dumps({"query": "standalone note"}).encode(),
        memory_host=host,
    )
    assert json.loads(out)["total"] == 1


async def test_scoped_read_falls_back_to_legacy_notes(host: Any) -> None:
    from corlinman_memory_host import MemoryDoc

    await host.upsert(
        MemoryDoc(content="pre-scoping preference", namespace="agent_notes")
    )
    out = await dispatch_memory_read(
        json.dumps({"query": "pre-scoping preference"}).encode(),
        memory_host=host,
        default_namespace="facts/default/U1/_",
        legacy_read_namespace="agent_notes",
    )
    payload = json.loads(out)
    assert payload["total"] == 1, "transition fallback must surface old notes"


# ---- relevance-recall lane ----------------------------------------------


async def test_recall_relevant_notes_scoped_with_fallback(host: Any) -> None:
    from corlinman_memory_host import MemoryDoc

    resolver = _StaticResolver({("qq", "10086"): "U1"})
    servicer = _servicer(host, resolver)
    try:
        await host.upsert(
            MemoryDoc(
                content="favorite editor is helix",
                namespace="facts/default/U1/_",
            )
        )
        start = _start("qq", "10086")
        # Legacy host = implicit-AND BM25: every query word must appear.
        start.messages = [{"role": "user", "content": "favorite editor"}]
        await servicer._recall_relevant_notes(start)
        joined = " ".join(str(m.get("content", "")) for m in start.messages)
        assert "favorite editor is helix" in joined

        # Empty scoped namespace + legacy note → fallback surfaces it.
        await host.upsert(
            MemoryDoc(content="legacy fact about kubernetes", namespace="agent_notes")
        )
        start2 = _start("qq", "10086")
        start2.messages = [{"role": "user", "content": "legacy fact kubernetes"}]
        await servicer._recall_relevant_notes(start2)
        joined2 = " ".join(str(m.get("content", "")) for m in start2.messages)
        assert "legacy fact about kubernetes" in joined2
    finally:
        await servicer.aclose()


# ---- merge re-homing ------------------------------------------------------


async def test_merge_rehomes_kernel_rows_and_note_namespaces(
    tmp_path: Path,
) -> None:
    from corlinman_memory_host import MemoryDoc, MemoryQuery
    from corlinman_memory_kernel import KernelScope, MemoryKernel, Observation, now_ms

    path = tmp_path / "memory.sqlite"
    host = await LocalSqliteHost.open("local", path)
    kernel = await MemoryKernel.open(path)
    try:
        await kernel.add_item(
            KernelScope(scope_user_id="LOSER"),
            text="fact from the losing identity",
            kind="fact",
            source="turn",
        )
        await kernel.observe(
            Observation(
                session_key="s1",
                user_text="x",
                reply_text="y",
                ts_ms=now_ms(),
                scope_user_id="LOSER",
            )
        )
        await host.upsert(
            MemoryDoc(content="old note", namespace="facts/default/LOSER/_")
        )

        moved = await kernel.merge_scope_user("LOSER", "WINNER")
        assert moved == 2
        renamed = await host.rename_namespace_prefix(
            "facts/default/LOSER", "facts/default/WINNER"
        )
        assert renamed == 1

        hits = await kernel.recall(
            KernelScope(scope_user_id="WINNER"), "losing identity fact"
        )
        assert len(hits) == 1
        note_hits = await host.query(
            MemoryQuery(
                text="old note", top_k=5, namespace="facts/default/WINNER/_"
            )
        )
        assert len(note_hits) == 1
        assert (
            await host.query(
                MemoryQuery(
                    text="old note", top_k=5, namespace="facts/default/LOSER/_"
                )
            )
            == []
        )
    finally:
        await kernel.close()
        await host.close()
