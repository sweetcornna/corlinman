"""Builtin ``todo_write`` tool — a session-scoped task list.

Mirrors opencode's ``todowrite`` and Claude Code's ``TodoWriteTool``: the
model maintains a short list of steps so it stays organised on
multi-step work. Each call **replaces** the whole list.

Item model (Claude Code's shape — ``content`` + ``activeForm``):

    {"content": "Run the tests", "activeForm": "Running the tests",
     "status": "pending" | "in_progress" | "completed"}

State lives in a :class:`TodoStore` keyed by ``session_key``. The store
is held by the agent servicer (one process, one store) so the list
survives across turns of the same conversation. A turn re-injects the
current list into context via :func:`render_todo_block`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import structlog

from corlinman_agent.coding._common import CodingArgsInvalidError, decode_args

logger = structlog.get_logger(__name__)

TODO_WRITE_TOOL: str = "todo_write"

_VALID_STATUS = ("pending", "in_progress", "completed")


@dataclass(slots=True)
class TodoItem:
    """One task in the agent's working list."""

    content: str
    active_form: str
    status: str

    def to_json(self) -> dict[str, str]:
        return {
            "content": self.content,
            "activeForm": self.active_form,
            "status": self.status,
        }


@dataclass
class TodoStore:
    """Per-session task lists. In-process; no cross-process durability —
    a chat turn is a single RPC and that is the scope that matters."""

    _by_session: dict[str, list[TodoItem]] = field(default_factory=dict)

    def get(self, session_key: str) -> list[TodoItem]:
        return list(self._by_session.get(session_key, []))

    def set(self, session_key: str, items: list[TodoItem]) -> None:
        if session_key:
            self._by_session[session_key] = items


def todo_write_tool_schema() -> dict[str, Any]:
    """OpenAI tool descriptor for ``todo_write``."""
    return {
        "type": "function",
        "function": {
            "name": TODO_WRITE_TOOL,
            "description": (
                "Record and update your task list for a multi-step job. "
                "Call it with the FULL list every time (it replaces the "
                "previous list). Use it for any task of 3+ steps: lay out "
                "the steps up front, mark exactly one 'in_progress', and "
                "flip a step to 'completed' the moment it is verified done. "
                "Skip it for trivial single-step requests."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": "The complete task list.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {
                                    "type": "string",
                                    "description": (
                                        "Imperative description, e.g. "
                                        "'Run the tests'."
                                    ),
                                },
                                "activeForm": {
                                    "type": "string",
                                    "description": (
                                        "Present-continuous form, e.g. "
                                        "'Running the tests'."
                                    ),
                                },
                                "status": {
                                    "type": "string",
                                    "enum": list(_VALID_STATUS),
                                },
                            },
                            "required": ["content", "activeForm", "status"],
                            "additionalProperties": False,
                        },
                    }
                },
                "required": ["todos"],
                "additionalProperties": False,
            },
        },
    }


def _parse_items(raw_todos: Any) -> list[TodoItem]:
    """Validate + coerce the ``todos`` argument into :class:`TodoItem`s."""
    if not isinstance(raw_todos, list):
        raise CodingArgsInvalidError("'todos' must be an array")
    items: list[TodoItem] = []
    for i, entry in enumerate(raw_todos):
        if not isinstance(entry, dict):
            raise CodingArgsInvalidError(f"todos[{i}] must be an object")
        content = entry.get("content")
        active = entry.get("activeForm") or entry.get("active_form")
        status = entry.get("status")
        if not isinstance(content, str) or not content.strip():
            raise CodingArgsInvalidError(f"todos[{i}].content is required")
        if not isinstance(active, str) or not active.strip():
            raise CodingArgsInvalidError(f"todos[{i}].activeForm is required")
        if status not in _VALID_STATUS:
            raise CodingArgsInvalidError(
                f"todos[{i}].status must be one of {_VALID_STATUS}"
            )
        items.append(TodoItem(content.strip(), active.strip(), status))
    return items


def dispatch_todo_write(
    *, args_json: bytes | str, store: TodoStore, session_key: str
) -> str:
    """Replace the session's task list. JSON envelope; never raises."""
    try:
        raw = decode_args(args_json)
        items = _parse_items(raw.get("todos"))
    except CodingArgsInvalidError as exc:
        return json.dumps({"error": f"args_invalid: {exc.message}"})

    store.set(session_key, items)

    in_progress = sum(1 for it in items if it.status == "in_progress")
    warning = None
    if in_progress > 1:
        warning = (
            f"{in_progress} tasks are in_progress — keep exactly one active."
        )
    counts = {
        s: sum(1 for it in items if it.status == s) for s in _VALID_STATUS
    }
    logger.info(
        "agent.todo.updated",
        session=session_key,
        total=len(items),
        **counts,
    )
    payload: dict[str, Any] = {
        "todos": [it.to_json() for it in items],
        "counts": counts,
    }
    if warning:
        payload["warning"] = warning
    return json.dumps(payload, ensure_ascii=False)


def render_todo_block(store: TodoStore, session_key: str) -> str | None:
    """Render the session's current task list as a context block, or
    ``None`` when the list is empty. Re-injected each turn so the model
    keeps sight of its plan across messages."""
    items = store.get(session_key)
    if not items:
        return None
    mark = {"pending": "[ ]", "in_progress": "[~]", "completed": "[x]"}
    lines = [
        f"{mark.get(it.status, '[ ]')} {it.content}" for it in items
    ]
    return "## Current task list\n" + "\n".join(lines)


__all__ = [
    "TODO_WRITE_TOOL",
    "TodoItem",
    "TodoStore",
    "dispatch_todo_write",
    "render_todo_block",
    "todo_write_tool_schema",
]
