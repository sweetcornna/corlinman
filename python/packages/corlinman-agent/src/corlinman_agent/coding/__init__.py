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
from corlinman_agent.coding._filestate import FileState
from corlinman_agent.coding._snapshot import (
    ensure_repo,
    list_snapshots,
    revert_last,
    snapshot,
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
from corlinman_agent.coding.patch import (
    APPLY_PATCH_TOOL,
    apply_patch_tool_schema,
    dispatch_apply_patch,
)
from corlinman_agent.coding.revert import (
    REVERT_CHANGES_TOOL,
    dispatch_revert_changes,
    revert_changes_tool_schema,
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
from corlinman_agent.coding.todo import (
    TODO_WRITE_TOOL,
    TodoItem,
    TodoStore,
    dispatch_todo_write,
    render_todo_block,
    todo_write_tool_schema,
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
        APPLY_PATCH_TOOL,
        TODO_WRITE_TOOL,
        REVERT_CHANGES_TOOL,
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
        apply_patch_tool_schema(),
        todo_write_tool_schema(),
        revert_changes_tool_schema(),
    ]


__all__ = [
    "APPLY_PATCH_TOOL",
    "CODING_TOOLS",
    "EDIT_FILE_TOOL",
    "LIST_FILES_TOOL",
    "READ_FILE_TOOL",
    "REVERT_CHANGES_TOOL",
    "RUN_SHELL_TOOL",
    "SEARCH_FILES_TOOL",
    "TODO_WRITE_TOOL",
    "WRITE_FILE_TOOL",
    "FileState",
    "TodoItem",
    "TodoStore",
    "apply_patch_tool_schema",
    "coding_tool_schemas",
    "dispatch_apply_patch",
    "dispatch_edit_file",
    "dispatch_list_files",
    "dispatch_read_file",
    "dispatch_revert_changes",
    "dispatch_run_shell",
    "dispatch_search_files",
    "dispatch_todo_write",
    "dispatch_write_file",
    "edit_file_tool_schema",
    "ensure_repo",
    "list_files_tool_schema",
    "list_snapshots",
    "read_file_tool_schema",
    "render_todo_block",
    "resolve_in_workspace",
    "resolve_workspace",
    "revert_changes_tool_schema",
    "revert_last",
    "run_shell_tool_schema",
    "search_files_tool_schema",
    "snapshot",
    "todo_write_tool_schema",
    "write_file_tool_schema",
]
