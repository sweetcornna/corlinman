"""W1.1 — ``subagent.spawn`` schema extensions + ``subagent_type`` wiring.

Covers the new Claude-Code-style fields (``subagent_type``, ``description``,
``run_in_background``, ``model``), the ``"*"`` wildcard semantic on
:class:`AgentCard.tools_allowed`, and the registry's
``get_or_default`` fallback to ``general-purpose``.

The tests construct the registry / parent context inline (mirroring the
fixtures in ``test_subagent_tool_wrapper.py`` / ``test_subagent_runner.py``
rather than importing them — keeps this module hermetic and the test
list traceable to the plan).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from corlinman_agent.agents.card import AgentCard
from corlinman_agent.agents.registry import (
    DEFAULT_SUBAGENT_NAME,
    AgentCardRegistry,
    builtin_general_purpose,
)
from corlinman_agent.subagent import (
    AGENT_NOT_FOUND_ERROR,
    BACKGROUND_NOT_IMPLEMENTED_ERROR,
    TOOL_ALLOWLIST_ESCALATION_ERROR,
    UNKNOWN_SUBAGENT_TYPE_ERROR,
    FinishReason,
    ParentContext,
    TaskSpec,
    dispatch_subagent_spawn,
    run_child,
)
from corlinman_providers.base import ProviderChunk

# ---------------------------------------------------------------------------
# Fixtures — keep parallel to the styles in test_subagent_tool_wrapper.py
# so a maintainer reading both files sees the same shapes.
# ---------------------------------------------------------------------------


def _card(
    name: str,
    *,
    system_prompt: str = "You are a test agent.",
    tools_allowed: list[str] | None = None,
    model: str | None = None,
) -> AgentCard:
    return AgentCard(
        name=name,
        description="",
        system_prompt=system_prompt,
        tools_allowed=tools_allowed or [],
        model=model,
    )


def _registry(*cards: AgentCard) -> AgentCardRegistry:
    return AgentCardRegistry({c.name: c for c in cards})


def _parent_ctx() -> ParentContext:
    return ParentContext(
        tenant_id="tenant-a",
        parent_agent_id="main",
        parent_session_key="root",
        depth=0,
        trace_id="trace-test",
    )


class _FakeProvider:
    """Records the messages + model + tools the loop forwarded.

    Mirrors the shape used by ``test_subagent_tool_wrapper.py::_FakeProvider``
    but also captures the ``model`` kwarg so the model-override / card-
    binding tests can assert verbatim.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.tools_seen: list[Any] = []
        self.model_seen: list[str] = []

    async def chat_stream(self, **kwargs: Any) -> AsyncIterator[ProviderChunk]:  # type: ignore[override]
        self.calls += 1
        self.tools_seen.append(kwargs.get("tools"))
        # ReasoningLoop forwards ``model=start.model`` from ChatStart;
        # capturing it as-is so the tests check the runner's
        # resolution precedence verbatim.
        self.model_seen.append(kwargs.get("model", ""))
        yield ProviderChunk(kind="token", text="child output")
        yield ProviderChunk(kind="done", finish_reason="stop")


def _tool(name: str) -> dict[str, Any]:
    """Construct an OpenAI-shaped tool schema entry."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"test tool {name}",
            "parameters": {"type": "object", "properties": {}},
        },
    }


# ---------------------------------------------------------------------------
# Registry fallback — ``get_or_default`` semantics.
# ---------------------------------------------------------------------------


async def test_default_subagent_type_resolves_to_general_purpose() -> None:
    """Omitting ``subagent_type`` from the dispatch call resolves to the
    registry's ``general-purpose`` card via
    :meth:`AgentCardRegistry.get_or_default`. The child's run goes
    through as if the LLM had named the default card explicitly."""
    gp_card = _card(DEFAULT_SUBAGENT_NAME, tools_allowed=["*"])
    other = _card("researcher")
    args = json.dumps({"goal": "anything"})  # subagent_type omitted

    provider = _FakeProvider()
    content = await dispatch_subagent_spawn(
        args_json=args,
        parent_ctx=_parent_ctx(),
        agent_registry=_registry(gp_card, other),
        provider=provider,
    )
    payload = json.loads(content)
    # The child ran (provider was invoked) and the mangled child_agent_id
    # carries the resolved card name — proves get_or_default fell back.
    assert payload["finish_reason"] == "stop"
    assert provider.calls == 1
    assert DEFAULT_SUBAGENT_NAME in payload["child_agent_id"]


async def test_explicit_subagent_type_resolved() -> None:
    """An explicit ``subagent_type="researcher"`` routes to the named
    card, NOT the default. Locks the "explicit type wins" branch in
    :meth:`AgentCardRegistry.get_or_default`."""
    gp_card = _card(DEFAULT_SUBAGENT_NAME, tools_allowed=["*"])
    researcher = _card("researcher")
    args = json.dumps({"goal": "x", "subagent_type": "researcher"})

    provider = _FakeProvider()
    content = await dispatch_subagent_spawn(
        args_json=args,
        parent_ctx=_parent_ctx(),
        agent_registry=_registry(gp_card, researcher),
        provider=provider,
    )
    payload = json.loads(content)
    assert payload["finish_reason"] == "stop"
    assert "researcher" in payload["child_agent_id"]
    # The default card name must NOT appear — proves the explicit name
    # wins over the fallback.
    assert DEFAULT_SUBAGENT_NAME not in payload["child_agent_id"]


async def test_unknown_subagent_type_rejected() -> None:
    """An explicit but unregistered ``subagent_type`` rejects with the
    Claude-Code-style sentinel ``unknown_subagent_type``. The dispatcher
    must NOT silently substitute the ``general-purpose`` fallback — a
    silent substitution would mask typos."""
    gp_card = _card(DEFAULT_SUBAGENT_NAME, tools_allowed=["*"])
    args = json.dumps({"goal": "x", "subagent_type": "nonexistent"})

    provider = _FakeProvider()
    content = await dispatch_subagent_spawn(
        args_json=args,
        parent_ctx=_parent_ctx(),
        agent_registry=_registry(gp_card),
        provider=provider,
    )
    payload = json.loads(content)
    assert payload["finish_reason"] == "rejected"
    assert payload["error"].startswith(UNKNOWN_SUBAGENT_TYPE_ERROR)
    assert "nonexistent" in payload["error"]
    # Provider must not have been called — spawn rejected pre-loop.
    assert provider.calls == 0


async def test_unknown_legacy_agent_field_keeps_agent_not_found() -> None:
    """The deprecated ``agent`` field still produces ``agent_not_found``
    on miss — keeps backwards compatibility with the pre-W1.1 schema."""
    args = json.dumps({"goal": "x", "agent": "ghost"})

    provider = _FakeProvider()
    content = await dispatch_subagent_spawn(
        args_json=args,
        parent_ctx=_parent_ctx(),
        agent_registry=_registry(_card("researcher")),
        provider=provider,
    )
    payload = json.loads(content)
    assert payload["finish_reason"] == "rejected"
    assert payload["error"].startswith(AGENT_NOT_FOUND_ERROR)
    assert provider.calls == 0


async def test_default_runs_incode_fallback_when_general_purpose_unregistered() -> None:
    """v1.12.2: when the caller omits ``subagent_type`` *and* the registry
    has no ``general-purpose`` card (e.g. a fresh VPS whose ``agents/`` dir
    is empty), the dispatcher now runs the IN-CODE ``builtin_general_purpose``
    fallback instead of rejecting with ``agent_not_found``.

    This is the fix for the prod ``agent_not_found: 'general-purpose'``
    incident — a model-less default spawn must Just Work offline."""
    args = json.dumps({"goal": "x"})
    provider = _FakeProvider()
    content = await dispatch_subagent_spawn(
        args_json=args,
        parent_ctx=_parent_ctx(),
        agent_registry=_registry(_card("researcher")),  # no general-purpose
        provider=provider,
        parent_model="claude-opus-4-8",
    )
    payload = json.loads(content)
    # The child actually ran on the in-code fallback card.
    assert payload["finish_reason"] == "stop"
    assert provider.calls == 1
    # And it inherited the parent's model (the fallback card binds none).
    assert provider.model_seen == ["claude-opus-4-8"]
    # The resolved card name threads into the child's agent id.
    assert DEFAULT_SUBAGENT_NAME in payload["child_agent_id"]


# ---------------------------------------------------------------------------
# Wildcard tools semantics — ``card.tools_allowed == ["*"]``.
# ---------------------------------------------------------------------------


async def test_wildcard_tools_inherits_parent_set() -> None:
    """A card with ``tools_allowed: ["*"]`` causes the child to inherit
    the parent's full tool set verbatim (no card-side narrowing)."""
    gp_card = _card(DEFAULT_SUBAGENT_NAME, tools_allowed=["*"])
    parent_tools = [_tool("a"), _tool("b"), _tool("c")]

    provider = _FakeProvider()
    await run_child(
        _parent_ctx(),
        gp_card,
        TaskSpec(goal="anything"),  # no caller-side allowlist
        provider=provider,
        parent_tools=parent_tools,
    )

    seen = provider.tools_seen[0] or []
    seen_names = {
        (t.get("function") or {}).get("name") if isinstance(t, dict) else None
        for t in seen
    }
    assert seen_names == {"a", "b", "c"}


async def test_caller_tool_allowlist_narrows_wildcard() -> None:
    """The caller's ``TaskSpec.tool_allowlist`` further narrows a
    wildcard card. Layer 2 (caller) intersects with the layer-1 result;
    a wildcard card resolves to "parent's full set" first, then the
    caller's list restricts down."""
    gp_card = _card(DEFAULT_SUBAGENT_NAME, tools_allowed=["*"])
    parent_tools = [_tool("a"), _tool("b"), _tool("c")]

    provider = _FakeProvider()
    await run_child(
        _parent_ctx(),
        gp_card,
        TaskSpec(goal="x", tool_allowlist=["a"]),
        provider=provider,
        parent_tools=parent_tools,
    )

    seen = provider.tools_seen[0] or []
    seen_names = {
        (t.get("function") or {}).get("name") if isinstance(t, dict) else None
        for t in seen
    }
    assert seen_names == {"a"}


async def test_escalation_still_rejected_without_wildcard() -> None:
    """Non-wildcard card + caller requesting a tool the *parent* lacks
    still escalates. The card-side narrowing doesn't widen the parent's
    set, so the layer-2 escalation check fires as it did pre-W1.1."""
    card = _card("limited", tools_allowed=["a"])
    parent_tools = [_tool("a")]  # parent does not have "forbidden"

    provider = _FakeProvider()
    result = await run_child(
        _parent_ctx(),
        card,
        TaskSpec(goal="x", tool_allowlist=["forbidden"]),
        provider=provider,
        parent_tools=parent_tools,
    )

    assert result.finish_reason is FinishReason.REJECTED
    assert result.error == TOOL_ALLOWLIST_ESCALATION_ERROR
    assert provider.calls == 0


async def test_card_narrows_to_explicit_list_without_wildcard() -> None:
    """A non-wildcard card with an explicit ``tools_allowed`` list
    narrows the child's effective set to the intersection of that list
    with the parent's set. Documents the layer-1 narrowing branch."""
    card = _card("limited", tools_allowed=["a", "b"])
    parent_tools = [_tool("a"), _tool("b"), _tool("c")]

    provider = _FakeProvider()
    await run_child(
        _parent_ctx(),
        card,
        TaskSpec(goal="x"),
        provider=provider,
        parent_tools=parent_tools,
    )

    seen = provider.tools_seen[0] or []
    seen_names = {
        (t.get("function") or {}).get("name") if isinstance(t, dict) else None
        for t in seen
    }
    # ``c`` was on the parent but NOT in the card's allowlist → dropped.
    assert seen_names == {"a", "b"}


# ---------------------------------------------------------------------------
# Model override — card binding vs caller arg precedence.
# ---------------------------------------------------------------------------


async def test_card_model_overrides_parent_default() -> None:
    """When the card carries a ``model:`` binding and the caller did
    NOT pass a ``model`` arg, the runner threads the card's model into
    ChatStart so the provider router picks that model."""
    card = _card("researcher", model="claude-sonnet-4-7")
    provider = _FakeProvider()

    await run_child(
        _parent_ctx(),
        card,
        TaskSpec(goal="x"),
        provider=provider,
        parent_tools=[],
    )

    assert provider.model_seen == ["claude-sonnet-4-7"]


async def test_caller_model_overrides_card() -> None:
    """Caller's ``model_override`` always wins over the card's binding.
    This is the per-spawn knob the orchestrator uses to send one
    sibling to a cheap model and another to a flagship in the same
    fan-out call."""
    card = _card("researcher", model="claude-sonnet-4-7")
    provider = _FakeProvider()

    await run_child(
        _parent_ctx(),
        card,
        TaskSpec(goal="x"),
        provider=provider,
        parent_tools=[],
        model_override="gpt-4o",
    )

    assert provider.model_seen == ["gpt-4o"]


async def test_no_model_keeps_legacy_empty_placeholder() -> None:
    """When neither the card nor the caller specify a model, the child's
    ChatStart.model stays ``""`` — the gateway substitutes the parent's
    resolved alias as it did pre-W1.1, so this branch must remain
    byte-compatible."""
    card = _card("researcher")  # no model
    provider = _FakeProvider()

    await run_child(
        _parent_ctx(),
        card,
        TaskSpec(goal="x"),
        provider=provider,
        parent_tools=[],
    )

    assert provider.model_seen == [""]


async def test_dispatcher_threads_caller_model_to_runner() -> None:
    """End-to-end through ``dispatch_subagent_spawn``: a ``model`` field
    in the tool call's args_json reaches the provider's ``model``
    kwarg. Locks the full LLM-call → runner wiring."""
    gp_card = _card(DEFAULT_SUBAGENT_NAME, tools_allowed=["*"])
    args = json.dumps({"goal": "x", "model": "gpt-4o-mini"})

    provider = _FakeProvider()
    await dispatch_subagent_spawn(
        args_json=args,
        parent_ctx=_parent_ctx(),
        agent_registry=_registry(gp_card),
        provider=provider,
    )

    assert provider.model_seen == ["gpt-4o-mini"]


# ---------------------------------------------------------------------------
# ``run_in_background=true`` — W1.3 placeholder rejection.
# ---------------------------------------------------------------------------


async def test_run_in_background_rejected_as_not_implemented() -> None:
    """The schema accepts ``run_in_background: true`` so the LLM's
    grammar matches Claude Code's, but the backend rejects with the
    W1.3-tracked sentinel ``run_in_background_not_implemented``. Once
    W1.3 lands the background dispatch path replaces this rejection."""
    gp_card = _card(DEFAULT_SUBAGENT_NAME, tools_allowed=["*"])
    args = json.dumps({"goal": "x", "run_in_background": True})

    provider = _FakeProvider()
    content = await dispatch_subagent_spawn(
        args_json=args,
        parent_ctx=_parent_ctx(),
        agent_registry=_registry(gp_card),
        provider=provider,
    )
    payload = json.loads(content)
    assert payload["finish_reason"] == "rejected"
    assert payload["error"] == BACKGROUND_NOT_IMPLEMENTED_ERROR
    assert provider.calls == 0


async def test_run_in_background_false_is_no_op() -> None:
    """Explicitly setting ``run_in_background: false`` runs the child
    synchronously — the default code path."""
    gp_card = _card(DEFAULT_SUBAGENT_NAME, tools_allowed=["*"])
    args = json.dumps({"goal": "x", "run_in_background": False})

    provider = _FakeProvider()
    content = await dispatch_subagent_spawn(
        args_json=args,
        parent_ctx=_parent_ctx(),
        agent_registry=_registry(gp_card),
        provider=provider,
    )
    payload = json.loads(content)
    assert payload["finish_reason"] == "stop"
    assert provider.calls == 1


# ---------------------------------------------------------------------------
# Schema shape — quick lock on the new properties.
# ---------------------------------------------------------------------------


def test_schema_carries_new_w1_1_properties() -> None:
    """The W1.1 fields are present in the OpenAI descriptor so the LLM
    knows it may emit them. Acceptance test for the new contract."""
    from corlinman_agent.subagent import subagent_spawn_tool_schema

    schema = subagent_spawn_tool_schema()
    props = schema["function"]["parameters"]["properties"]

    for key in ("subagent_type", "description", "run_in_background", "model"):
        assert key in props, f"W1.1 field {key!r} missing from schema"

    # ``goal`` is the only required field now — the four new fields and
    # the legacy ``agent`` are all optional.
    assert schema["function"]["parameters"]["required"] == ["goal"]


# ---------------------------------------------------------------------------
# Registry-level test — ``get_or_default`` unit coverage.
# ---------------------------------------------------------------------------


def test_get_or_default_returns_named_when_present() -> None:
    reg = _registry(_card("researcher"), _card(DEFAULT_SUBAGENT_NAME))
    card = reg.get_or_default("researcher")
    assert card is not None
    assert card.name == "researcher"


def test_get_or_default_falls_back_on_empty_name() -> None:
    reg = _registry(_card("researcher"), _card(DEFAULT_SUBAGENT_NAME))
    assert reg.get_or_default(None) is not None
    assert reg.get_or_default(None).name == DEFAULT_SUBAGENT_NAME  # type: ignore[union-attr]
    assert reg.get_or_default("").name == DEFAULT_SUBAGENT_NAME  # type: ignore[union-attr]


def test_get_or_default_returns_none_for_unknown_named() -> None:
    """Explicit names that miss must NOT silently substitute the default
    — caller (the dispatcher) needs to surface ``unknown_subagent_type``
    rather than running the wrong card."""
    reg = _registry(_card("researcher"), _card(DEFAULT_SUBAGENT_NAME))
    assert reg.get_or_default("nope") is None


def test_get_or_default_returns_none_when_default_absent() -> None:
    """Bare registry without ``general-purpose`` returns ``None`` on the
    fallback path — dispatcher folds this into an ``agent_not_found``
    envelope."""
    reg = _registry(_card("researcher"))
    assert reg.get_or_default(None) is None
    assert reg.get_or_default("") is None


# ---------------------------------------------------------------------------
# v1.12.2 — parent-model inheritance + in-code general-purpose fallback.
# ---------------------------------------------------------------------------


async def test_parent_model_inherited_when_card_and_override_absent() -> None:
    """v1.12.2: when neither ``model_override`` nor ``agent_card.model``
    is set, the runner inherits ``parent_model`` — the fix for the
    ``model is required`` 400 on model-less (esp. inline) spawns."""
    card = _card("researcher")  # no model binding
    provider = _FakeProvider()

    await run_child(
        _parent_ctx(),
        card,
        TaskSpec(goal="x"),
        provider=provider,
        parent_tools=[],
        parent_model="claude-opus-4-8",
    )

    assert provider.model_seen == ["claude-opus-4-8"]


async def test_card_model_wins_over_parent_model() -> None:
    """The card's own binding takes precedence over the inherited
    parent model (the card author chose that model deliberately)."""
    card = _card("researcher", model="claude-sonnet-4-7")
    provider = _FakeProvider()

    await run_child(
        _parent_ctx(),
        card,
        TaskSpec(goal="x"),
        provider=provider,
        parent_tools=[],
        parent_model="claude-opus-4-8",
    )

    assert provider.model_seen == ["claude-sonnet-4-7"]


async def test_override_wins_over_parent_model() -> None:
    """An explicit ``model_override`` beats both the card binding and
    the inherited parent model — top of the precedence ladder."""
    card = _card("researcher", model="claude-sonnet-4-7")
    provider = _FakeProvider()

    await run_child(
        _parent_ctx(),
        card,
        TaskSpec(goal="x"),
        provider=provider,
        parent_tools=[],
        model_override="gpt-4o",
        parent_model="claude-opus-4-8",
    )

    assert provider.model_seen == ["gpt-4o"]


async def test_no_model_anywhere_keeps_empty_placeholder() -> None:
    """With no override, no card binding, AND no parent_model, the
    child's ChatStart.model stays ``""`` — byte-compat with callers
    (tests / pre-v1.12.2 paths) that thread none of the three."""
    card = _card("researcher")
    provider = _FakeProvider()

    await run_child(
        _parent_ctx(),
        card,
        TaskSpec(goal="x"),
        provider=provider,
        parent_tools=[],
    )

    assert provider.model_seen == [""]


async def test_dispatcher_threads_parent_model_to_runner() -> None:
    """End-to-end: ``dispatch_subagent_spawn(parent_model=...)`` with no
    ``model`` in the args_json and a card with no binding reaches the
    provider as the parent's model. Locks the servicer→dispatcher→runner
    wiring that fixes the prod ``model is required`` incident."""
    gp_card = _card(DEFAULT_SUBAGENT_NAME, tools_allowed=["*"])  # no model
    args = json.dumps({"goal": "x"})  # no model field

    provider = _FakeProvider()
    await dispatch_subagent_spawn(
        args_json=args,
        parent_ctx=_parent_ctx(),
        agent_registry=_registry(gp_card),
        provider=provider,
        parent_model="claude-opus-4-8",
    )

    assert provider.model_seen == ["claude-opus-4-8"]


async def test_dispatcher_arg_model_wins_over_parent_model() -> None:
    """A ``model`` in the args_json still beats the inherited
    parent_model — the per-spawn knob keeps its precedence."""
    gp_card = _card(DEFAULT_SUBAGENT_NAME, tools_allowed=["*"])
    args = json.dumps({"goal": "x", "model": "gpt-4o-mini"})

    provider = _FakeProvider()
    await dispatch_subagent_spawn(
        args_json=args,
        parent_ctx=_parent_ctx(),
        agent_registry=_registry(gp_card),
        provider=provider,
        parent_model="claude-opus-4-8",
    )

    assert provider.model_seen == ["gpt-4o-mini"]


def test_get_or_builtin_default_falls_back_without_file() -> None:
    """v1.12.2: an EMPTY registry (fresh VPS, no bundled cards loaded)
    still resolves the default via the in-code ``builtin_general_purpose``
    card — this is the fix for ``agent_not_found: 'general-purpose'``."""
    reg = _registry()  # no cards at all
    card = reg.get_or_builtin_default(None)
    assert card is not None
    assert card.name == DEFAULT_SUBAGENT_NAME
    assert card.source_path is None  # in-code, never on disk
    assert "*" in card.tools_allowed  # inherits the parent's tools


def test_get_or_builtin_default_explicit_general_purpose() -> None:
    """Explicitly naming ``general-purpose`` on an empty registry also
    resolves the in-code fallback (not just the omitted-name path)."""
    reg = _registry()
    card = reg.get_or_builtin_default(DEFAULT_SUBAGENT_NAME)
    assert card is not None
    assert card.name == DEFAULT_SUBAGENT_NAME


def test_get_or_builtin_default_prefers_loaded_card() -> None:
    """When a bundled ``general-purpose`` IS loaded, that card wins over
    the in-code fallback (deployment can customise the default)."""
    loaded = _card(DEFAULT_SUBAGENT_NAME, system_prompt="CUSTOM default")
    reg = _registry(loaded)
    card = reg.get_or_builtin_default(None)
    assert card is not None
    assert card.system_prompt == "CUSTOM default"


def test_get_or_builtin_default_unknown_named_still_none() -> None:
    """An explicit OTHER unknown name still returns ``None`` so the
    dispatcher surfaces ``unknown_subagent_type`` (typo protection is
    preserved — only the *default* gets the offline fallback)."""
    reg = _registry()
    assert reg.get_or_builtin_default("nope") is None


def test_builtin_general_purpose_card_shape() -> None:
    """The in-code card is self-consistent: ephemeral (no source_path),
    model-less (inherits parent), wildcard tools, non-empty prompt."""
    card = builtin_general_purpose()
    assert card.name == DEFAULT_SUBAGENT_NAME
    assert card.source_path is None
    assert card.model is None
    assert card.tools_allowed == ["*"]
    assert card.system_prompt.strip()
