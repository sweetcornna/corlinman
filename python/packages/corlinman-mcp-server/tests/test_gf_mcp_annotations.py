"""gap-fill lane-mcp: ToolAnnotations + outputSchema serialization
round-trip, and the ``*/list_changed`` server->client notification path.

Tests live alongside the package suite but are uniquely named
``test_gf_mcp_*`` so they never collide with sibling gap-fill lanes.
"""

from __future__ import annotations

import asyncio

import pytest
from corlinman_mcp_server.adapters import SessionContext
from corlinman_mcp_server.dispatch import AdapterDispatcher, ServerInfo
from corlinman_mcp_server.tools import (
    ToolsAdapter,
    _tool_annotations,
    _tool_output_schema,
)
from corlinman_mcp_server.types import (
    PROMPTS_LIST_CHANGED_NOTIFICATION,
    RESOURCES_LIST_CHANGED_NOTIFICATION,
    TOOLS_LIST_CHANGED_NOTIFICATION,
    ToolAnnotations,
    ToolDescriptor,
    ToolsListResult,
    list_changed_notification,
)

from .conftest import StubPluginRegistry, StubPluginRuntime, make_plugin_entry


# ---------------------------------------------------------------------
# ToolAnnotations / ToolDescriptor serialization round-trip
# ---------------------------------------------------------------------


def test_annotations_serialize_camelcase_and_drop_unset():
    ann = ToolAnnotations(readOnlyHint=True, destructiveHint=False, title="Search")
    dumped = ann.model_dump()
    assert dumped == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "title": "Search",
    }
    # Unset hints (idempotent/openWorld) are elided, not emitted as null.
    assert "idempotentHint" not in dumped
    assert "openWorldHint" not in dumped


def test_empty_annotations_is_empty_and_serialize_to_blank_object():
    ann = ToolAnnotations()
    assert ann.is_empty()
    assert ann.model_dump() == {}


def test_annotations_accept_snake_case_alias_population():
    # populate_by_name=True lets internal callers use the python names.
    ann = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    assert ann.model_dump() == {"readOnlyHint": True, "openWorldHint": False}


def test_tool_descriptor_surfaces_annotations_output_schema_and_title():
    td = ToolDescriptor(
        name="kb:search",
        description="find stuff",
        inputSchema={"type": "object"},
        outputSchema={"type": "object", "properties": {}},
        annotations=ToolAnnotations(readOnlyHint=True),
        title="Search KB",
    )
    out = td.model_dump()
    assert out["name"] == "kb:search"
    assert out["title"] == "Search KB"
    assert out["inputSchema"] == {"type": "object"}
    assert out["outputSchema"] == {"type": "object", "properties": {}}
    assert out["annotations"] == {"readOnlyHint": True}


def test_tool_descriptor_elides_empty_annotation_block():
    td = ToolDescriptor(
        name="kb:get",
        inputSchema={"type": "object"},
        annotations=ToolAnnotations(),
    )
    out = td.model_dump()
    # An annotations object with no hints set MUST NOT appear on the wire.
    assert "annotations" not in out
    assert "outputSchema" not in out
    assert "title" not in out


def test_tool_descriptor_round_trips_through_list_result():
    td = ToolDescriptor(
        name="kb:search",
        description="d",
        inputSchema={"type": "object"},
        annotations=ToolAnnotations(destructiveHint=True),
    )
    res = ToolsListResult(tools=[td], nextCursor=None)
    dumped = res.model_dump()
    assert dumped["tools"][0]["annotations"] == {"destructiveHint": True}
    assert "nextCursor" not in dumped


# ---------------------------------------------------------------------
# Annotation extraction from heterogeneous plugin-tool shapes
# ---------------------------------------------------------------------


def test_extract_annotations_degrades_on_bridge_without_attrs():
    # The current bridge PluginTool has no annotations attribute; the
    # adapter must degrade to None rather than raise.
    entry = make_plugin_entry("kb", [("search", "find stuff")])
    tool = entry.manifest.capabilities.tools[0]
    assert _tool_annotations(tool) is None
    assert _tool_output_schema(tool) is None


def test_extract_annotations_from_mapping():
    class RichTool:
        annotations = {"readOnlyHint": False, "destructiveHint": True}
        output_schema = {"type": "object"}

    ann = _tool_annotations(RichTool())
    assert ann is not None
    assert ann.model_dump() == {"readOnlyHint": False, "destructiveHint": True}
    assert _tool_output_schema(RichTool()) == {"type": "object"}


def test_extract_annotations_passthrough_toolannotations_obj():
    class RichTool:
        annotations = ToolAnnotations(idempotentHint=True)

    ann = _tool_annotations(RichTool())
    assert ann is not None
    assert ann.model_dump() == {"idempotentHint": True}


def test_extract_empty_annotation_mapping_returns_none():
    class RichTool:
        annotations: dict = {}

    assert _tool_annotations(RichTool()) is None


@pytest.mark.asyncio
async def test_list_tools_emits_no_annotations_for_plain_bridge():
    reg = StubPluginRegistry()
    reg.add(make_plugin_entry("kb", [("search", "find stuff")]))
    adapter = ToolsAdapter.with_runtime(
        reg, StubPluginRuntime(outcome=None)
    )
    res = adapter.list_tools(SessionContext.permissive())
    dumped = res.model_dump()
    assert dumped["tools"][0]["name"] == "kb:search"
    # Plain bridge tools carry no annotations → key absent.
    assert "annotations" not in dumped["tools"][0]


# ---------------------------------------------------------------------
# */list_changed notification path
# ---------------------------------------------------------------------


def test_list_changed_notification_frame_is_idless():
    frame = list_changed_notification(TOOLS_LIST_CHANGED_NOTIFICATION)
    assert frame == {
        "jsonrpc": "2.0",
        "method": "notifications/tools/list_changed",
    }
    # JSON-RPC notifications carry no id.
    assert "id" not in frame


class _FakeToolsAdapter:
    def capability_name(self) -> str:
        return "tools"

    async def handle(self, method, params, ctx):  # noqa: ARG002
        return {}


def test_register_advertises_list_changed_capability():
    d = AdapterDispatcher(ServerInfo(name="t", version="1"))
    d.register(_FakeToolsAdapter())
    assert d.capabilities.tools is not None
    assert d.capabilities.tools.model_dump() == {"listChanged": True}


@pytest.mark.asyncio
async def test_notify_list_changed_fans_out_to_registered_sinks():
    d = AdapterDispatcher(ServerInfo(name="t", version="1"))
    d.register(_FakeToolsAdapter())

    received_a: list[dict] = []
    received_b: list[dict] = []

    async def sink_a(frame):
        received_a.append(frame)

    async def sink_b(frame):
        received_b.append(frame)

    h_a = await d.register_sink(sink_a)
    await d.register_sink(sink_b)

    delivered = await d.notify_list_changed("tools")
    assert delivered == 2
    assert received_a == [list_changed_notification(TOOLS_LIST_CHANGED_NOTIFICATION)]
    assert received_b == received_a

    # Simulate a connection teardown — the dropped sink stops receiving.
    await d.unregister_sink(h_a)
    received_a.clear()
    received_b.clear()
    delivered2 = await d.notify_list_changed("tools")
    assert delivered2 == 1
    assert received_a == []
    assert received_b == [list_changed_notification(TOOLS_LIST_CHANGED_NOTIFICATION)]


@pytest.mark.asyncio
async def test_notify_unknown_capability_is_noop():
    d = AdapterDispatcher(ServerInfo(name="t", version="1"))

    async def sink(frame):  # pragma: no cover — must not fire
        raise AssertionError("unknown capability should not deliver")

    await d.register_sink(sink)
    assert await d.notify_list_changed("bogus") == 0


@pytest.mark.asyncio
async def test_notify_resources_and_prompts_use_correct_methods():
    d = AdapterDispatcher(ServerInfo(name="t", version="1"))
    seen: list[dict] = []

    async def sink(frame):
        seen.append(frame)

    await d.register_sink(sink)
    await d.notify_list_changed("resources")
    await d.notify_list_changed("prompts")
    methods = [f["method"] for f in seen]
    assert methods == [
        RESOURCES_LIST_CHANGED_NOTIFICATION,
        PROMPTS_LIST_CHANGED_NOTIFICATION,
    ]


@pytest.mark.asyncio
async def test_one_dead_sink_does_not_abort_fanout():
    d = AdapterDispatcher(ServerInfo(name="t", version="1"))
    good: list[dict] = []

    async def dead_sink(frame):  # noqa: ARG001
        raise RuntimeError("connection closed")

    async def good_sink(frame):
        good.append(frame)

    await d.register_sink(dead_sink)
    await d.register_sink(good_sink)
    # The dead sink raises but the good one still receives.
    delivered = await d.notify_list_changed("tools")
    assert delivered == 1
    assert good == [list_changed_notification(TOOLS_LIST_CHANGED_NOTIFICATION)]
