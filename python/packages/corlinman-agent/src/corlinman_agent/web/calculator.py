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
import math
import operator
import statistics
from collections.abc import Callable
from typing import Any

import structlog

from corlinman_agent.web._common import WebArgsInvalidError, decode_args

logger = structlog.get_logger(__name__)

#: Wire-stable tool name.
CALCULATOR_TOOL: str = "calculator"

#: Scientific function allowlist: the ONLY names ``ast.Call`` may target.
#: Every entry maps to a stdlib :mod:`math` / :mod:`statistics` callable
#: — there is no path to an arbitrary attribute or builtin. Variadic
#: stats functions (mean/median/stdev) accept either a single iterable
#: argument (``mean([1,2,3])``) or positional numbers (``mean(1,2,3)``)
#: via :func:`_stats_adapter`.
def _stats_adapter(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Let a ``statistics`` reducer accept either an iterable or *args."""

    def _call(*args: Any) -> Any:
        if len(args) == 1 and isinstance(args[0], (list, tuple)):
            return fn(args[0])
        return fn(args)

    return _call


_SCI_FUNCS: dict[str, Callable[..., Any]] = {
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "log": math.log,  # log(x) or log(x, base)
    "log10": math.log10,
    "exp": math.exp,
    "pow": math.pow,
    "factorial": math.factorial,
    "hypot": math.hypot,
    "degrees": math.degrees,
    "radians": math.radians,
    "mean": _stats_adapter(statistics.mean),
    "median": _stats_adapter(statistics.median),
    "stdev": _stats_adapter(statistics.stdev),
}

#: Named numeric constants ``ast.Name`` may resolve to. NOTHING else — a
#: bare ``ast.Name`` outside this set is rejected, preserving the
#: no-arbitrary-eval guarantee (no variable / builtin / module lookup).
_SCI_CONSTS: dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}

#: Cap on the integer passed to ``factorial`` so a single call cannot
#: pin the CPU / exhaust memory (``factorial(10**9)``).
_MAX_FACTORIAL = 10_000

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

#: Ceiling on the bit-length of any integer that a ``BinOp`` produces. The
#: per-Pow exponent cap above bounds a SINGLE power, but a NESTED chain like
#: ``(((10**1000)**1000)**1000)**1000`` keeps every exponent at the cap while
#: multiplying them together — the result grows to ~10**12 digits and the
#: synchronous bignum math pins the CPU / exhausts memory before the
#: arithmetic-error guard ever sees it (SEC-03). Bounding the RESULT
#: magnitude after each integer op short-circuits the whole class in ms.
#: ~16 kbit ≈ 5_000 decimal digits — far above any legitimate calculator
#: answer, well below CPython's default int-string-conversion limit (4300
#: digits) so we reject before the value even becomes hard to render.
_MAX_RESULT_BITS = 16_384


def calculator_tool_schema() -> dict[str, Any]:
    """OpenAI-shaped tool descriptor for ``calculator``."""
    return {
        "type": "function",
        "function": {
            "name": CALCULATOR_TOOL,
            "description": (
                "Evaluate a math expression precisely. Supports arithmetic "
                "(+, -, *, /, // floor-div, % modulo, ** power, parentheses) "
                "AND a scientific function allowlist: sqrt, sin, cos, tan, "
                "asin, acos, atan, log (log(x) or log(x, base)), log10, exp, "
                "pow, factorial, hypot, degrees, radians, and the stats "
                "reducers mean/median/stdev (each takes either a bracketed "
                "list like mean([1,2,3]) or positional numbers). The "
                "constants pi, e, and tau are available. Use this instead of "
                "doing multi-digit or trig/log arithmetic yourself. No "
                "variables, no arbitrary functions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": (
                            "The math expression, e.g. '(1234 * 5678) / 90', "
                            "'sqrt(2) * pi', 'log(1000, 10)', or "
                            "'mean([3, 5, 7])'."
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
        result = impl(left, right)
        # Bound the RESULT magnitude, not just the per-op exponent: a nested
        # power chain keeps every exponent at the cap while the running value
        # explodes (SEC-03). ``bit_length`` is O(1)-ish and never stringifies
        # the int, so the check is cheap even on a value too big to render.
        if isinstance(result, int) and result.bit_length() > _MAX_RESULT_BITS:
            raise _UnsafeExpressionError("expression too large")
        return result
    if isinstance(node, ast.UnaryOp):
        impl = _UNARY_OPS.get(type(node.op))
        if impl is None:
            raise _UnsafeExpressionError(
                f"unary operator {type(node.op).__name__} is not allowed"
            )
        return impl(_eval_node(node.operand))
    if isinstance(node, ast.Name):
        # Only the whitelisted numeric constants resolve. Any other bare
        # name (a variable, a builtin like ``__import__``, a module) is
        # rejected — there is no general name lookup.
        const = _SCI_CONSTS.get(node.id)
        if const is None:
            raise _UnsafeExpressionError(
                f"unknown name {node.id!r} (only pi, e, tau allowed)"
            )
        return const
    if isinstance(node, (ast.List, ast.Tuple)):
        # Numeric sequence literals — used as the argument to the stats
        # reducers (``mean([1,2,3])``). Elements are evaluated through the
        # same allowlist, so a list cannot smuggle anything unsafe. We
        # forbid starred elements (``[*x]``).
        elts: list[Any] = []
        for elt in node.elts:
            if isinstance(elt, ast.Starred):
                raise _UnsafeExpressionError("starred elements not allowed")
            elts.append(_eval_node(elt))
        return elts
    if isinstance(node, ast.Call):
        # Calls are permitted ONLY against the scientific allowlist, named
        # directly (``sqrt(2)``). Attribute calls (``math.sqrt``), keyword
        # args, *args/**kwargs unpacking, and any non-Name callee are all
        # rejected so no arbitrary callable can be reached.
        if not isinstance(node.func, ast.Name):
            raise _UnsafeExpressionError(
                "only direct calls to allowed functions are permitted"
            )
        fn = _SCI_FUNCS.get(node.func.id)
        if fn is None:
            raise _UnsafeExpressionError(
                f"function {node.func.id!r} is not allowed"
            )
        if node.keywords:
            raise _UnsafeExpressionError("keyword arguments are not allowed")
        args = [_eval_node(arg) for arg in node.args]
        if node.func.id == "factorial":
            if (
                len(args) != 1
                or not isinstance(args[0], int)
                or isinstance(args[0], bool)
            ):
                raise _UnsafeExpressionError(
                    "factorial requires a single integer argument"
                )
            if args[0] < 0 or args[0] > _MAX_FACTORIAL:
                raise _UnsafeExpressionError(
                    f"factorial argument out of range (0..{_MAX_FACTORIAL})"
                )
        return fn(*args)
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
