"""Tests for :class:`ContextAssembler` — the B2-BE4 integrated pipeline.

Covers the five-stage contract plus six golden snapshot fixtures that
nail down representative prompt-template inputs. Dependencies are stubbed:

* :class:`PlaceholderClient` — replaced with :class:`_StubPlaceholderClient`
  so the tests don't require a live Rust gateway.
* :class:`VariableCascade` — built against ``tmp_path`` directories using
  ``monkeypatch.setenv`` for the ``Var``/``Sar`` tiers.
* :class:`SkillRegistry` / :class:`AgentCardRegistry` — real instances,
  loaded from ``*.md`` / ``*.yaml`` fixtures written into ``tmp_path``.
* :class:`HookEmitter` — captured via :class:`RecordingHookEmitter`.

The goldens in ``tests/fixtures/golden/batch2/`` exist to detect silent
drift in the pipeline's output shape across refactors. Regenerating them
is intentional — see :func:`_REGENERATE_GOLDENS` below.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from corlinman_agent.agents import AgentCardRegistry
from corlinman_agent.context_assembler import AssembledContext, ContextAssembler
from corlinman_agent.hooks import RecordingHookEmitter
from corlinman_agent.placeholder_client import RenderResult
from corlinman_agent.skills import SkillRegistry
from corlinman_agent.variables import VariableCascade

# Set to True locally (or export ``_REGENERATE_GOLDENS=1``) to rewrite
# the golden ``.out.json`` files after an intentional pipeline change.
# Never commit as True.
_REGENERATE_GOLDENS = os.environ.get("_REGENERATE_GOLDENS") == "1"

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden" / "batch2"


# --------------------------------------------------------------------------- #
# Stub PlaceholderClient                                                       #
# --------------------------------------------------------------------------- #


class _StubPlaceholderClient:
    """In-memory stand-in for :class:`PlaceholderClient`.

    ``substitutions`` is a ``{full-token -> replacement}`` dict — the
    stub substitutes those and reports the remaining ``{{namespace.*}}``
    tokens as unresolved. Each render call is recorded so tests can
    assert *what* reached the placeholder stage.
    """

    def __init__(self, substitutions: dict[str, str] | None = None) -> None:
        self.substitutions = substitutions or {}
        self.calls: list[dict[str, Any]] = []

    async def render(
        self,
        *,
        template: str,
        session_key: str,
        model_name: str = "",
        metadata=None,
        max_depth: int = 0,
    ) -> RenderResult:
        self.calls.append(
            {
                "template": template,
                "session_key": session_key,
                "model_name": model_name,
                "metadata": dict(metadata or {}),
            }
        )
        out = template
        for key, value in self.substitutions.items():
            out = out.replace("{{" + key + "}}", value)
        import re

        unresolved: list[str] = []
        for m in re.finditer(r"\{\{([A-Za-z][A-Za-z0-9_]*\.[^{}]+?)\}\}", out):
            k = m.group(1).strip()
            if k and k not in unresolved:
                unresolved.append(k)
        return RenderResult(rendered=out, unresolved_keys=unresolved)


# --------------------------------------------------------------------------- #
# Fixture helpers                                                              #
# --------------------------------------------------------------------------- #


def _write_agent(dir_: Path, name: str, body: str) -> None:
    (dir_ / f"{name}.yaml").write_text(body, encoding="utf-8")


def _write_skill(dir_: Path, filename: str, body: str) -> None:
    (dir_ / filename).write_text(body, encoding="utf-8")


def _make_cascade(
    tmp_path: Path,
    *,
    tar: dict[str, str] | None = None,
    var: dict[str, str] | None = None,
    sar: dict[str, str] | None = None,
) -> VariableCascade:
    """Build a :class:`VariableCascade` with per-tier file backing.

    Writes each ``{stem -> text}`` entry as ``<tier>/<stem>.txt`` so the
    cascade's real loaders find it. ``hot_reload=False`` is set
    everywhere — tests don't need the watcher and starting one forces
    async fixture setup."""
    tar_dir = tmp_path / "tar"
    var_dir = tmp_path / "var"
    sar_dir = tmp_path / "sar"
    fixed_dir = tmp_path / "fixed"
    for d in (tar_dir, var_dir, sar_dir, fixed_dir):
        d.mkdir(parents=True, exist_ok=True)
    for stem, text in (tar or {}).items():
        (tar_dir / f"{stem}.txt").write_text(text, encoding="utf-8")
    for stem, text in (var or {}).items():
        (var_dir / f"{stem}.txt").write_text(text, encoding="utf-8")
    for stem, text in (sar or {}).items():
        (sar_dir / f"{stem}.txt").write_text(text, encoding="utf-8")
    return VariableCascade(tar_dir, var_dir, sar_dir, fixed_dir, hot_reload=False)


def _make_assembler(
    tmp_path: Path,
    *,
    agents_body: dict[str, str] | None = None,
    skills: list[tuple[str, str]] | None = None,
    cascade: VariableCascade | None = None,
    placeholder: _StubPlaceholderClient | None = None,
    hook: RecordingHookEmitter | None = None,
    config_lookup=None,
    single_agent_gate: bool = True,
    default_skill_refs: tuple[str, ...] = (),
) -> tuple[ContextAssembler, RecordingHookEmitter, _StubPlaceholderClient]:
    """One-liner assembler constructor used across the tests."""
    agents_dir = tmp_path / "agents"
    skills_dir = tmp_path / "skills"
    agents_dir.mkdir(parents=True, exist_ok=True)
    skills_dir.mkdir(parents=True, exist_ok=True)

    for name, body in (agents_body or {}).items():
        _write_agent(agents_dir, name, body)
    for filename, body in skills or []:
        _write_skill(skills_dir, filename, body)

    ag = AgentCardRegistry.load_from_dir(agents_dir)
    sk = SkillRegistry.load_from_dir(skills_dir)
    vc = cascade if cascade is not None else _make_cascade(tmp_path)
    ph = placeholder if placeholder is not None else _StubPlaceholderClient()
    hk = hook if hook is not None else RecordingHookEmitter()
    cfg = config_lookup if config_lookup is not None else (lambda _k: None)

    assembler = ContextAssembler(
        agents=ag,
        variables=vc,
        skills=sk,
        placeholder_client=ph,  # type: ignore[arg-type]
        hook_emitter=hk,
        config_lookup=cfg,
        single_agent_gate=single_agent_gate,
        default_skill_refs=default_skill_refs,
    )
    return assembler, hk, ph


# --------------------------------------------------------------------------- #
# 1. happy path                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_pipeline_happy_path(tmp_path: Path) -> None:
    """Full five-stage run. Asserts every stage produces an effect."""
    cascade = _make_cascade(tmp_path, tar={"Mood": "cheerful"})
    stub = _StubPlaceholderClient(substitutions={"session.user_id": "u-42"})
    assembler, hook, _ = _make_assembler(
        tmp_path,
        agents_body={
            "mentor": (
                "name: mentor\n"
                "description: d\n"
                "system_prompt: |\n"
                "  You are a {{TarMood}} senior dev for {{session.user_id}}.\n"
                "skill_refs:\n"
                "  - codereview\n"
            ),
        },
        skills=[
            (
                "codereview.md",
                "---\nname: codereview\ndescription: Review code carefully\n---\n"
                "Always check the tests.\n",
            )
        ],
        cascade=cascade,
        placeholder=stub,
    )

    messages = [
        {"role": "system", "content": "{{agent.mentor}}"},
        {"role": "user", "content": "hi"},
    ]
    result = await assembler.assemble(
        messages, session_key="sess-1", model_name="gpt-4"
    )

    sys_content = result.messages[0]["content"]
    # Stage 1: agent expanded.
    assert "senior dev" in sys_content
    # Stage 2: cascade var substituted.
    assert "cheerful" in sys_content
    assert "{{TarMood}}" not in sys_content
    # Stage 3: skill body injected.
    assert "## Skill: codereview" in sys_content
    assert "Always check the tests." in sys_content
    # Stage 4: placeholder engine resolved the namespaced token.
    assert "u-42" in sys_content
    # Stage 5: hook fired.
    assert [e[0] for e in hook.events] == ["message.preprocessed"]
    assert result.expanded_agent == "mentor"
    assert result.unresolved_keys == []
    assert result.skill_errors == []


# --------------------------------------------------------------------------- #
# 2. agent expansion must run before cascade                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_agent_expansion_happens_before_cascade(tmp_path: Path) -> None:
    """If the agent body carries a cascade var, stage 2 must see it.

    We don't set ``TimeVar`` here — the fixed registry always resolves
    it — so we assert the fixed tier fired post-expansion.
    """
    cascade = _make_cascade(tmp_path)
    assembler, _, _ = _make_assembler(
        tmp_path,
        agents_body={
            "clock": (
                "name: clock\n"
                "description: d\n"
                "system_prompt: |\n"
                "  Current time is {{TimeVar}}.\n"
            ),
        },
        cascade=cascade,
    )

    messages = [{"role": "system", "content": "{{agent.clock}}"}]
    result = await assembler.assemble(
        messages, session_key="s", model_name="gpt"
    )

    body = result.messages[0]["content"]
    assert "{{TimeVar}}" not in body
    # The fixed resolver produces an ISO-8601 time string ending in Z.
    assert "Current time is " in body
    assert "Z" in body


# --------------------------------------------------------------------------- #
# 3. all four cascade tiers substitute                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cascade_subs_fixed_var_sar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixed / Tar / Var / Sar: all four tiers produce substitutions."""
    # Tar file → "TarWelcome" resolves to a file stem "Welcome".
    # Var env → VarUser reads os.environ["VarUser"].
    # Sar → SarPrompt1 + SarModel1 must contain our model.
    monkeypatch.setenv("VarUser", "alice")
    monkeypatch.setenv("SarModel1", "gpt-4,claude-3")

    cascade = _make_cascade(
        tmp_path,
        tar={"Welcome": "hello world"},
        sar={"SarPrompt1": "sar-fired"},
    )
    assembler, _, _ = _make_assembler(tmp_path, cascade=cascade)

    messages = [
        {
            "role": "system",
            "content": (
                "date={{Date}} user={{VarUser}} tar={{TarWelcome}} sar={{SarPrompt1}}"
            ),
        }
    ]
    result = await assembler.assemble(
        messages, session_key="s", model_name="gpt-4"
    )

    body = result.messages[0]["content"]
    assert "user=alice" in body
    assert "tar=hello world" in body
    assert "sar=sar-fired" in body
    # Date is the built-in fixed resolver → non-empty ISO date.
    assert "date=" in body
    assert "{{Date}}" not in body


# --------------------------------------------------------------------------- #
# 4. skill injection                                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_skill_injection_when_agent_refs_skill(tmp_path: Path) -> None:
    """A skill referenced by the expanded agent lands in the system
    prompt ahead of the authored text."""
    assembler, _, _ = _make_assembler(
        tmp_path,
        agents_body={
            "reviewer": (
                "name: reviewer\n"
                "description: d\n"
                "system_prompt: |\n"
                "  Authored body here.\n"
                "skill_refs:\n"
                "  - lint\n"
            ),
        },
        skills=[
            (
                "lint.md",
                "---\nname: lint\ndescription: Enforce linting rules\n---\n"
                "Always run ruff before committing.\n",
            )
        ],
    )

    messages = [{"role": "system", "content": "{{agent.reviewer}}"}]
    result = await assembler.assemble(
        messages, session_key="s", model_name="gpt"
    )

    body = result.messages[0]["content"]
    assert body.index("## Skill: lint") < body.index("Authored body here.")
    assert "Always run ruff" in body


# --------------------------------------------------------------------------- #
# 5. skill with failing requirements is skipped and recorded                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_skill_with_failing_requirements_is_skipped_and_recorded(
    tmp_path: Path,
) -> None:
    """Requirement miss → skill body NOT injected; skill_errors captures
    the problem message."""
    assembler, _, _ = _make_assembler(
        tmp_path,
        agents_body={
            "webby": (
                "name: webby\n"
                "description: d\n"
                "system_prompt: |\n"
                "  Authored.\n"
                "skill_refs:\n"
                "  - search\n"
            ),
        },
        skills=[
            (
                "search.md",
                "---\n"
                "name: search\n"
                "description: Web search\n"
                "metadata:\n"
                "  openclaw:\n"
                "    requires:\n"
                "      config:\n"
                "        - providers.brave.api_key\n"
                "---\n"
                "Use Brave Search.\n",
            )
        ],
        # Config lookup always returns None → requirement fails.
        config_lookup=lambda _k: None,
    )

    messages = [{"role": "system", "content": "{{agent.webby}}"}]
    result = await assembler.assemble(
        messages, session_key="s", model_name="gpt"
    )

    body = result.messages[0]["content"]
    assert "Use Brave Search." not in body
    assert "## Skill: search" not in body
    assert any("providers.brave.api_key" in e for e in result.skill_errors)


# --------------------------------------------------------------------------- #
# 6. placeholder pass handles session.* namespace                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_placeholder_pass_handles_session_namespace(tmp_path: Path) -> None:
    """A ``{{session.user_id}}`` token must survive stages 1-3 and be
    resolved by the placeholder stub in stage 4."""
    stub = _StubPlaceholderClient(substitutions={"session.user_id": "u-99"})
    assembler, _, _ = _make_assembler(tmp_path, placeholder=stub)

    messages = [
        {"role": "system", "content": "hello {{session.user_id}}"},
    ]
    result = await assembler.assemble(
        messages, session_key="s", model_name="gpt"
    )

    assert result.messages[0]["content"] == "hello u-99"
    # The placeholder stub must have seen the content that emerged from
    # stages 1-3 — untouched in this case.
    assert stub.calls[0]["template"] == "hello {{session.user_id}}"


# --------------------------------------------------------------------------- #
# 7. unresolved bare keys propagate                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_unresolved_keys_propagated(tmp_path: Path) -> None:
    """A bare ``{{UnknownVar}}`` is left literal by the cascade, remains
    untouched by the placeholder engine (stub returns it), and appears
    in ``unresolved_keys`` via the cascade path."""
    assembler, _, _ = _make_assembler(tmp_path)

    messages = [{"role": "system", "content": "x={{UnknownVar}} y={{TarMissing}}"}]
    result = await assembler.assemble(
        messages, session_key="s", model_name="gpt"
    )

    assert "UnknownVar" in result.unresolved_keys
    assert "TarMissing" in result.unresolved_keys
    # Tokens survive the pipeline literal.
    assert "{{UnknownVar}}" in result.messages[0]["content"]


# --------------------------------------------------------------------------- #
# 8. hook fires                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_preprocessed_hook_fires(tmp_path: Path) -> None:
    """The ``message.preprocessed`` hook carries the expected payload."""
    assembler, hook, _ = _make_assembler(tmp_path)

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello there"},
    ]
    await assembler.assemble(
        messages,
        session_key="sess-xyz",
        model_name="gpt",
        metadata={"is_group": "true", "group_id": "g-1"},
    )

    assert len(hook.events) == 1
    kind, payload = hook.events[0]
    assert kind == "message.preprocessed"
    assert payload["session_key"] == "sess-xyz"
    assert payload["transcript"] == "hello there"
    assert payload["is_group"] is True
    assert payload["group_id"] == "g-1"


# --------------------------------------------------------------------------- #
# 9. empty messages / no privileged rows                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_assembly_without_privileged_messages(tmp_path: Path) -> None:
    """User-only transcripts still get cascade-var substitution but no
    placeholder render stage."""
    stub = _StubPlaceholderClient()
    assembler, hook, _ = _make_assembler(tmp_path, placeholder=stub)

    messages = [{"role": "user", "content": "just user {{Date}}"}]
    result = await assembler.assemble(
        messages, session_key="s", model_name="gpt"
    )

    # Stage 2 applied: Date is a fixed resolver, so substituted.
    assert "{{Date}}" not in result.messages[0]["content"]
    # No placeholder calls — privileged gate kept them out.
    assert stub.calls == []
    assert len(hook.events) == 1


# --------------------------------------------------------------------------- #
# Golden snapshots                                                             #
# --------------------------------------------------------------------------- #


def _assembled_to_snapshot(result: AssembledContext) -> dict[str, Any]:
    """Serialise an :class:`AssembledContext` into a stable dict for
    snapshot diffing. Excludes timing-sensitive fields like hook
    timestamps and collapses sequences to plain lists."""
    return {
        "messages": result.messages,
        "expanded_agent": result.expanded_agent,
        "muted_agents": result.muted_agents,
        "unresolved_keys": result.unresolved_keys,
        "skill_errors": result.skill_errors,
    }


async def _run_golden(
    tmp_path: Path,
    name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive one golden fixture pair ``<name>.in.json`` / ``.out.json``.

    The fixture ``.in.json`` shape::

        {
          "messages": [...],
          "session_key": "...",
          "model_name": "...",
          "metadata": {...},
          "agents": {"name": "yaml body", ...},
          "skills": [["file.md", "body"], ...],
          "tar": {"stem": "text", ...},
          "var": {"stem": "text", ...},
          "sar": {"stem": "text", ...},
          "env": {"VarUser": "...", ...},
          "placeholder_subs": {"session.x": "..."}
        }
    """
    in_path = GOLDEN_DIR / f"{name}.in.json"
    out_path = GOLDEN_DIR / f"{name}.out.json"
    payload = json.loads(in_path.read_text(encoding="utf-8"))

    for k, v in (payload.get("env") or {}).items():
        monkeypatch.setenv(k, v)

    cascade = _make_cascade(
        tmp_path,
        tar=payload.get("tar") or {},
        var=payload.get("var") or {},
        sar=payload.get("sar") or {},
    )
    stub = _StubPlaceholderClient(substitutions=payload.get("placeholder_subs") or {})

    assembler, _, _ = _make_assembler(
        tmp_path,
        agents_body=payload.get("agents") or {},
        skills=[tuple(pair) for pair in (payload.get("skills") or [])],
        cascade=cascade,
        placeholder=stub,
        single_agent_gate=payload.get("single_agent_gate", True),
    )

    result = await assembler.assemble(
        payload["messages"],
        session_key=payload["session_key"],
        model_name=payload["model_name"],
        metadata=payload.get("metadata") or {},
    )

    snapshot = _assembled_to_snapshot(result)

    if _REGENERATE_GOLDENS or not out_path.exists():
        out_path.write_text(
            json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        if not _REGENERATE_GOLDENS:
            return  # first-run materialisation

    expected = json.loads(out_path.read_text(encoding="utf-8"))
    assert snapshot == expected, (
        f"golden {name} drifted; rerun with _REGENERATE_GOLDENS=1 "
        f"after an intentional pipeline change.\n"
        f"diff actual vs expected:\n"
        f"actual: {json.dumps(snapshot, indent=2, sort_keys=True, ensure_ascii=False)}\n"
        f"expected: {json.dumps(expected, indent=2, sort_keys=True, ensure_ascii=False)}"
    )


@pytest.mark.parametrize(
    "name",
    [
        "01_simple_system",
        "02_agent_expansion",
        "03_cascade_all_tiers",
        "04_skill_injection",
        "05_single_agent_gate_mute",
        "06_mixed_placeholders",
        "07_chinese_agent_name",
        "08_nested_agent_reference_depth_2",
        "09_single_agent_gate_cross_message",
        "10_unresolved_tokens_preserved",
        "11_empty_content_and_non_string_safety",
        "12_system_inject_prefix_variations",
    ],
)
@pytest.mark.asyncio
async def test_golden_snapshot(
    name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive each golden fixture through the pipeline and compare its
    serialised output to the committed ``.out.json``."""
    # Goldens may set env vars — the monkeypatch fixture undoes them
    # between tests so cross-contamination stays impossible.
    assert GOLDEN_DIR.exists(), f"missing golden dir {GOLDEN_DIR}"
    await _run_golden(tmp_path, name, monkeypatch)


# --------------------------------------------------------------------------- #
# default_skill_refs — always-on skills reach the MAIN agent (v1.12.3)         #
# --------------------------------------------------------------------------- #

_DOCGEN_SKILL = (
    "document-generator.md",
    "---\nname: document-generator\ndescription: make clean PDFs\n---\n"
    "Use corlinman-md2pdf; never hand-roll a PDF.\n",
)


@pytest.mark.asyncio
async def test_default_skill_injected_without_agent_card(tmp_path: Path) -> None:
    """v1.12.3 — a plain user turn that invokes NO agent card still gets the
    default skill injected. This is the fix: the main bot agent never matched
    the ``expanded_agent`` gate, so it previously received zero skills."""
    assembler, _, _ = _make_assembler(
        tmp_path,
        skills=[_DOCGEN_SKILL],
        default_skill_refs=("document-generator",),
    )
    result = await assembler.assemble(
        [{"role": "user", "content": "把这个总结成 pdf"}],
        session_key="s",
        model_name="gpt-4",
    )
    sys_content = result.messages[0]["content"]
    assert "## Skill: document-generator" in sys_content
    assert "never hand-roll a PDF" in sys_content
    assert result.skill_errors == []
    assert result.expanded_agent is None  # no card was invoked


@pytest.mark.asyncio
async def test_no_default_skill_means_no_injection(tmp_path: Path) -> None:
    """Without default_skill_refs, a card-less turn gets no skill (the old
    behaviour stays intact for callers that don't opt in)."""
    assembler, _, _ = _make_assembler(tmp_path, skills=[_DOCGEN_SKILL])
    result = await assembler.assemble(
        [{"role": "user", "content": "hi"}], session_key="s", model_name="gpt-4"
    )
    joined = " ".join(
        m.get("content", "") for m in result.messages if isinstance(m.get("content"), str)
    )
    assert "## Skill:" not in joined


@pytest.mark.asyncio
async def test_default_and_card_skills_merge_deduped(tmp_path: Path) -> None:
    """When a card IS invoked, its skill_refs merge with the defaults without
    duplicating a shared name."""
    assembler, _, _ = _make_assembler(
        tmp_path,
        agents_body={
            "mentor": (
                "name: mentor\n"
                "description: d\n"
                "system_prompt: You are a mentor.\n"
                "skill_refs:\n"
                "  - codereview\n"
                "  - document-generator\n"
            ),
        },
        skills=[
            _DOCGEN_SKILL,
            (
                "codereview.md",
                "---\nname: codereview\ndescription: review\n---\nCheck tests.\n",
            ),
        ],
        default_skill_refs=("document-generator",),
    )
    result = await assembler.assemble(
        [{"role": "system", "content": "{{agent.mentor}}"}, {"role": "user", "content": "hi"}],
        session_key="s",
        model_name="gpt-4",
    )
    sys_content = result.messages[0]["content"]
    # both skills present, document-generator only once despite being in both
    assert "## Skill: codereview" in sys_content
    assert sys_content.count("## Skill: document-generator") == 1
