import { describe, expect, it } from "vitest";

import {
  type ScheduleState,
  composeCron,
  parseCron,
} from "./cron-schedule";
import { nextFireTime } from "./api/scheduler";

const state = (over: Partial<ScheduleState>): ScheduleState => ({
  mode: "daily",
  time: "00:00",
  weekdays: [],
  raw: "",
  ...over,
});

describe("composeCron", () => {
  it("daily → `M H * * *`", () => {
    expect(composeCron(state({ mode: "daily", time: "21:30" }))).toBe(
      "30 21 * * *",
    );
  });

  it("daily strips leading zeros from the cron fields", () => {
    expect(composeCron(state({ mode: "daily", time: "09:05" }))).toBe(
      "5 9 * * *",
    );
  });

  it("weekly → `M H * * d,d,…` with sorted, deduped days", () => {
    expect(
      composeCron(state({ mode: "weekly", time: "21:30", weekdays: [5, 1, 3, 1] })),
    ).toBe("30 21 * * 1,3,5");
  });

  it("weekly emits Sunday as 0, never 7", () => {
    expect(
      composeCron(state({ mode: "weekly", time: "09:00", weekdays: [7] })),
    ).toBe("0 9 * * 0");
  });

  it("advanced returns raw verbatim", () => {
    expect(
      composeCron(state({ mode: "advanced", raw: "*/5 9-17 * * 1-5" })),
    ).toBe("*/5 9-17 * * 1-5");
  });

  it("returns null for weekly with no weekdays selected", () => {
    expect(composeCron(state({ mode: "weekly", time: "09:00", weekdays: [] }))).toBe(
      null,
    );
  });

  it("returns null for a bad time (out-of-range hour)", () => {
    expect(composeCron(state({ mode: "daily", time: "25:00" }))).toBe(null);
  });

  it("returns null for a bad time (single-digit minute)", () => {
    expect(composeCron(state({ mode: "daily", time: "9:5" }))).toBe(null);
  });
});

describe("parseCron", () => {
  it("plain-int minute/hour with */* → daily", () => {
    const s = parseCron("30 21 * * *");
    expect(s.mode).toBe("daily");
    expect(s.time).toBe("21:30");
    expect(s.weekdays).toEqual([]);
    expect(s.raw).toBe("30 21 * * *");
  });

  it("pads the reconstructed time to HH:MM", () => {
    expect(parseCron("5 9 * * *").time).toBe("09:05");
  });

  it("comma dow list → weekly", () => {
    const s = parseCron("30 21 * * 1,3,5");
    expect(s.mode).toBe("weekly");
    expect(s.time).toBe("21:30");
    expect(s.weekdays).toEqual([1, 3, 5]);
  });

  it("normalises dow=7 to 0 on the way in", () => {
    const s = parseCron("0 9 * * 7");
    expect(s.mode).toBe("weekly");
    expect(s.weekdays).toEqual([0]);
  });

  it("step field (*/5) → advanced, raw preserved", () => {
    const s = parseCron("*/5 * * * *");
    expect(s.mode).toBe("advanced");
    expect(s.raw).toBe("*/5 * * * *");
  });

  it("6-field (seconds) cron → advanced, raw preserved", () => {
    const s = parseCron("0 30 21 * * *");
    expect(s.mode).toBe("advanced");
    expect(s.raw).toBe("0 30 21 * * *");
  });

  it("range in dow → advanced", () => {
    expect(parseCron("30 21 * * 1-5").mode).toBe("advanced");
  });

  it("day-of-month constraint → advanced", () => {
    expect(parseCron("0 9 1 * *").mode).toBe("advanced");
  });

  it("empty / garbage never throws and lands on advanced", () => {
    expect(parseCron("").mode).toBe("advanced");
    expect(parseCron("not a cron").mode).toBe("advanced");
  });
});

describe("round-trip composeCron(parseCron(x))", () => {
  it("preserves daily shapes", () => {
    for (const x of ["30 21 * * *", "0 0 * * *", "5 9 * * *"]) {
      expect(composeCron(parseCron(x))).toBe(x);
    }
  });

  it("preserves weekly shapes", () => {
    for (const x of ["30 21 * * 1,3,5", "0 9 * * 1", "15 6 * * 0,6"]) {
      expect(composeCron(parseCron(x))).toBe(x);
    }
  });

  it("folds a Sunday-7 weekly to the equivalent Sunday-0 expression", () => {
    // 7 and 0 are the same day; nextFireTime only matches 0, so 7 must
    // round-trip to 0.
    expect(composeCron(parseCron("0 9 * * 7"))).toBe("0 9 * * 0");
  });

  it("preserves advanced expressions verbatim", () => {
    for (const x of ["*/5 * * * *", "0 30 21 * * *", "0 9 1 * *"]) {
      expect(composeCron(parseCron(x))).toBe(x);
    }
  });
});

// Lock the day-of-week behaviour of the shared preview engine that makes
// composeCron emit 0-not-7. These are behaviour locks, not logic changes.
describe("nextFireTime dow contract (locks compose's 0-not-7 rule)", () => {
  it("matches a comma-separated dow list", () => {
    const from = new Date(2026, 0, 1, 0, 0, 0); // 2026-01-01, a Thursday
    const next = nextFireTime("30 21 * * 1,3,5", from);
    expect(next).not.toBeNull();
    expect([1, 3, 5]).toContain(next!.getDay());
    expect(next!.getHours()).toBe(21);
    expect(next!.getMinutes()).toBe(30);
    expect(next!.getTime()).toBeGreaterThan(from.getTime());
  });

  it("fires for dow=0 (Sunday)", () => {
    const from = new Date(2026, 0, 1, 0, 0, 0);
    const next = nextFireTime("0 12 * * 0", from);
    expect(next).not.toBeNull();
    expect(next!.getDay()).toBe(0);
  });

  it("never fires for dow=7 — the reason composeCron emits 0", () => {
    const from = new Date(2026, 0, 1, 0, 0, 0);
    expect(nextFireTime("0 12 * * 7", from)).toBeNull();
  });
});
