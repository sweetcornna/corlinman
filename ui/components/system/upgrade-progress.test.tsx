import { describe, expect, it } from "vitest";

import { detectUpgradeOutcome } from "./upgrade-progress";

describe("detectUpgradeOutcome", () => {
  it("is pending before the server comes back on a new version", () => {
    // Same version as before, still up, update still available.
    expect(
      detectUpgradeOutcome({
        infoCurrent: "1.21.8",
        infoAvailable: true,
        currentBefore: "1.21.8",
        sawServerDown: false,
      }),
    ).toBe("pending");
  });

  it("succeeds when the reported version differs from the pre-upgrade one", () => {
    expect(
      detectUpgradeOutcome({
        infoCurrent: "1.27.0",
        infoAvailable: false,
        currentBefore: "1.21.8",
        sawServerDown: true,
      }),
    ).toBe("succeeded");
  });

  it("succeeds on a version change even if we never observed a restart (fast path)", () => {
    // Poll interval may miss the brief downtime; a changed version is
    // still definitive.
    expect(
      detectUpgradeOutcome({
        infoCurrent: "1.27.0",
        infoAvailable: false,
        currentBefore: "1.21.8",
        sawServerDown: false,
      }),
    ).toBe("succeeded");
  });

  it("succeeds after a restart when no update remains, even without a known baseline", () => {
    // currentBefore unknown (e.g. /info hadn't loaded), but we watched the
    // gateway go down and come back reporting up-to-date.
    expect(
      detectUpgradeOutcome({
        infoCurrent: "1.27.0",
        infoAvailable: false,
        currentBefore: null,
        sawServerDown: true,
      }),
    ).toBe("succeeded");
  });

  it("stays pending after a restart while an update is still advertised", () => {
    // Server bounced but somehow still reports an update — don't call it
    // done (would trigger a premature reload loop).
    expect(
      detectUpgradeOutcome({
        infoCurrent: "1.21.8",
        infoAvailable: true,
        currentBefore: "1.21.8",
        sawServerDown: true,
      }),
    ).toBe("pending");
  });

  it("stays pending when the baseline is unknown and the server never went down", () => {
    expect(
      detectUpgradeOutcome({
        infoCurrent: "1.21.8",
        infoAvailable: true,
        currentBefore: null,
        sawServerDown: false,
      }),
    ).toBe("pending");
  });
});
