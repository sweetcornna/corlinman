"""gap-fill lane-calc-web: scientific calculator allowlist.

Covers gap ``calculator-scientific-symbolic``: the AST evaluator now
permits a whitelist of stdlib math/statistics functions and the
constants pi/e/tau, while still rejecting every other Call / Name /
Attribute (no arbitrary eval).
"""

from __future__ import annotations

import json
import math

import pytest
from corlinman_agent.web.calculator import (
    calculator_tool_schema,
    dispatch_calculator,
)


def _calc(expr: str) -> dict:
    return json.loads(dispatch_calculator(args_json=json.dumps({"expression": expr})))


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("sqrt(2)", math.sqrt(2)),
        ("sqrt(9)", 3.0),
        ("sin(0)", 0.0),
        ("cos(0)", 1.0),
        ("tan(0)", 0.0),
        ("asin(1)", math.pi / 2),
        ("acos(1)", 0.0),
        ("atan(0)", 0.0),
        ("log(1)", 0.0),
        ("log(1000, 10)", 3.0),
        ("log10(1000)", 3.0),
        ("exp(0)", 1.0),
        ("pow(2, 10)", 1024.0),
        ("factorial(5)", 120),
        ("hypot(3, 4)", 5.0),
        ("degrees(pi)", 180.0),
        ("radians(180)", math.pi),
    ],
)
def test_scientific_functions_evaluate(expr: str, expected: float) -> None:
    out = _calc(expr)
    assert "error" not in out, out
    assert out["result"] == pytest.approx(expected)


@pytest.mark.parametrize(
    ("name", "value"),
    [("pi", math.pi), ("e", math.e), ("tau", math.tau)],
)
def test_named_constants_evaluate(name: str, value: float) -> None:
    out = _calc(name)
    assert out["result"] == pytest.approx(value)


def test_constants_combine_with_arithmetic() -> None:
    out = _calc("2 + sqrt(9) * pi")
    assert out["result"] == pytest.approx(2 + 3 * math.pi)


@pytest.mark.parametrize(
    "expr",
    [
        "mean([3, 5, 7])",
        "mean(3, 5, 7)",
        "median([1, 2, 3, 4])",
        "stdev([2, 4, 4, 4, 5, 5, 7, 9])",
    ],
)
def test_stats_reducers_accept_list_or_args(expr: str) -> None:
    out = _calc(expr)
    assert "error" not in out, out
    assert isinstance(out["result"], (int, float))


def test_mean_list_and_args_agree() -> None:
    assert _calc("mean([3, 5, 7])")["result"] == _calc("mean(3, 5, 7)")["result"] == 5


@pytest.mark.parametrize(
    "evil",
    [
        "__import__('os')",
        "os.system('ls')",
        "open('x')",
        "x + 1",
        "[i for i in range(3)]",
        "math.sqrt(2)",  # attribute access rejected
        "sqrt(x=2)",  # keyword args rejected
        "eval('1')",
        "foo()",  # un-whitelisted function
        "[1, 2, 3]",  # bare list is not a numeric result
        "().__class__",
        "lambda: 1",
    ],
)
def test_rejects_arbitrary_eval(evil: str) -> None:
    out = _calc(evil)
    assert "error" in out
    assert "result" not in out


def test_factorial_rejects_huge_and_negative() -> None:
    for expr in ["factorial(10 ** 9)", "factorial(-3)", "factorial(2.5)"]:
        out = _calc(expr)
        assert out["error"].startswith("invalid_expression:"), (expr, out)


def test_plain_arithmetic_still_works() -> None:
    # Regression: the original arithmetic path is untouched.
    assert _calc("2 + 3 * 4")["result"] == 14
    assert _calc("17 % 5")["result"] == 2
    assert _calc("2 ** 10")["result"] == 1024


def test_schema_mentions_scientific_functions() -> None:
    schema = calculator_tool_schema()
    desc = schema["function"]["description"].lower()
    assert "sqrt" in desc
    assert "mean" in desc
    assert "pi" in desc
