"""gap-fill wire-A — progressive-disclosure skill catalog.

Covers gap ``skills-no-progressive-disclosure`` (context-assembler half):
instead of dumping every model-invokable skill body into the prompt every
turn, the assembler injects EXPLICIT ``skill_refs`` bodies + a compact
catalog (name + when_to_use) of the rest so the model can pull a body on
demand via the servicer's ``Skill`` tool.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from corlinman_agent.agents import AgentCardRegistry
from corlinman_agent.context_assembler import ContextAssembler
from corlinman_agent.hooks import RecordingHookEmitter
from corlinman_agent.placeholder_client import RenderResult
from corlinman_agent.skills import SkillRegistry
from corlinman_agent.variables import VariableCascade


class _NoopPlaceholder:
    async def render(
        self,
        *,
        template: str,
        session_key: str,
        model_name: str = "",
        metadata: Any = None,
        max_depth: int = 0,
    ) -> RenderResult:
        return RenderResult(rendered=template, unresolved_keys=[])


def _cascade(tmp_path: Path) -> VariableCascade:
    for tier in ("tar", "var", "sar", "fixed"):
        (tmp_path / tier).mkdir(parents=True, exist_ok=True)
    return VariableCascade(
        tmp_path / "tar",
        tmp_path / "var",
        tmp_path / "sar",
        tmp_path / "fixed",
        hot_reload=False,
    )


def _assembler(
    tmp_path: Path,
    skills: dict[str, str],
    *,
    progressive: bool = True,
    default_refs: tuple[str, ...] = (),
) -> ContextAssembler:
    skills_dir = tmp_path / "skills"
    agents_dir = tmp_path / "agents"
    skills_dir.mkdir(parents=True, exist_ok=True)
    agents_dir.mkdir(parents=True, exist_ok=True)
    for filename, body in skills.items():
        (skills_dir / filename).write_text(body, encoding="utf-8")
    return ContextAssembler(
        agents=AgentCardRegistry.load_from_dir(agents_dir),
        variables=_cascade(tmp_path),
        skills=SkillRegistry.load_from_dir(skills_dir),
        placeholder_client=_NoopPlaceholder(),  # type: ignore[arg-type]
        hook_emitter=RecordingHookEmitter(),
        config_lookup=lambda _k: None,
        progressive_skill_disclosure=progressive,
    )


_PDF_SKILL = (
    "---\nname: pdf-maker\ndescription: Make a PDF\n"
    "whenToUse: When the user wants a PDF document\n---\n"
    "Run the pdf pipeline.\n"
)
_HIDDEN_SKILL = (
    "---\nname: quiet\ndescription: noisy\n"
    "disable-model-invocation: true\n---\nNever auto-select me.\n"
)


@pytest.mark.asyncio
async def test_catalog_lists_unreferenced_skill(tmp_path: Path) -> None:
    assembler = _assembler(tmp_path, {"pdf.md": _PDF_SKILL})
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "hi"},
    ]
    result = await assembler.assemble(
        messages, session_key="s1", model_name="gpt-4"
    )
    sys_content = result.messages[0]["content"]
    # Catalog row present; full body NOT dumped (progressive disclosure).
    assert "## Available skills" in sys_content
    assert "pdf-maker" in sys_content
    assert "When the user wants a PDF document" in sys_content
    assert "Run the pdf pipeline." not in sys_content


@pytest.mark.asyncio
async def test_explicit_ref_gets_body_not_catalog_row(tmp_path: Path) -> None:
    assembler = _assembler(
        tmp_path, {"pdf.md": _PDF_SKILL}, default_refs=("pdf-maker",)
    )
    assembler._default_skill_refs = ["pdf-maker"]  # explicit operator ref
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "hi"},
    ]
    result = await assembler.assemble(
        messages, session_key="s1", model_name="gpt-4"
    )
    sys_content = result.messages[0]["content"]
    # Explicit ref -> full body injected, and excluded from the catalog.
    assert "## Skill: pdf-maker" in sys_content
    assert "Run the pdf pipeline." in sys_content


@pytest.mark.asyncio
async def test_disabled_skill_not_in_catalog(tmp_path: Path) -> None:
    assembler = _assembler(
        tmp_path, {"pdf.md": _PDF_SKILL, "quiet.md": _HIDDEN_SKILL}
    )
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "hi"},
    ]
    result = await assembler.assemble(
        messages, session_key="s1", model_name="gpt-4"
    )
    sys_content = result.messages[0]["content"]
    assert "pdf-maker" in sys_content
    assert "quiet" not in sys_content


@pytest.mark.asyncio
async def test_progressive_disabled_keeps_legacy_no_catalog(tmp_path: Path) -> None:
    assembler = _assembler(
        tmp_path, {"pdf.md": _PDF_SKILL}, progressive=False
    )
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "hi"},
    ]
    result = await assembler.assemble(
        messages, session_key="s1", model_name="gpt-4"
    )
    sys_content = result.messages[0]["content"]
    assert "## Available skills" not in sys_content
