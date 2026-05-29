"""``calculator`` builtin tool — safe arithmetic evaluation.

The "one more cheap, self-contained tool" — no network, no API key, no
state. LLMs are unreliable at multi-digit arithmetic; giving them a
deterministic evaluator removes a whole class of wrong answers.

Safety: the expression is parsed with :mod:`ast` and walked against an
allowlist of node types and operators. There is **no** ``eval`` of
arbitrary code — names, calls, attribute access, comprehensions etc. are
all rejected. Only numeric literals and arithmetic / comparison
operators are permitted.

Wire contract matches the other builtin tools.

Success envelope::  {"expression": "2 + 2*3", "result": 8}
Failure envelope::  {"expression": "...", "error": "..."}
"""

from __future__ import annotations

import ast
import json
import operator
from collections.abc import Callable
from typing import Any

import structlog

from corlinman_agent.web._common import WebArgsInvalidError, decode_args

logger = structlog.get_logger(__name__)

#: Wire-stable tool name.
CALCULATOR_TOOL: str = "calculator"

#: Allowed binary operators → their implementation.
_BIN_OPS: dict[type[ast.operator], Callable[..., Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

#: Allowed unary operators.
_UNARY_OPS: dict[type[ast.unaryop], Callable[..., Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

#: Guard against ``9**9**9``-style resource exhaustion.
_MAX_POW_EXPONENT = 1_000


def calculator_tool_schema() -> dict[str, Any]:
    """OpenAI-shaped tool descriptor for ``calculator``."""
    return {
        "type": "function",
        "function": {
            "name": CALCULATOR_TOOL,
            "description": (
                "Evaluate an arithmetic expression precisely. Supports "
                "+, -, *, /, // (floor div), % (modulo), ** (power) and "
                "parentheses. Use this instead of doing multi-digit "
                "arithmetic yourself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": (
                            "The arithmetic expression, e.g. "
                            "'(1234 * 5678) / 90'."
                        ),
                    }
                },
                "required": ["expression"],
                "additionalProperties": False,
            },
        },
    }


class _UnsafeExpressionError(Exception):
    """Raised when the expression contains a disallowed AST node."""


def _eval_node(node: ast.AST) -> Any:
    """Recursively evaluate an allowlisted AST node."""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(
            node.value, (int, float)
        ):
            raise _UnsafeExpressionError(
                f"only numeric literals allowed, got {node.value!r}"
            )
        return node.value
    if isinstance(node, ast.BinOp):
        op_type = type(node.op)
        impl = _BIN_OPS.get(op_type)
        if impl is None:
            raise _UnsafeExpressionError(
                f"operator {op_type.__name__} is not allowed"
            )
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        if op_type is ast.Pow and isinstance(right, (int, float)):
            if abs(right) > _MAX_POW_EXPONENT:
                raise _UnsafeExpressionError("exponent too large")
        return impl(left, right)
    if isinstance(node, ast.UnaryOp):
        impl = _UNARY_OPS.get(type(node.op))
        if impl is None:
            raise _UnsafeExpressionError(
                f"unary operator {type(node.op).__name__} is not allowed"
            )
        return impl(_eval_node(node.operand))
    raise _UnsafeExpressionError(
        f"expression node {type(node).__name__} is not allowed"
    )


def _evaluate(expression: str) -> int | float:
    """Parse + evaluate an arithmetic string. Raises
    :class:`_UnsafeExpressionError` or arithmetic errors on failure."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise _UnsafeExpressionError(f"syntax error: {exc.msg}") from exc
    result = _eval_node(tree)
    # _eval_node returns Any (it walks dynamic AST values), but the
    # allowlist guarantees only numeric literals + arithmetic operators
    # reach here, so the result is always an int or float.
    if not isinstance(result, (int, float)):
        raise _UnsafeExpressionError(
            f"non-numeric result {type(result).__name__}"
        )
    return result


def dispatch_calculator(*, args_json: bytes | str) -> str:
    """Translate one ``calculator`` tool call into a JSON envelope.

    Synchronous (no I/O). Returns the JSON string for
    ``ToolResult.content``; never raises.
    """
    try:
        raw = decode_args(args_json)
    except WebArgsInvalidError as exc:
        return json.dumps({"error": f"args_invalid: {exc.message}"})

    expression = raw.get("expression")
    if not isinstance(expression, str) or not expression.strip():
        return json.dumps(
            {"error": "args_invalid: missing or empty 'expression' field"}
        )
    expression = expression.strip()

    try:
        result = _evaluate(expression)
    except _UnsafeExpressionError as exc:
        return json.dumps(
            {"expression": expression, "error": f"invalid_expression: {exc}"}
        )
    except ZeroDivisionError:
        return json.dumps(
            {"expression": expression, "error": "division by zero"}
        )
    except (ValueError, OverflowError, ArithmeticError) as exc:
        return json.dumps(
            {"expression": expression, "error": f"arithmetic_error: {exc}"}
        )
    except Exception as exc:  # noqa: BLE001 - dispatcher must never raise
        logger.exception("calculator.unexpected", expression=expression)
        return json.dumps(
            {"expression": expression, "error": f"calculator_failed: {exc}"}
        )

    # JSON can't represent inf/nan — surface a clean error instead.
    if isinstance(result, float) and (
        result != result or result in (float("inf"), float("-inf"))
    ):
        return json.dumps(
            {"expression": expression, "error": "result is not finite"}
        )
    return json.dumps({"expression": expression, "result": result})
