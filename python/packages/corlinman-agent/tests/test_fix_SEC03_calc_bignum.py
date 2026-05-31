"""SEC-03 repro: calculator nested-power bignum result magnitude guard.

The exponent-only guard (abs(right) > _MAX_POW_EXPONENT) does not bound a
NESTED power whose every exponent is <= the cap but whose RESULT bit-length
explodes — e.g. (((10**1000)**1000)**1000)**1000 multiplies the exponents to
10**12 digits. This computes a giant bignum synchronously. The fix bounds the
result magnitude so such an expression returns expression-too-large fast.
"""

from __future__ import annotations

import json
import time

from corlinman_agent.web.calculator import dispatch_calculator


def test_nested_power_rejected_fast() -> None:
    # Each exponent is 1000 == _MAX_POW_EXPONENT (not over it), so the
    # exponent-only guard never trips, yet the result is astronomically large.
    expr = "(((10**1000)**1000)**1000)**1000"
    t0 = time.monotonic()
    out = json.loads(dispatch_calculator(args_json=json.dumps({"expression": expr})))
    elapsed = time.monotonic() - t0
    # Must be rejected as an invalid (too-large) expression, not computed.
    assert "result" not in out, f"unexpectedly computed a giant bignum: {out!r}"
    assert "error" in out
    assert "too large" in out["error"] or "too_large" in out["error"]
    # And it must be fast — no multi-second bignum math.
    assert elapsed < 1.0, f"took {elapsed:.3f}s — guard did not short-circuit"


def test_normal_power_still_works() -> None:
    out = json.loads(dispatch_calculator(args_json=json.dumps({"expression": "2 ** 10"})))
    assert out["result"] == 1024
