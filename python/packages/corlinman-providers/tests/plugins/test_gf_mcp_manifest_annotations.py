"""gap-fill lane-mcp: plugin-manifest Tool carries MCP ToolAnnotations /
outputSchema / title (parse-and-keep instead of drop).

Uniquely named ``test_gf_mcp_*`` so it never collides with sibling
gap-fill lanes touching the manifest schema.
"""

from __future__ import annotations

import pytest
from corlinman_providers.plugins.manifest import Tool, ToolAnnotations
from pydantic import ValidationError


def test_tool_parses_and_keeps_annotations_camelcase():
    t = Tool.model_validate(
        {
            "name": "search",
            "description": "find stuff",
            "parameters": {"type": "object"},
            "annotations": {
                "readOnlyHint": True,
                "destructiveHint": False,
                "idempotentHint": True,
                "openWorldHint": False,
                "title": "Search",
            },
        }
    )
    assert isinstance(t.annotations, ToolAnnotations)
    assert t.annotations.read_only_hint is True
    assert t.annotations.destructive_hint is False
    assert t.annotations.idempotent_hint is True
    assert t.annotations.open_world_hint is False
    assert t.annotations.title == "Search"


def test_tool_keeps_output_schema_and_title():
    t = Tool.model_validate(
        {
            "name": "fetch",
            "outputSchema": {"type": "object", "properties": {"id": {"type": "string"}}},
            "title": "Fetch Record",
        }
    )
    assert t.output_schema == {
        "type": "object",
        "properties": {"id": {"type": "string"}},
    }
    assert t.title == "Fetch Record"


def test_tool_without_new_fields_still_parses():
    t = Tool.model_validate({"name": "noop"})
    assert t.annotations is None
    assert t.output_schema is None
    assert t.title is None
    # Existing defaults unchanged.
    assert t.parameters == {"type": "object"}
    assert t.description == ""


def test_tool_annotation_camelcase_round_trip_by_alias():
    t = Tool.model_validate(
        {
            "name": "write",
            "annotations": {"destructiveHint": True, "readOnlyHint": False},
            "outputSchema": {"type": "string"},
            "title": "Writer",
        }
    )
    dumped = t.model_dump(by_alias=True, exclude_none=True)
    assert dumped["annotations"] == {"destructiveHint": True, "readOnlyHint": False}
    assert dumped["outputSchema"] == {"type": "string"}
    assert dumped["title"] == "Writer"


def test_tool_annotations_snake_case_population_supported():
    # populate_by_name lets internal Python callers use the field names.
    ann = ToolAnnotations(read_only_hint=True, destructive_hint=False)
    assert ann.model_dump(by_alias=True, exclude_none=True) == {
        "readOnlyHint": True,
        "destructiveHint": False,
    }


def test_unknown_annotation_field_still_rejected():
    # extra="forbid" on ToolAnnotations means typos are caught, not
    # silently swallowed.
    with pytest.raises(ValidationError):
        ToolAnnotations.model_validate({"readonlyhint": True})
