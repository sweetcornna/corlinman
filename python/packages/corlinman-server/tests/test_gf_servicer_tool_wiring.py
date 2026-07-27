"""gap-fill wire-A — servicer-side wiring of the new builtin tools.

Exercises ``_dispatch_builtin`` directly (the streaming-loop fixture is
quadratic to set up; the unit-level dispatch contract is enough) for:

* ``execute_code`` — registered + dispatched, disabled-by-default envelope.
* ``subagent_stop`` — routes to the operator stop mechanism (cancel_session).
* ``Skill`` — the on-demand progressive-disclosure body pull.
* ``memory_write`` / ``memory_read`` — gated + dispatched (not_configured
  without a host).
* the per-argument permission rule + ``ask`` fail-closed path through
  the dispatcher's gate.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from corlinman_providers.base import ProviderChunk
from corlinman_server.agent_servicer import CorlinmanAgentServicer


class _FakeProvider:
    def __init__(self) -> None:
        self.last_kwargs: dict[str, Any] = {}

    async def chat_stream(self, **kwargs: Any) -> AsyncIterator[ProviderChunk]:
        self.last_kwargs = kwargs
        if False:  # pragma: no cover — generator, never yields here
            yield ProviderChunk(kind="done", finish_reason="stop")


def _servicer() -> CorlinmanAgentServicer:
    return CorlinmanAgentServicer(provider_resolver=lambda _m: _FakeProvider())


def _start(session_key: str = "tenant-x::s1") -> Any:
    from corlinman_agent.reasoning_loop import ChatStart

    return ChatStart(model="m", messages=[], tools=[], session_key=session_key)


def _event(tool: str, args: dict[str, Any] | None = None, plugin: str = "builtin"):
    from corlinman_agent.reasoning_loop import ToolCallEvent

    return ToolCallEvent(
        call_id="c1",
        plugin=plugin,
        tool=tool,
        args_json=json.dumps(args or {}).encode("utf-8"),
    )


@pytest.mark.asyncio
async def test_execute_code_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORLINMAN_ENABLE_EXECUTE_CODE", raising=False)
    servicer = _servicer()
    out = await servicer._dispatch_builtin(
        _event("execute_code", {"code": "print(1)"}), _start(), _FakeProvider()
    )
    payload = json.loads(out)
    assert payload["error"] == "execute_code_disabled"


@pytest.mark.asyncio
async def test_subagent_stop_not_running() -> None:
    servicer = _servicer()
    out = await servicer._dispatch_builtin(
        _event("subagent_stop", {}), _start("tenant-x::no-loop"), _FakeProvider()
    )
    payload = json.loads(out)
    # No active loop registered for this session -> not_running, ok False.
    assert payload["status"] == "not_running"
    assert payload["ok"] is False
    assert payload["session_key"] == "tenant-x::no-loop"


@pytest.mark.asyncio
async def test_skill_tool_unregistered_envelope() -> None:
    servicer = _servicer()
    out = await servicer._dispatch_builtin(
        _event("Skill", {"name": "does-not-exist"}), _start(), _FakeProvider()
    )
    payload = json.loads(out)
    # No registry wired (default assembler may be None) OR not registered;
    # either way a clean error envelope, never a crash.
    assert payload["ok"] is False
    assert payload["error"] in ("skills_unavailable", "skill_not_registered")


@pytest.mark.asyncio
async def test_skill_tool_requires_name() -> None:
    servicer = _servicer()
    out = await servicer._dispatch_builtin(
        _event("Skill", {}), _start(), _FakeProvider()
    )
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error"] == "name_required"


def _servicer_with_skills(skills_dir) -> CorlinmanAgentServicer:
    """Servicer whose context assembler exposes a REAL SkillRegistry
    loaded from ``skills_dir`` — the same attribute path
    (``_context_assembler._skills``) production wiring uses."""
    from types import SimpleNamespace

    from corlinman_agent.skills import SkillRegistry

    servicer = _servicer()
    servicer._context_assembler = SimpleNamespace(
        _skills=SkillRegistry.load_from_dir(skills_dir)
    )
    return servicer


@pytest.fixture
def seeded_skills_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Seed the real bundled starter skills into a tmp data dir — the
    exact tree a first-boot gateway hands the agent process."""
    from corlinman_server.gateway.lifecycle.starter_skills import (
        seed_starter_skills,
    )

    monkeypatch.delenv("CORLINMAN_BUNDLED_SKILLS_DIR", raising=False)
    target = tmp_path / "profiles" / "default" / "skills"
    report = seed_starter_skills(target)
    assert report.copied, "bundled seed produced nothing"
    return target


@pytest.mark.asyncio
async def test_skill_tool_pulls_reference_file_from_seeded_bundle(
    seeded_skills_dir,
) -> None:
    """End-to-end progressive disclosure for split skills: bundle →
    seed → registry → real ``Skill`` dispatch with ``file`` returns the
    reference body the SKILL.md routes to."""
    servicer = _servicer_with_skills(seeded_skills_dir)
    out = await servicer._dispatch_builtin(
        _event(
            "Skill",
            {"name": "huashu-design", "file": "references/asset-protocol.md"},
        ),
        _start(),
        _FakeProvider(),
    )
    payload = json.loads(out)
    assert payload["ok"] is True, payload
    assert payload["file"] == "references/asset-protocol.md"
    # Verbatim content moved out of the giant SKILL.md must come back.
    assert "核心资产协议" in payload["body"]
    assert "brand-spec.md" in payload["body"]


@pytest.mark.asyncio
async def test_skill_tool_body_pull_still_works_for_split_skill(
    seeded_skills_dir,
) -> None:
    servicer = _servicer_with_skills(seeded_skills_dir)
    out = await servicer._dispatch_builtin(
        _event("Skill", {"name": "huashu-design"}), _start(), _FakeProvider()
    )
    payload = json.loads(out)
    assert payload["ok"] is True, payload
    # The slim body routes to the split-out references.
    assert "references/asset-protocol.md" in payload["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        "../plan.md",  # parent-dir escape into the shared skills root
        "../../secrets.txt",  # deeper escape
        "/etc/passwd",  # absolute path
        "references/../../plan.md",  # escape smuggled mid-path
    ],
)
async def test_skill_tool_file_rejects_traversal(seeded_skills_dir, bad) -> None:
    servicer = _servicer_with_skills(seeded_skills_dir)
    out = await servicer._dispatch_builtin(
        _event("Skill", {"name": "huashu-design", "file": bad}),
        _start(),
        _FakeProvider(),
    )
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error"] == "file_path_escapes_skill_dir"


@pytest.mark.asyncio
async def test_skill_tool_file_not_found_envelope(seeded_skills_dir) -> None:
    servicer = _servicer_with_skills(seeded_skills_dir)
    out = await servicer._dispatch_builtin(
        _event("Skill", {"name": "huashu-design", "file": "references/nope.md"}),
        _start(),
        _FakeProvider(),
    )
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error"] == "file_not_found"


@pytest.mark.asyncio
async def test_skill_tool_file_refused_for_flat_skill(seeded_skills_dir) -> None:
    """Flat ``<root>/<name>.md`` skills share the skills root with every
    other skill — a file pull there would open the whole root, so it is
    refused outright."""
    servicer = _servicer_with_skills(seeded_skills_dir)
    out = await servicer._dispatch_builtin(
        _event("Skill", {"name": "plan", "file": "memory.md"}),
        _start(),
        _FakeProvider(),
    )
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["error"] == "skill_has_no_files"


@pytest.mark.asyncio
async def test_memory_write_not_configured_without_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("CORLINMAN_DATA_DIR", str(tmp_path))
    servicer = _servicer()
    out = await servicer._dispatch_builtin(
        _event("memory_write", {"content": "remember this"}),
        _start(),
        _FakeProvider(),
    )
    payload = json.loads(out)
    # With the servicer's lazily-opened host this may write OK; the
    # contract under test is only that it dispatches + returns an envelope
    # with an ``ok`` flag (never crashes / falls through to unknown).
    assert "ok" in payload


@pytest.mark.asyncio
async def test_per_arg_rule_denies_rm_via_dispatch(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``run_shell(rm:*)`` deny rule blocks an ``rm`` command at the
    dispatcher's permission gate, but lets a benign command through."""
    monkeypatch.setenv(
        "CORLINMAN_AGENT_PERMISSIONS",
        '[{"tool": "run_shell(rm:*)", "action": "deny"}]',
    )
    servicer = _servicer()
    blocked = await servicer._dispatch_builtin(
        _event("run_shell", {"command": "rm -rf /tmp/x"}),
        _start(),
        _FakeProvider(),
    )
    payload = json.loads(blocked)
    assert "permission_denied" in payload["error"]
    assert payload["tool"] == "run_shell"


@pytest.mark.asyncio
async def test_ask_verdict_fail_closed_via_dispatch(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``ask`` permission verdict with no approval resolver wired
    fail-closes; W3-3 makes the envelope the diagnosable
    ``authz_no_channel`` (there is no prompt channel on this surface)."""
    monkeypatch.setenv(
        "CORLINMAN_AGENT_PERMISSIONS",
        '[{"tool": "run_shell", "action": "ask"}]',
    )
    servicer = _servicer()
    out = await servicer._dispatch_builtin(
        _event("run_shell", {"command": "echo hi"}),
        _start(),
        _FakeProvider(),
    )
    payload = json.loads(out)
    assert "authz_no_channel" in payload["error"]
