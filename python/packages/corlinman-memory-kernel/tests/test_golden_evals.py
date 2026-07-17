"""The W4 golden-eval CI gate.

Runs the bundled golden set against a real kernel and enforces the hard
invariants. Failures print the per-probe detail so a regression names
the exact behaviour that broke (not just "recall dropped").
"""

from __future__ import annotations

from corlinman_memory_kernel.evals import (
    BUNDLED_GOLDENS,
    load_golden_cases,
    run_goldens,
)

#: Generous bound sized for loaded CI runners — golden DBs are tiny, so
#: anywhere near this means a pathological query plan, not variance.
_P95_BUDGET_MS = 250.0


def test_golden_set_is_nonempty_and_loads() -> None:
    cases = load_golden_cases()
    assert len(cases) >= 8
    assert all(case.probes for case in cases), "every case needs probes"


async def test_golden_evals_pass_with_zero_leaks() -> None:
    report = await run_goldens()
    detail = "\n".join(
        f"  [{f.kind}] {f.case} / {f.probe!r}: {f.detail}"
        for f in report.failures
    )
    # Scope-leak count is the unconditional privacy invariant.
    assert report.leaks == 0, f"SCOPE LEAK(S):\n{detail}"
    assert report.recall == 1.0, f"recall@k regressions:\n{detail}"
    assert report.p95_ms < _P95_BUDGET_MS, (
        f"p95 probe latency {report.p95_ms:.1f}ms exceeds "
        f"{_P95_BUDGET_MS}ms — check the query plan"
    )


async def test_harness_detects_misses_and_leaks() -> None:
    """A gate that cannot fail is vacuous — prove the harness catches
    both failure kinds with a deliberately-broken in-code case."""
    from corlinman_memory_kernel.evals import (
        EvalReport,
        GoldenCase,
        GoldenProbe,
        run_golden_case,
    )

    case = GoldenCase(
        name="deliberate-failure",
        items=[{"scope_user": "u1", "text": "the sky is blue"}],
        probes=[
            GoldenProbe(
                text="sky blue",
                scope_user="u1",
                # Miss: this text was never stored.
                expect=["the grass is green"],
                # Leak: this text WILL surface.
                forbid=["sky is blue"],
            )
        ],
    )
    report = EvalReport()
    await run_golden_case(case, report, [])
    assert report.leaks == 1
    assert report.expected_hit == 0 and report.expected_total == 1
    kinds = sorted(f.kind for f in report.failures)
    assert kinds == ["leak", "miss"]


async def test_goldens_dir_ships_with_package() -> None:
    # The goldens live inside the package tree (not tests/) so the CLI
    # and future prod-side gate ride the wheel. NOTE: from a repo/
    # editable checkout this cannot prove wheel contents — hatchling
    # bundles committed non-.py package files by default; a packaging
    # regression would surface via the CLI on an installed box.
    assert BUNDLED_GOLDENS.is_dir()
    assert list(BUNDLED_GOLDENS.glob("*.yaml"))


def test_loader_rejects_empty_or_missing_dir(tmp_path: object) -> None:
    """A misconfigured goldens path must not look green forever."""
    from pathlib import Path

    import pytest
    from corlinman_memory_kernel.evals import GoldenLoadError, load_golden_cases

    with pytest.raises(GoldenLoadError):
        load_golden_cases(Path(str(tmp_path)) / "nope")
    with pytest.raises(GoldenLoadError):
        load_golden_cases(Path(str(tmp_path)))  # exists but empty
