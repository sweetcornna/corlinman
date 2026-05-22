"""Builtin coding tools — file ops, search, and shell execution.

This package gives the agent the "operate a codebase" surface that
hermes-agent / opencode / Claude Code expose: read / write / edit files,
search the tree, and run shell commands. All file operations are
confined to an agent workspace directory (see :mod:`._common`).

Public surface, per tool: a ``*_tool_schema()`` OpenAI descriptor and a
``dispatch_*`` callable that returns a JSON envelope string. The agent
servicer wires these into ``BUILTIN_TOOLS`` exactly like the web tools.
"""

from __future__ import annotations

from corlinman_agent.coding._common import (
    resolve_in_workspace,
    resolve_workspace,
)
from corlinman_agent.coding.files import (
    EDIT_FILE_TOOL,
    LIST_FILES_TOOL,
    READ_FILE_TOOL,
    WRITE_FILE_TOOL,
    dispatch_edit_file,
    dispatch_list_files,
    dispatch_read_file,
    dispatch_write_file,
    edit_file_tool_schema,
    list_files_tool_schema,
    read_file_tool_schema,
    write_file_tool_schema,
)
from corlinman_agent.coding.search import (
    SEARCH_FILES_TOOL,
    dispatch_search_files,
    search_files_tool_schema,
)
from corlinman_agent.coding.shell import (
    RUN_SHELL_TOOL,
    dispatch_run_shell,
    run_shell_tool_schema,
)

#: Every coding tool name — the agent servicer folds this into
#: ``BUILTIN_TOOLS`` and advertises the schemas to the model.
CODING_TOOLS: frozenset[str] = frozenset(
    {
        READ_FILE_TOOL,
        WRITE_FILE_TOOL,
        EDIT_FILE_TOOL,
        LIST_FILES_TOOL,
        SEARCH_FILES_TOOL,
        RUN_SHELL_TOOL,
    }
)


def coding_tool_schemas() -> list[dict]:
    """OpenAI tool descriptors for every coding tool."""
    return [
        read_file_tool_schema(),
        write_file_tool_schema(),
        edit_file_tool_schema(),
        list_files_tool_schema(),
        search_files_tool_schema(),
        run_shell_tool_schema(),
    ]


__all__ = [
    "CODING_TOOLS",
    "EDIT_FILE_TOOL",
    "LIST_FILES_TOOL",
    "READ_FILE_TOOL",
    "RUN_SHELL_TOOL",
    "SEARCH_FILES_TOOL",
    "WRITE_FILE_TOOL",
    "coding_tool_schemas",
    "dispatch_edit_file",
    "dispatch_list_files",
    "dispatch_read_file",
    "dispatch_run_shell",
    "dispatch_search_files",
    "dispatch_write_file",
    "edit_file_tool_schema",
    "list_files_tool_schema",
    "read_file_tool_schema",
    "resolve_in_workspace",
    "resolve_workspace",
    "run_shell_tool_schema",
    "search_files_tool_schema",
    "write_file_tool_schema",
]
