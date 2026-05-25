"""Agent-card subsystem — ``{{角色}}`` expansion.

This subpackage is deliberately self-contained: it depends only on
PyYAML and the stdlib, and knows nothing about the Rust placeholder
engine or the gRPC bridge. It runs as a pre-step before the main
placeholder render pass — callers expand ``{{agent.*}}`` tokens here,
then hand the resulting messages to the placeholder bridge to resolve
everything else (``{{var.*}}``, ``{{skill.*}}``, dynamic tokens, ...).
"""

from __future__ import annotations

from corlinman_agent.agents.card import AgentCard, AgentSource
from corlinman_agent.agents.expander import (
    AgentCircularReferenceError,
    AgentExpander,
    ExpansionResult,
)
from corlinman_agent.agents.markdown import parse_markdown_card
from corlinman_agent.agents.registry import AgentCardLoadError, AgentCardRegistry

__all__ = [
    "AgentCard",
    "AgentCardLoadError",
    "AgentCardRegistry",
    "AgentCircularReferenceError",
    "AgentExpander",
    "AgentSource",
    "ExpansionResult",
    "parse_markdown_card",
]
