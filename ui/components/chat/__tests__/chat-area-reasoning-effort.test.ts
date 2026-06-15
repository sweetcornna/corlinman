import { describe, expect, it } from "vitest";

import {
  effectiveReasoningEffortForModel,
  modelAllowsXHighReasoningEffort,
} from "../chat-area";

describe("ChatArea reasoning effort gating", () => {
  it("does not send reasoning effort to obvious non-reasoning model ids", () => {
    expect(effectiveReasoningEffortForModel("gpt-4o", "high")).toBeUndefined();
    expect(
      effectiveReasoningEffortForModel("claude-fable-5", "medium"),
    ).toBeUndefined();
    expect(
      effectiveReasoningEffortForModel("gemini-3.5-flash", "low"),
    ).toBeUndefined();
  });

  it("keeps low/medium/high for aliases so the backend can validate them", () => {
    expect(effectiveReasoningEffortForModel("default", "high")).toBe("high");
    expect(effectiveReasoningEffortForModel("work", "medium")).toBe("medium");
  });

  it("normalizes xhigh away from non-Codex OpenAI models and aliases", () => {
    expect(effectiveReasoningEffortForModel("gpt-5.5", "xhigh")).toBe("high");
    expect(effectiveReasoningEffortForModel("work", "xhigh")).toBe("high");
  });

  it("only allows the xhigh control for Codex-labeled models", () => {
    expect(modelAllowsXHighReasoningEffort("codex-mini-latest")).toBe(true);
    expect(modelAllowsXHighReasoningEffort("gpt-5.5")).toBe(false);
  });
});
