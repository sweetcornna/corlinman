/**
 * `resolveRestartOutcome` — the strict restart-window verdict (v1.28).
 *
 * Strict mode: when the unauthenticated `/health` probe reports a
 * version AND the target tag is known, ONLY an exact match (modulo the
 * leading `v`) counts as success — healthy-but-wrong-version keeps
 * pending. Without both strict inputs it defers to the legacy
 * `detectUpgradeOutcome` heuristic.
 */

import { describe, expect, it } from "vitest";

import {
  detectUpgradeOutcome,
  resolveRestartOutcome,
} from "../upgrade-progress";

describe("resolveRestartOutcome", () => {
  it("succeeds on an exact version match", () => {
    expect(
      resolveRestartOutcome({
        healthVersion: "1.28.0",
        targetTag: "v1.28.0",
        sawServerDown: true,
      }),
    ).toBe("succeeded");
  });

  it("normalizes the leading v on both sides", () => {
    expect(
      resolveRestartOutcome({
        healthVersion: "v1.28.0",
        targetTag: "1.28.0",
        sawServerDown: false,
      }),
    ).toBe("succeeded");
  });

  it("stays pending when the gateway is healthy on the WRONG version", () => {
    // The old heuristic would call this success (version changed!) — the
    // strict signal must not: the backend finalizer will fail the record.
    expect(
      resolveRestartOutcome({
        healthVersion: "1.27.1",
        targetTag: "v1.28.0",
        // Heuristic inputs that would scream "succeeded":
        infoCurrent: "1.27.1",
        currentBefore: "1.27.0",
        infoAvailable: false,
        sawServerDown: true,
      }),
    ).toBe("pending");
  });

  it("falls back to the heuristic when /health has no version (old backend)", () => {
    expect(
      resolveRestartOutcome({
        healthVersion: null,
        targetTag: "v1.28.0",
        infoCurrent: "1.28.0",
        currentBefore: "1.27.0",
        infoAvailable: false,
        sawServerDown: true,
      }),
    ).toBe("succeeded");
  });

  it("falls back to the heuristic when the target tag is unknown", () => {
    expect(
      resolveRestartOutcome({
        healthVersion: "1.28.0",
        targetTag: null,
        infoCurrent: "1.27.0",
        currentBefore: "1.27.0",
        infoAvailable: true,
        sawServerDown: false,
      }),
    ).toBe("pending");
  });
});

describe("detectUpgradeOutcome (legacy heuristic, unchanged)", () => {
  it("succeeds when the reported version changed", () => {
    expect(
      detectUpgradeOutcome({
        infoCurrent: "1.28.0",
        currentBefore: "1.27.0",
        infoAvailable: true,
        sawServerDown: false,
      }),
    ).toBe("succeeded");
  });

  it("succeeds when the server came back with no update available", () => {
    expect(
      detectUpgradeOutcome({
        infoCurrent: "1.27.0",
        currentBefore: "1.27.0",
        infoAvailable: false,
        sawServerDown: true,
      }),
    ).toBe("succeeded");
  });

  it("stays pending otherwise", () => {
    expect(
      detectUpgradeOutcome({
        infoCurrent: "1.27.0",
        currentBefore: "1.27.0",
        infoAvailable: true,
        sawServerDown: false,
      }),
    ).toBe("pending");
  });
});
