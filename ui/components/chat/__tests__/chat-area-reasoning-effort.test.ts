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

  it("does not send reasoning effort for opaque aliases", () => {
    expect(effectiveReasoningEffortForModel("default", "high")).toBeUndefined();
    expect(effectiveReasoningEffortForModel("work", "medium")).toBeUndefined();
  });

  it("keeps reasoning effort when alias metadata resolves to a capable model", () => {
    expect(effectiveReasoningEffortForModel("default", "high", null, "gpt-5.5")).toBe(
      "high",
    );
    expect(
      effectiveReasoningEffortForModel("default", "medium", null, "gpt-4o"),
    ).toBeUndefined();
  });

  it("normalizes xhigh away from non-Codex OpenAI models and aliases", () => {
    expect(effectiveReasoningEffortForModel("gpt-5.5", "xhigh")).toBe("high");
    expect(effectiveReasoningEffortForModel("gpt-5.5", "xhigh", "openai")).toBe(
      "high",
    );
    expect(effectiveReasoningEffortForModel("work", "xhigh")).toBeUndefined();
  });

  it("allows xhigh for Codex models and Codex-provisioned aliases", () => {
    expect(modelAllowsXHighReasoningEffort("codex-mini-latest")).toBe(true);
    expect(modelAllowsXHighReasoningEffort("gpt-5.5", "codex")).toBe(true);
    expect(modelAllowsXHighReasoningEffort("fast", null, "codex-mini-latest")).toBe(
      true,
    );
    expect(modelAllowsXHighReasoningEffort("gpt-5.5")).toBe(false);
    expect(modelAllowsXHighReasoningEffort("gpt-5.5", "openai")).toBe(false);
    expect(effectiveReasoningEffortForModel("gpt-5.5", "xhigh", "codex")).toBe(
      "xhigh",
    );
  });
});
