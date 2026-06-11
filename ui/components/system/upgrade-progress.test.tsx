import { describe, expect, it } from "vitest";

import { phaseProgressPercent } from "./upgrade-progress";
import type { UpgradeStatusResponse } from "@/lib/api";

function status(
  partial: Partial<UpgradeStatusResponse>,
): UpgradeStatusResponse {
  return {
    request_id: "req-1",
    tag: "v1.19.0",
    state: "running",
    phase: "validating",
    started_at: 0,
    finished_at: null,
    log_excerpt: "",
    error: null,
    ...partial,
  };
}

describe("phaseProgressPercent", () => {
  it("returns a small non-zero floor before any status", () => {
    expect(phaseProgressPercent(null)).toBe(3);
  });

  it("fills monotonically through the known phases", () => {
    const v = phaseProgressPercent(status({ phase: "validating" }));
    const p = phaseProgressPercent(status({ phase: "pulling" }));
    const r = phaseProgressPercent(status({ phase: "recreating" }));
    const h = phaseProgressPercent(status({ phase: "healthcheck" }));
    expect(v).toBeLessThan(p);
    expect(p).toBeLessThan(r);
    expect(r).toBeLessThan(h);
    expect(h).toBeLessThan(100);
  });

  it("snaps to 100 on a succeeded terminal", () => {
    expect(phaseProgressPercent(status({ state: "succeeded", phase: "done" }))).toBe(
      100,
    );
    // even if the success frame lacks a known phase
    expect(phaseProgressPercent(status({ state: "succeeded", phase: "" }))).toBe(
      100,
    );
  });

  it("holds at the failed phase mark (not 0, not 100)", () => {
    const pct = phaseProgressPercent(
      status({ state: "failed", phase: "recreating" }),
    );
    expect(pct).toBeGreaterThan(0);
    expect(pct).toBeLessThan(100);
    // same mark as the running phase — the bar simply turns red, it
    // doesn't reset.
    expect(pct).toBe(phaseProgressPercent(status({ phase: "recreating" })));
  });

  it("never regresses below the high-water floor", () => {
    // A failed/stalled terminal whose phase is a backend code not in
    // PHASE_ORDER must hold near where it died, not snap to the floor.
    expect(
      phaseProgressPercent(
        status({ state: "failed", phase: "image_pull_failed" }),
        72,
      ),
    ).toBe(72);
    expect(
      phaseProgressPercent(status({ state: "stalled", phase: "timeout" }), 90),
    ).toBe(90);
    // succeeded still wins over any floor.
    expect(
      phaseProgressPercent(status({ state: "succeeded", phase: "done" }), 45),
    ).toBe(100);
  });

  it("falls back to the floor for an unknown/early phase", () => {
    // default floor when no high-water has been recorded yet
    expect(phaseProgressPercent(status({ phase: "queued-something" }))).toBe(3);
  });
});
