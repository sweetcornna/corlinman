"""W-D1 — per-agent model binding parses from ``agents/<name>.yaml``.

Coverage:
  (a) cards with neither ``model:`` nor ``provider:`` keep parsing
      (back-compat: pre-W-D1 yamls must not regress);
  (b) ``model:`` alone parses (provider stays ``None``);
  (c) ``provider:`` alone parses (model stays ``None``);
  (d) both fields together parse;
  (e) a non-string ``model:`` (e.g. ``model: 123``) is rejected with
      :class:`AgentCardLoadError` so a stray int can't silently become
      an upstream model id.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from corlinman_agent.agents import AgentCardRegistry
from corlinman_agent.agents.registry import AgentCardLoadError, _load_card


def _write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


_BASE = """\
name: {name}
description: per-agent model binding fixture
system_prompt: |
  fixture body.
"""


def test_card_without_model_or_provider_parses(tmp_path: Path) -> None:
    """Pre-W-D1 yamls (no model/provider) must keep working."""
    _write(tmp_path / "plain.yaml", _BASE.format(name="plain"))
    reg = AgentCardRegistry.load_from_dir(tmp_path)
    card = reg.get("plain")
    assert card is not None
    assert card.model is None
    assert card.provider is None


def test_card_with_just_model_parses(tmp_path: Path) -> None:
    body = _BASE.format(name="m_only") + "model: claude-sonnet-4-6\n"
    _write(tmp_path / "m_only.yaml", body)
    reg = AgentCardRegistry.load_from_dir(tmp_path)
    card = reg.get("m_only")
    assert card is not None
    assert card.model == "claude-sonnet-4-6"
    assert card.provider is None


def test_card_with_just_provider_parses(tmp_path: Path) -> None:
    body = _BASE.format(name="p_only") + "provider: anthropic\n"
    _write(tmp_path / "p_only.yaml", body)
    reg = AgentCardRegistry.load_from_dir(tmp_path)
    card = reg.get("p_only")
    assert card is not None
    assert card.model is None
    assert card.provider == "anthropic"


def test_card_with_model_and_provider_parses(tmp_path: Path) -> None:
    body = (
        _BASE.format(name="both")
        + "model: claude-sonnet-4-6\nprovider: anthropic\n"
    )
    _write(tmp_path / "both.yaml", body)
    reg = AgentCardRegistry.load_from_dir(tmp_path)
    card = reg.get("both")
    assert card is not None
    assert card.model == "claude-sonnet-4-6"
    assert card.provider == "anthropic"


def test_card_with_action_trace_disabled_parses(tmp_path: Path) -> None:
    body = _BASE.format(name="quiet") + "show_action_trace: false\n"
    _write(tmp_path / "quiet.yaml", body)
    reg = AgentCardRegistry.load_from_dir(tmp_path)
    card = reg.get("quiet")
    assert card is not None
    assert card.show_action_trace is False


def test_card_defaults_to_showing_action_trace(tmp_path: Path) -> None:
    _write(tmp_path / "visible.yaml", _BASE.format(name="visible"))
    reg = AgentCardRegistry.load_from_dir(tmp_path)
    card = reg.get("visible")
    assert card is not None
    assert card.show_action_trace is True


def test_card_with_non_string_model_raises(tmp_path: Path) -> None:
    """Stray int / bool in ``model:`` must be rejected at load time."""
    body = _BASE.format(name="bad_model") + "model: 123\n"
    path = _write(tmp_path / "bad_model.yaml", body)
    with pytest.raises(AgentCardLoadError, match="model must be a string"):
        _load_card(path)


def test_card_with_non_string_provider_raises(tmp_path: Path) -> None:
    """Same guard on the provider field — must be a string slot name."""
    body = _BASE.format(name="bad_prov") + "provider: 42\n"
    path = _write(tmp_path / "bad_prov.yaml", body)
    with pytest.raises(AgentCardLoadError, match="provider must be a string"):
        _load_card(path)


def test_card_with_non_bool_action_trace_raises(tmp_path: Path) -> None:
    body = _BASE.format(name="bad_trace") + "show_action_trace: nope\n"
    path = _write(tmp_path / "bad_trace.yaml", body)
    with pytest.raises(AgentCardLoadError, match="show_action_trace must be a boolean"):
        _load_card(path)
