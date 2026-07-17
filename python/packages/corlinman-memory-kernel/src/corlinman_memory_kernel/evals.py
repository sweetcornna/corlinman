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


def load_golden_cases(goldens_dir: Path | None = None) -> list[GoldenCase]:
    """Load every ``*.yaml`` golden case, sorted by filename."""
    root = goldens_dir or BUNDLED_GOLDENS
    cases: list[GoldenCase] = []
    for path in sorted(root.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise GoldenLoadError(f"{path.name}: {exc}") from exc
        if not isinstance(raw, dict):
            raise GoldenLoadError(f"{path.name}: top level must be a mapping")
        try:
            probes = [
                GoldenProbe(
                    text=str(p["text"]),
                    scope_user=p.get("scope_user"),
                    persona=str(p.get("persona", "")),
                    top_k=int(p.get("top_k", 4)),
                    expect=[str(e) for e in p.get("expect", [])],
                    forbid=[str(fb) for fb in p.get("forbid", [])],
                )
                for p in raw.get("probes", [])
            ]
            cases.append(
                GoldenCase(
                    name=str(raw.get("name", path.stem)),
                    description=str(raw.get("description", "")),
                    items=list(raw.get("items", [])),
                    probes=probes,
                    min_recall=float(raw.get("min_recall", 1.0)),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GoldenLoadError(f"{path.name}: {exc}") from exc
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
                    scope, probe.text, top_k=probe.top_k
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
        report.p95_ms = (
            statistics.quantiles(latencies_ms, n=20)[-1]
            if len(latencies_ms) >= 2
            else latencies_ms[0]
        )
    return report


def main() -> int:
    """CLI: run the bundled goldens, print JSON, exit 1 on any failure."""
    report = asyncio.run(run_goldens())
    print(json.dumps(report.to_json(), ensure_ascii=False, indent=2))
    return 1 if (report.leaks or report.recall < 1.0) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
