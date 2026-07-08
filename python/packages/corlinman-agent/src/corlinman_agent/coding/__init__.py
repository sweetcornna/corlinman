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
from corlinman_agent.coding.environment import (
    ENV_SANDBOX_BACKEND,
    DockerEnvironment,
    Environment,
    LocalEnvironment,
    SpawnedProcess,
    get_environment,
)
from corlinman_agent.coding.files import (
    EDIT_FILE_TOOL,
    LIST_FILES_TOOL,
    NOTEBOOK_EDIT_TOOL,
    READ_FILE_TOOL,
    WRITE_FILE_TOOL,
    dispatch_edit_file,
    dispatch_list_files,
    dispatch_notebook_edit,
    dispatch_read_file,
    dispatch_write_file,
    edit_file_tool_schema,
    list_files_tool_schema,
    notebook_edit_tool_schema,
    read_file_tool_schema,
    write_file_tool_schema,
)
from corlinman_agent.coding.patch import (
    APPLY_PATCH_TOOL,
    apply_patch_tool_schema,
    dispatch_apply_patch,
)
from corlinman_agent.coding.repl import (
    EXECUTE_CODE_TOOL,
    dispatch_execute_code,
    execute_code_tool_schema,
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
from corlinman_agent.coding.shell_tasks import (
    SHELL_TASK_KILL_TOOL,
    SHELL_TASK_OUTPUT_TOOL,
    dispatch_shell_task_kill,
    dispatch_shell_task_output,
    shell_task_kill_tool_schema,
    shell_task_output_tool_schema,
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
        NOTEBOOK_EDIT_TOOL,
        LIST_FILES_TOOL,
        SEARCH_FILES_TOOL,
        RUN_SHELL_TOOL,
        SHELL_TASK_OUTPUT_TOOL,
        SHELL_TASK_KILL_TOOL,
        APPLY_PATCH_TOOL,
        TODO_WRITE_TOOL,
        REVERT_CHANGES_TOOL,
    }
)

#: ``execute_code`` is opt-in (disabled by default — see
#: :func:`dispatch_execute_code`). It is intentionally excluded from the
#: always-advertised :data:`CODING_TOOLS` so the model is never offered a
#: tool that would just return a ``execute_code_disabled`` envelope; the
#: servicer advertises + dispatches it only when explicitly enabled.
OPTIONAL_CODING_TOOLS: frozenset[str] = frozenset({EXECUTE_CODE_TOOL})


def coding_tool_schemas(*, include_optional: bool = False) -> list[dict]:
    """OpenAI tool descriptors for every coding tool.

    ``include_optional`` adds the opt-in tools (currently ``execute_code``);
    callers that have enabled them pass ``True``.
    """
    schemas = [
        read_file_tool_schema(),
        write_file_tool_schema(),
        edit_file_tool_schema(),
        notebook_edit_tool_schema(),
        list_files_tool_schema(),
        search_files_tool_schema(),
        run_shell_tool_schema(),
        shell_task_output_tool_schema(),
        shell_task_kill_tool_schema(),
        apply_patch_tool_schema(),
        todo_write_tool_schema(),
        revert_changes_tool_schema(),
    ]
    if include_optional:
        schemas.append(execute_code_tool_schema())
    return schemas


__all__ = [
    "APPLY_PATCH_TOOL",
    "CODING_TOOLS",
    "EDIT_FILE_TOOL",
    "ENV_SANDBOX_BACKEND",
    "EXECUTE_CODE_TOOL",
    "LIST_FILES_TOOL",
    "NOTEBOOK_EDIT_TOOL",
    "OPTIONAL_CODING_TOOLS",
    "READ_FILE_TOOL",
    "REVERT_CHANGES_TOOL",
    "RUN_SHELL_TOOL",
    "SEARCH_FILES_TOOL",
    "SHELL_TASK_KILL_TOOL",
    "SHELL_TASK_OUTPUT_TOOL",
    "TODO_WRITE_TOOL",
    "WRITE_FILE_TOOL",
    "DockerEnvironment",
    "Environment",
    "FileState",
    "LocalEnvironment",
    "SpawnedProcess",
    "TodoItem",
    "TodoStore",
    "apply_patch_tool_schema",
    "coding_tool_schemas",
    "dispatch_apply_patch",
    "dispatch_edit_file",
    "dispatch_execute_code",
    "dispatch_list_files",
    "dispatch_notebook_edit",
    "dispatch_read_file",
    "dispatch_revert_changes",
    "dispatch_run_shell",
    "dispatch_search_files",
    "dispatch_shell_task_kill",
    "dispatch_shell_task_output",
    "dispatch_todo_write",
    "dispatch_write_file",
    "edit_file_tool_schema",
    "ensure_repo",
    "execute_code_tool_schema",
    "get_environment",
    "list_files_tool_schema",
    "list_snapshots",
    "notebook_edit_tool_schema",
    "read_file_tool_schema",
    "render_todo_block",
    "resolve_in_workspace",
    "resolve_workspace",
    "revert_changes_tool_schema",
    "revert_last",
    "run_shell_tool_schema",
    "search_files_tool_schema",
    "shell_task_kill_tool_schema",
    "shell_task_output_tool_schema",
    "snapshot",
    "todo_write_tool_schema",
    "write_file_tool_schema",
]
