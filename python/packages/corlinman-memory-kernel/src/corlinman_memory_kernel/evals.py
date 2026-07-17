"""Memory golden evals — YAML-scripted recall regression harness (W4).

Memory regressions are silent: a scoping bug leaks another user's note,
a tokenizer change stops Chinese recall, a ranking tweak buries the
fact that matters — and every unit test still passes. This harness pins
the SYSTEM behaviour: each golden case scripts a store state (items
across scopes, some invalidated) and a set of recall probes with
expected and forbidden outcomes, then runs them against a real
:class:`MemoryKernel` on a throwaway database.

Hard invariants (enforced by the pytest gate in
``tests/test_golden_evals.py``):

- **scope-leak count == 0** — a forbidden text surfacing in any probe
  fails the suite, always.
- recall@k ≥ the case's ``min_recall`` (default 1.0 — expected texts
  must ALL surface).
- p95 probe latency under a generous local bound.

The same harness is the gate for belief-mutating pipelines (W5
consolidation, W8 dream demotions): run the goldens before and after,
require a non-negative delta. CLI: ``python -m corlinman_memory_kernel.evals``.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from corlinman_memory_kernel.kernel import MemoryKernel
from corlinman_memory_kernel.types import KernelScope

#: Directory of bundled golden cases (shipped with the package so the
#: CLI and external gates run the same set the CI gate pins).
BUNDLED_GOLDENS = Path(__file__).parent / "goldens"


@dataclass
class GoldenProbe:
    text: str
    scope_user: str | None = None
    persona: str = ""
    top_k: int = 4
    expect: list[str] = field(default_factory=list)
    forbid: list[str] = field(default_factory=list)
    # W6 affect lens: probe-time persona mood [e, p, a] + affect weight.
    mood: list[float] | None = None
    affect_weight: float = 0.0


@dataclass
class GoldenCase:
    name: str
    description: str = ""
    items: list[dict[str, Any]] = field(default_factory=list)
    probes: list[GoldenProbe] = field(default_factory=list)
    min_recall: float = 1.0


@dataclass
class ProbeFailure:
    case: str
    probe: str
    kind: str  # "miss" | "leak"
    detail: str


@dataclass
class EvalReport:
    cases: int = 0
    probes: int = 0
    expected_total: int = 0
    expected_hit: int = 0
    leaks: int = 0
    p95_ms: float = 0.0
    failures: list[ProbeFailure] = field(default_factory=list)

    @property
    def recall(self) -> float:
        if self.expected_total == 0:
            return 1.0
        return self.expected_hit / self.expected_total

    def to_json(self) -> dict[str, Any]:
        return {
            "cases": self.cases,
            "probes": self.probes,
            "recall": round(self.recall, 4),
            "leaks": self.leaks,
            "p95_ms": round(self.p95_ms, 2),
            "failures": [
                {
                    "case": f.case,
                    "probe": f.probe,
                    "kind": f.kind,
                    "detail": f.detail,
                }
                for f in self.failures
            ],
        }


class GoldenLoadError(RuntimeError):
    """A golden YAML file is malformed."""


def _validated_mood(raw: Any, filename: str) -> list[float] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) != 3:
        raise GoldenLoadError(f"{filename}: mood must be a 3-element [e,p,a]")
    return [float(x) for x in raw]


def load_golden_cases(goldens_dir: Path | None = None) -> list[GoldenCase]:
    """Load every ``*.yaml`` golden case, sorted by filename.

    A missing directory or an empty set raises — a misconfigured path
    that silently shadows zero cases would look green forever (same
    guard the shadow-tester's loader enforces).
    """
    root = goldens_dir or BUNDLED_GOLDENS
    if not root.is_dir():
        raise GoldenLoadError(f"goldens dir missing: {root}")
    cases: list[GoldenCase] = []
    for path in sorted(root.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise GoldenLoadError(f"{path.name}: {exc}") from exc
        if not isinstance(raw, dict):
            raise GoldenLoadError(f"{path.name}: top level must be a mapping")
        try:
            items = list(raw.get("items", []))
            for item in items:
                # Validate at load so a malformed item names its file
                # instead of aborting the whole run with a bare KeyError
                # from the seeding step.
                if not isinstance(item, dict) or "text" not in item:
                    raise GoldenLoadError(
                        f"{path.name}: every item needs a 'text' key"
                    )
            probes = [
                GoldenProbe(
                    text=str(p["text"]),
                    scope_user=p.get("scope_user"),
                    persona=str(p.get("persona", "")),
                    top_k=int(p.get("top_k", 4)),
                    expect=[str(e) for e in p.get("expect", [])],
                    forbid=[str(fb) for fb in p.get("forbid", [])],
                    mood=_validated_mood(p.get("mood"), path.name),
                    affect_weight=float(p.get("affect_weight", 0.0)),
                )
                for p in raw.get("probes", [])
            ]
            cases.append(
                GoldenCase(
                    name=str(raw.get("name", path.stem)),
                    description=str(raw.get("description", "")),
                    items=items,
                    probes=probes,
                    min_recall=float(raw.get("min_recall", 1.0)),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GoldenLoadError(f"{path.name}: {exc}") from exc
    if not cases:
        raise GoldenLoadError(f"no golden cases found under {root}")
    return cases


async def _seed_case(kernel: MemoryKernel, case: GoldenCase) -> None:
    for item in case.items:
        scope = KernelScope(
            scope_user_id=item.get("scope_user"),
            persona_id=str(item.get("persona", "")),
        )
        item_id = await kernel.add_item(
            scope,
            text=str(item["text"]),
            kind=str(item.get("kind", "fact")),
            source=str(item.get("source", "golden")),
            risk=str(item.get("risk", "low")),
            trust=float(item.get("trust", 0.5)),
            importance=float(item.get("importance", 0.5)),
        )
        affect = item.get("affect")
        if isinstance(affect, list) and len(affect) == 4:
            await kernel.set_affect(
                item_id,
                float(affect[0]),
                float(affect[1]),
                float(affect[2]),
                float(affect[3]),
            )
        if item.get("invalidate"):
            await kernel.invalidate_item(
                item_id, reason=str(item.get("invalidate_reason", "golden"))
            )


async def run_golden_case(
    case: GoldenCase, report: EvalReport, latencies_ms: list[float]
) -> None:
    """Run one case against a fresh throwaway kernel DB."""
    with tempfile.TemporaryDirectory(prefix="mk-golden-") as tmp:
        kernel = await MemoryKernel.open(Path(tmp) / "memory.sqlite")
        try:
            await _seed_case(kernel, case)
            for probe in case.probes:
                scope = KernelScope(
                    scope_user_id=probe.scope_user, persona_id=probe.persona
                )
                started = time.monotonic()
                hits = await kernel.recall_ranked(
                    scope,
                    probe.text,
                    top_k=probe.top_k,
                    weights=(
                        {"w_aff": probe.affect_weight}
                        if probe.affect_weight > 0
                        else None
                    ),
                    mood=(
                        (probe.mood[0], probe.mood[1], probe.mood[2])
                        if probe.mood
                        else None
                    ),
                )
                latencies_ms.append((time.monotonic() - started) * 1000.0)
                report.probes += 1
                joined = "\n".join(h.text for h in hits)
                for expected in probe.expect:
                    report.expected_total += 1
                    if expected in joined:
                        report.expected_hit += 1
                    else:
                        report.failures.append(
                            ProbeFailure(
                                case=case.name,
                                probe=probe.text,
                                kind="miss",
                                detail=f"expected {expected!r} absent",
                            )
                        )
                for forbidden in probe.forbid:
                    if forbidden in joined:
                        report.leaks += 1
                        report.failures.append(
                            ProbeFailure(
                                case=case.name,
                                probe=probe.text,
                                kind="leak",
                                detail=f"forbidden {forbidden!r} surfaced",
                            )
                        )
        finally:
            await kernel.close()


async def run_goldens(goldens_dir: Path | None = None) -> EvalReport:
    """Run the whole golden set; the caller applies pass/fail policy."""
    report = EvalReport()
    latencies_ms: list[float] = []
    for case in load_golden_cases(goldens_dir):
        report.cases += 1
        await run_golden_case(case, report, latencies_ms)
    if latencies_ms:
        # method="inclusive" is bounded by the sample max; the default
        # exclusive method EXTRAPOLATES above it for small n, letting a
        # single slow probe manufacture a phantom p95 breach.
        report.p95_ms = (
            statistics.quantiles(latencies_ms, n=20, method="inclusive")[-1]
            if len(latencies_ms) >= 2
            else latencies_ms[0]
        )
    return report


#: CLI latency budget — generous for loaded CI/prod boxes; anywhere near
#: it means a pathological query plan, not variance.
CLI_P95_BUDGET_MS = 250.0


def main(argv: list[str] | None = None) -> int:
    """CLI: run a golden set, print JSON, exit 1 on ANY failure
    (leak, recall miss, or a busted latency budget)."""
    import argparse

    parser = argparse.ArgumentParser(prog="corlinman-memory-evals")
    parser.add_argument(
        "--goldens-dir",
        type=Path,
        default=None,
        help="directory of *.yaml golden cases (default: bundled set)",
    )
    args = parser.parse_args(argv)
    report = asyncio.run(run_goldens(args.goldens_dir))
    print(json.dumps(report.to_json(), ensure_ascii=False, indent=2))
    failed = (
        report.leaks > 0
        or report.recall < 1.0
        or report.p95_ms >= CLI_P95_BUDGET_MS
    )
    return 1 if failed else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
