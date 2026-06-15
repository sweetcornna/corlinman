import { describe, expect, it } from "vitest";

import { modelSupportsReasoningEffort } from "../reasoning-effort";

describe("modelSupportsReasoningEffort", () => {
  it("allows OpenAI and Codex reasoning-capable model ids", () => {
    expect(modelSupportsReasoningEffort("gpt-5.5")).toBe(true);
    expect(modelSupportsReasoningEffort("openai/gpt-oss-120b")).toBe(true);
    expect(modelSupportsReasoningEffort("o4-mini")).toBe(true);
    expect(modelSupportsReasoningEffort("codex-mini-latest")).toBe(true);
  });

  it("does not send OpenAI reasoning knobs to unrelated provider families", () => {
    expect(modelSupportsReasoningEffort("claude-fable-5")).toBe(false);
    expect(modelSupportsReasoningEffort("gemini-3.5-flash")).toBe(false);
    expect(modelSupportsReasoningEffort("anthropic.claude-sonnet-4-5")).toBe(false);
  });
});
