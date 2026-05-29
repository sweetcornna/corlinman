"""Acceptance tests for the Python ``PlaceholderEngine`` port (R4-F2).

Ports the Rust ``corlinman_core::placeholder::tests`` suite
(``git show 338e94c~1:rust/crates/corlinman-core/src/placeholder.rs``)
1:1, plus the required B1-BE2 behaviour cases, plus an end-to-end
real-run against a seeded ``episodes.sqlite`` through the SAME
``build_default_engine(state)`` path the gateway boot uses.

The engine echoed every template back verbatim before this landed
(``_NullEngine``); these tests prove the dynamic resolvers actually
resolve now.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
from corlinman_server.gateway.grpc.placeholder import (
    CycleError,
    DepthExceededError,
    PlaceholderCtx,
    PlaceholderEngine,
    PlaceholderEngineLike,
    PlaceholderService,
    ResolverError,
    build_default_engine,
)

# ---------------------------------------------------------------------------
# Fakes mirroring the Rust test resolvers (UpperResolver / RecordingResolver)
# ---------------------------------------------------------------------------


def _ctx() -> PlaceholderCtx:
    return PlaceholderCtx("test", metadata={"trace": "trace-1"})


class _UpperResolver:
    """Mirror of the Rust ``UpperResolver`` — uppercases its key."""

    async def resolve(self, key: str, ctx: object | None = None) -> str:
        return key.upper()


class _RecordingResolver:
    """Mirror of the Rust ``RecordingResolver`` — records every key it
    was asked to resolve so a test can prove namespace routing."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.seen: list[str] = []

    async def resolve(self, key: str, ctx: object | None = None) -> str:
        self.seen.append(key)
        return f"{self.tag}:{key}"


class _BoomResolver:
    """Raises a plain exception so the engine must wrap it into
    :class:`ResolverError`."""

    async def resolve(self, key: str, ctx: object | None = None) -> str:
        raise ValueError("kaboom")


# ---------------------------------------------------------------------------
# Static values  (Rust: static_hit_replaces_token .. bare_token_matches_*)
# ---------------------------------------------------------------------------


async def test_static_hit_replaces_token() -> None:
    eng = PlaceholderEngine().with_static("date.today", "2026-04-20")
    out = await eng.render("today is {{date.today}}", _ctx())
    assert out == "today is 2026-04-20"


async def test_multiple_tokens_all_replaced() -> None:
    eng = (
        PlaceholderEngine()
        .with_static("date.today", "2026-04-20")
        .with_static("system.port", "6005")
        .with_static("user.name", "Nova")
    )
    out = await eng.render(
        "{{user.name}} @ {{date.today}} on port {{system.port}}", _ctx()
    )
    assert out == "Nova @ 2026-04-20 on port 6005"


async def test_empty_token_preserved() -> None:
    eng = PlaceholderEngine()
    out = await eng.render("a {{}} b {{ }} c", _ctx())
    assert out == "a {{}} b {{ }} c"


async def test_utf8_values_and_templates_roundtrip() -> None:
    eng = (
        PlaceholderEngine()
        .with_static("user.name", "小克🐱")
        .with_static("greeting.cn", "你好 世界🌏")
    )
    out = await eng.render("{{greeting.cn}} - 我是 {{user.name}}", _ctx())
    assert out == "你好 世界🌏 - 我是 小克🐱"


async def test_whitespace_inside_braces_is_trimmed() -> None:
    eng = PlaceholderEngine().with_static("date.today", "2026-04-20")
    out = await eng.render("{{ date.today }} / {{  date.today}}", _ctx())
    assert out == "2026-04-20 / 2026-04-20"


async def test_bare_token_matches_default_namespace() -> None:
    eng = PlaceholderEngine().with_static("default.today", "2026-04-20")
    out = await eng.render("today={{today}}", _ctx())
    assert out == "today=2026-04-20"


# ---------------------------------------------------------------------------
# Dynamic resolver  (Rust: dynamic_resolver_handles_namespace,
# test_namespace_routing — key is the post-prefix remainder)
# ---------------------------------------------------------------------------


async def test_dynamic_resolver_handles_namespace() -> None:
    eng = PlaceholderEngine().with_dynamic("upper", _UpperResolver())
    out = await eng.render("{{upper.hello}} / {{upper.world}}", _ctx())
    assert out == "HELLO / WORLD"


async def test_namespace_routing_each_resolver_sees_only_its_keys() -> None:
    agent = _RecordingResolver("agent")
    var = _RecordingResolver("var")
    eng = PlaceholderEngine()
    eng.register_namespace("agent", agent)
    eng.register_namespace("var", var)

    out = await eng.render("{{agent.mentor}} {{var.foo}} {{agent.peer}}", _ctx())
    assert out == "agent:mentor var:foo agent:peer"
    # The resolver receives the remainder after the namespace prefix.
    assert agent.seen == ["mentor", "peer"]
    assert var.seen == ["foo"]


# ---------------------------------------------------------------------------
# Recursion / cycle / depth  (Rust: test_recursion_expand,
# test_cycle_detection, depth_zero_disables_recursion, depth_limit_errors_out)
# ---------------------------------------------------------------------------


async def test_recursion_expand_resolved_value_containing_token() -> None:
    eng = (
        PlaceholderEngine().with_static("a", "{{b}}").with_static("b", "final")
    )
    out = await eng.render("value={{a}}", _ctx())
    assert out == "value=final"


async def test_cycle_detection_raises_cycle_error() -> None:
    eng = PlaceholderEngine().with_static("a", "{{b}}").with_static("b", "{{a}}")
    with pytest.raises(CycleError) as info:
        await eng.render("{{a}}", _ctx())
    # The cycle key is the in-flight token body that re-appeared.
    assert info.value.key in {"a", "b"}


async def test_depth_zero_disables_recursion() -> None:
    eng = (
        PlaceholderEngine()
        .with_static("a", "{{b}}")
        .with_static("b", "final")
        .with_max_depth(0)
    )
    out = await eng.render("{{a}}", _ctx())
    assert out == "{{b}}"


async def test_depth_limit_errors_out() -> None:
    eng = (
        PlaceholderEngine()
        .with_static("l0", "{{l1}}")
        .with_static("l1", "{{l2}}")
        .with_static("l2", "{{l3}}")
        .with_static("l3", "{{l4}}")
        .with_static("l4", "{{l5}}")
        .with_max_depth(2)
    )
    with pytest.raises(DepthExceededError):
        await eng.render("{{l0}}", _ctx())


# ---------------------------------------------------------------------------
# Resolver raise → ResolverError  (Python-specific wrapping contract)
# ---------------------------------------------------------------------------


async def test_resolver_raise_wraps_into_resolver_error() -> None:
    eng = PlaceholderEngine().with_dynamic("boom", _BoomResolver())
    with pytest.raises(ResolverError) as info:
        await eng.render("{{boom.x}}", _ctx())
    assert info.value.namespace == "boom"
    assert "kaboom" in info.value.message


async def test_resolver_placeholder_error_reraised_not_double_wrapped() -> None:
    class _CycleRaiser:
        async def resolve(self, key: str, ctx: object | None = None) -> str:
            raise CycleError("already-a-placeholder-error")

    eng = PlaceholderEngine().with_dynamic("ns", _CycleRaiser())
    with pytest.raises(CycleError):
        await eng.render("{{ns.x}}", _ctx())


# ---------------------------------------------------------------------------
# Back-compat / passthrough / reserved  (Rust: test_flat_backcompat,
# test_unknown_key_passthrough, static_wins_over_dynamic,
# reserved_namespaces_listed)
# ---------------------------------------------------------------------------


async def test_flat_backcompat() -> None:
    eng = (
        PlaceholderEngine()
        .with_static("date.today", "2026-04-20")
        .with_static("date.tomorrow", "2026-04-21")
        .with_static("system.port", "6005")
    )
    out = await eng.render(
        "{{date.today}} -> {{date.tomorrow}} @ {{system.port}}", _ctx()
    )
    assert out == "2026-04-20 -> 2026-04-21 @ 6005"


async def test_unknown_token_preserved_verbatim() -> None:
    eng = PlaceholderEngine().with_static("date.today", "X")
    out = await eng.render(
        "{{mystery.thing}} and {{date.today}} and {{xyz}}", _ctx()
    )
    assert out == "{{mystery.thing}} and X and {{xyz}}"


async def test_static_wins_over_dynamic() -> None:
    eng = (
        PlaceholderEngine()
        .with_static("upper.hello", "static-wins")
        .with_dynamic("upper", _UpperResolver())
    )
    out = await eng.render("{{upper.hello}}", _ctx())
    assert out == "static-wins"


def test_reserved_namespaces_listed() -> None:
    for ns in (
        "var",
        "sar",
        "tar",
        "agent",
        "session",
        "tool",
        "vector",
        "skill",
        "episodes",
    ):
        assert PlaceholderEngine.is_reserved_namespace(ns), f"{ns} should be reserved"
    assert not PlaceholderEngine.is_reserved_namespace("upper")


# ---------------------------------------------------------------------------
# clone_with_max_depth + protocol conformance + service acceptance
# ---------------------------------------------------------------------------


async def test_clone_with_max_depth_shares_registry_new_depth() -> None:
    eng = (
        PlaceholderEngine()
        .with_static("a", "{{b}}")
        .with_static("b", "final")
        .with_dynamic("upper", _UpperResolver())
    )
    cloned = eng.clone_with_max_depth(0)
    assert cloned.max_depth == 0
    assert eng.max_depth == 4  # original untouched
    # Shared registry: dynamic + static carried over.
    assert await cloned.render("{{upper.hi}}", _ctx()) == "HI"
    # Depth 0 disables recursion on the clone.
    assert await cloned.render("{{a}}", _ctx()) == "{{b}}"


def test_engine_satisfies_protocol() -> None:
    eng = PlaceholderEngine()
    assert isinstance(eng, PlaceholderEngineLike)


def test_service_accepts_real_engine() -> None:
    # PlaceholderService must accept the concrete engine without type error.
    svc = PlaceholderService(PlaceholderEngine())
    assert svc is not None


# ===========================================================================
# REAL-RUN: end-to-end {{episodes.*}} resolution against a seeded DB
# ===========================================================================

# Schema lifted from tests/gateway/placeholder/test_episodes_stub.py — the
# exact shape EpisodesResolver reads.
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS episodes (
    id                  TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL DEFAULT 'default',
    started_at          INTEGER NOT NULL,
    ended_at            INTEGER NOT NULL,
    kind                TEXT NOT NULL,
    summary_text        TEXT NOT NULL,
    source_session_keys TEXT NOT NULL DEFAULT '[]',
    source_signal_ids   TEXT NOT NULL DEFAULT '[]',
    source_history_ids  TEXT NOT NULL DEFAULT '[]',
    embedding           BLOB,
    embedding_dim       INTEGER,
    importance_score    REAL NOT NULL DEFAULT 0.5,
    last_referenced_at  INTEGER,
    distilled_by        TEXT NOT NULL,
    distilled_at        INTEGER NOT NULL,
    schema_version      INTEGER NOT NULL DEFAULT 1
);
"""


async def _close_engine_resolvers(engine: object) -> None:
    """Close any cached resolver connections so aiosqlite's worker thread
    shuts down before the event loop does (avoids a ResourceWarning)."""
    dynamic = getattr(engine, "_dynamic", {})
    for resolver in dynamic.values():
        close = getattr(resolver, "close", None)
        if close is not None:
            await close()


async def _seed_episode(root: Path, tenant: str, *, summary: str) -> None:
    """Seed one episode at the per-tenant path EpisodesResolver expects."""
    from corlinman_server.tenancy import TenantId, tenant_db_path

    tid = TenantId.new(tenant)
    path = tenant_db_path(root, tid, "episodes")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(path), isolation_level=None)
    try:
        await conn.executescript(_SCHEMA_SQL)
        await conn.execute(
            """INSERT INTO episodes
                 (id, tenant_id, started_at, ended_at, kind, summary_text,
                  source_session_keys, source_signal_ids, source_history_ids,
                  importance_score, distilled_by, distilled_at, schema_version)
               VALUES (?, ?, ?, ?, ?, ?, '[]', '[]', '[]', ?, 'stub', 0, 1)""",
            (
                "ep-real",
                tenant,
                1_699_999_999_000,
                1_700_000_000_000,
                "conversation",
                summary,
                0.9,
            ),
        )
    finally:
        await conn.close()


async def test_real_episodes_resolve_through_default_engine(tmp_path: Path) -> None:
    """Build the engine via the SAME path the gateway boot uses
    (``build_default_engine(state)``), seed a real episodes.sqlite, and
    assert ``{{episodes.recent}}`` resolves to real content — proving the
    R4-F2 dead-end is fixed end-to-end."""
    await _seed_episode(tmp_path, "default", summary="the real seeded episode")

    state = SimpleNamespace(data_dir=tmp_path)
    engine = build_default_engine(state)
    assert engine is not None, "build_default_engine must return a real engine"

    ctx = PlaceholderCtx("sess-real", metadata={"tenant_id": "default"})

    try:
        # --- BEFORE (the dead-end behaviour): _NullEngine echoes verbatim.
        from corlinman_server.gateway.grpc.placeholder import _NullEngine

        null = _NullEngine()
        before = await null.render("recap: {{episodes.recent}}", ctx)
        assert before == "recap: {{episodes.recent}}"  # echoed back, unresolved

        # --- AFTER (real engine): the token resolves to the seeded summary.
        after = await engine.render("recap: {{episodes.recent}}", ctx)
        assert "the real seeded episode" in after
        assert "{{episodes.recent}}" not in after  # resolved, not echoed

        # An UNREGISTERED namespace token still echoes verbatim.
        other = await engine.render("{{weather.beijing}}", ctx)
        assert other == "{{weather.beijing}}"
    finally:
        await _close_engine_resolvers(engine)


async def test_real_episodes_through_service_unresolved_keys(tmp_path: Path) -> None:
    """Drive the full PlaceholderService.Render seam with a real engine and
    assert the resolved token is NOT in unresolved_keys while an
    unregistered one is."""
    from corlinman_server.gateway.grpc.placeholder import collect_unresolved

    await _seed_episode(tmp_path, "default", summary="service-path episode")
    engine = build_default_engine(SimpleNamespace(data_dir=tmp_path))
    ctx = PlaceholderCtx("sess", metadata={"tenant_id": "default"})

    try:
        rendered = await engine.render(
            "{{episodes.recent}} | {{weather.beijing}}", ctx
        )
        assert "service-path episode" in rendered
        unresolved = collect_unresolved(rendered)
        assert "episodes.recent" not in unresolved
        assert "weather.beijing" in unresolved
    finally:
        await _close_engine_resolvers(engine)


def test_build_default_engine_no_data_dir_returns_null_engine() -> None:
    """A state without a usable data dir must NOT crash boot — it falls
    back to an echo-only engine."""
    engine = build_default_engine(SimpleNamespace())  # no data_dir
    assert engine is not None


# ===========================================================================
# G8: {{persona.*}} / {{user.*}} / {{goals.*}} wire-up
#
# The three resolver classes expose ``resolve(self, key, id: str)`` —
# the per-agent / per-user id, NOT the engine's ``PlaceholderCtx``. The
# engine only ever calls ``resolve(key, ctx)``. Before this landed,
# ``build_default_engine`` never registered these namespaces, so the
# tokens leaked verbatim into the prompt. These tests put STUB stores on
# the engine input (``state``) — the same forward-compatible probe the
# ``memory`` namespace uses — and prove the adapter maps ctx→id and the
# namespaces resolve once a store is reachable.
# ===========================================================================


class _StubPersonaResolverStore:
    """Mirror of ``corlinman_persona`` resolver: ``resolve(key, agent_id)``.

    Resolves ``mood`` from a per-agent dict; unknown agents / keys → "".
    """

    def __init__(self, by_agent: dict[str, dict[str, str]]) -> None:
        self._by_agent = by_agent

    async def resolve(self, key: str, agent_id: str) -> str:
        return self._by_agent.get(agent_id, {}).get(key, "")


class _StubUserModelResolverStore:
    """Mirror of ``corlinman_user_model`` resolver: ``resolve(key, user_id)``.

    The real resolver keys on ``user.interests`` etc.; the stub keys on
    the post-prefix remainder it actually receives from the engine
    (``interests``, ``name``).
    """

    def __init__(self, by_user: dict[str, dict[str, str]]) -> None:
        self._by_user = by_user

    async def resolve(self, key: str, user_id: str) -> str:
        if not user_id:
            return ""
        return self._by_user.get(user_id, {}).get(key, "")


class _StubGoalsResolverStore:
    """Mirror of ``corlinman_goals`` resolver: ``resolve(key, agent_id)``."""

    def __init__(self, by_agent: dict[str, dict[str, str]]) -> None:
        self._by_agent = by_agent

    async def resolve(self, key: str, agent_id: str) -> str:
        if not agent_id:
            return ""
        return self._by_agent.get(agent_id, {}).get(key, "")


def _g8_state(tmp_path: Path) -> SimpleNamespace:
    """An AppState carrying a data dir + stub persona/user/goals resolvers
    on the documented attribute names ``build_default_engine`` probes."""
    return SimpleNamespace(
        data_dir=tmp_path,
        persona_resolver=_StubPersonaResolverStore(
            {"agent-x": {"mood": "curious"}}
        ),
        user_model_resolver=_StubUserModelResolverStore(
            {"user-1": {"interests": "rust, sqlite", "name": "Nova"}}
        ),
        goals_resolver=_StubGoalsResolverStore(
            {"agent-x": {"weekly": "- ship the port: score 8 — on track"}}
        ),
    )


async def test_g8_persona_user_goals_leak_without_wiring(tmp_path: Path) -> None:
    """REPRODUCE: today a NullEngine (no stores on state) echoes the three
    tokens back verbatim — they surface in ``unresolved_keys``."""
    from corlinman_server.gateway.grpc.placeholder import collect_unresolved

    # No stores published → echo-only fallback (the pre-G8 behaviour for
    # these namespaces).
    null_state = SimpleNamespace(data_dir=tmp_path)
    engine = build_default_engine(null_state)
    ctx = PlaceholderCtx(
        "sess",
        metadata={"agent_id": "agent-x", "user_id": "user-1"},
    )
    template = "{{persona.mood}} {{user.interests}} {{goals.weekly}}"
    try:
        rendered = await engine.render(template, ctx)
        unresolved = collect_unresolved(rendered)
        # All three leak verbatim — no resolver registered.
        assert "persona.mood" in unresolved
        assert "user.interests" in unresolved
        assert "goals.weekly" in unresolved
    finally:
        await _close_engine_resolvers(engine)


async def test_g8_persona_user_goals_resolve_when_stores_present(
    tmp_path: Path,
) -> None:
    """AFTER: with stub stores on the engine input, the adapter maps
    ctx→id and the three namespaces resolve to their source values and
    drop out of ``unresolved_keys``."""
    from corlinman_server.gateway.grpc.placeholder import collect_unresolved

    engine = build_default_engine(_g8_state(tmp_path))
    ctx = PlaceholderCtx(
        "sess",
        metadata={"agent_id": "agent-x", "user_id": "user-1"},
    )
    template = "{{persona.mood}} {{user.interests}} {{goals.weekly}}"
    try:
        rendered = await engine.render(template, ctx)
        assert "curious" in rendered
        assert "rust, sqlite" in rendered
        assert "ship the port: score 8" in rendered
        # None of the three leak verbatim anymore.
        assert "{{persona.mood}}" not in rendered
        assert "{{user.interests}}" not in rendered
        assert "{{goals.weekly}}" not in rendered
        unresolved = collect_unresolved(rendered)
        assert "persona.mood" not in unresolved
        assert "user.interests" not in unresolved
        assert "goals.weekly" not in unresolved
    finally:
        await _close_engine_resolvers(engine)


async def test_g8_adapter_maps_ctx_metadata_to_id(tmp_path: Path) -> None:
    """The adapter must hand the resolver the id pulled from
    ``ctx.metadata`` (``agent_id`` for persona, ``user_id`` for user).
    A missing id → empty source lookup → token consumed (not leaked)."""
    engine = build_default_engine(_g8_state(tmp_path))
    # ctx with NO agent_id/user_id → the adapter passes "" → stub returns
    # "" → token resolves to empty string (consumed, not echoed verbatim).
    ctx = PlaceholderCtx("sess", metadata={})
    try:
        rendered = await engine.render("[{{persona.mood}}]", ctx)
        assert rendered == "[]"
    finally:
        await _close_engine_resolvers(engine)
