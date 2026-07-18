import { describe, expect, it } from "vitest";

import {
  CANONICAL_REASONING_TIERS,
  clampReasoningTier,
  isReasoningTier,
} from "@/lib/chat/reasoning-effort";
import { effectiveReasoningEffortForModel } from "@/components/chat/chat-area";

describe("isReasoningTier", () => {
  it("accepts every canonical tier and rejects junk", () => {
    for (const tier of CANONICAL_REASONING_TIERS) {
      expect(isReasoningTier(tier)).toBe(true);
    }
    expect(isReasoningTier("turbo")).toBe(false);
    expect(isReasoningTier("")).toBe(false);
    expect(isReasoningTier(undefined)).toBe(false);
  });
});

describe("clampReasoningTier", () => {
  it("passes a supported tier through", () => {
    expect(clampReasoningTier(["low", "medium", "high"], "medium")).toBe(
      "medium",
    );
  });

  it("snaps to the nearest tier, ties resolving downward", () => {
    // o-series ladder: max → high
    expect(clampReasoningTier(["low", "medium", "high"], "max")).toBe("high");
    // gemini-3-pro ladder: medium is equidistant → low (cost-conservative)
    expect(clampReasoningTier(["low", "high"], "medium")).toBe("low");
    // toggle ladder: graded request lands on `on`
    expect(clampReasoningTier(["none", "on"], "high")).toBe("on");
  });

  it("returns undefined for an empty ladder or junk request", () => {
    expect(clampReasoningTier([], "high")).toBeUndefined();
    expect(clampReasoningTier(["low"], "turbo")).toBeUndefined();
  });

  it("ignores unknown strings inside the ladder", () => {
    expect(clampReasoningTier(["mega", "high"], "max")).toBe("high");
  });
});

describe("effectiveReasoningEffortForModel × reasoning_tiers", () => {
  it("clamps onto the API-provided ladder", () => {
    expect(
      effectiveReasoningEffortForModel("cornna", "max", "cornna", "gpt-5.6-sol", [
        "none",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
      ]),
    ).toBe("max");
    expect(
      effectiveReasoningEffortForModel("mini", "max", null, "o3-mini", [
        "low",
        "medium",
        "high",
      ]),
    ).toBe("high");
  });

  it("sends nothing for a known no-knob model", () => {
    expect(
      effectiveReasoningEffortForModel("grok", "high", null, "grok-4", []),
    ).toBeUndefined();
  });

  it("falls back to legacy heuristics when the ladder is unknown", () => {
    // codex keeps xhigh
    expect(
      effectiveReasoningEffortForModel("codex-mini", "xhigh", null, null, null),
    ).toBe("xhigh");
    // non-reasoning legacy model sends nothing
    expect(
      effectiveReasoningEffortForModel("gpt-4o", "high", null, null, null),
    ).toBeUndefined();
  });
});
