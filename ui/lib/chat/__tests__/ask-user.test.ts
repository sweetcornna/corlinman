import { describe, expect, it } from "vitest";

import {
  ASK_USER_TOOL_NAME,
  askUserQuestions,
  parseAskUserArgs,
} from "@/lib/chat/ask-user";
import type { ToolCallState } from "@/lib/chat/types";

describe("parseAskUserArgs", () => {
  it("parses question + options + multiple", () => {
    const parsed = parseAskUserArgs(
      JSON.stringify({
        question: "侧重哪方面?",
        options: ["代码质量", "安全"],
        multiple: true,
      }),
    );
    expect(parsed).toEqual({
      question: "侧重哪方面?",
      options: ["代码质量", "安全"],
      multiple: true,
    });
  });

  it("returns null for partial/invalid JSON (still streaming)", () => {
    expect(parseAskUserArgs('{"question": "hal')).toBeNull();
    expect(parseAskUserArgs("")).toBeNull();
    expect(parseAskUserArgs("[]")).toBeNull();
  });

  it("returns null when the question is empty", () => {
    expect(parseAskUserArgs('{"question": "  "}')).toBeNull();
    expect(parseAskUserArgs('{"options": ["a"]}')).toBeNull();
  });

  it("mirrors backend caps: 8 options, newlines stripped, labels trimmed", () => {
    const parsed = parseAskUserArgs(
      JSON.stringify({
        question: "q",
        options: [
          "one\ntwo",
          "  ",
          ...Array.from({ length: 10 }, (_, i) => `opt-${i}`),
        ],
      }),
    );
    expect(parsed?.options[0]).toBe("one two");
    // 8 raw entries considered, blank dropped → 7 survive
    expect(parsed?.options).toHaveLength(7);
  });

  it("caps an overlong option label at 120 chars", () => {
    const parsed = parseAskUserArgs(
      JSON.stringify({ question: "q", options: ["x".repeat(300)] }),
    );
    expect(parsed?.options[0]).toHaveLength(120);
    expect(parsed?.options[0]?.endsWith("…")).toBe(true);
  });
});

describe("askUserQuestions", () => {
  const tc = (toolName: string, argsJson: string): ToolCallState => ({
    callId: `c-${toolName}`,
    toolName,
    argsJson,
    status: "ok",
  });

  it("collects only parseable ask_user calls, in order", () => {
    const out = askUserQuestions([
      tc("read_file", '{"path": "x"}'),
      tc(ASK_USER_TOOL_NAME, '{"question": "A?"}'),
      tc(ASK_USER_TOOL_NAME, "{broken"),
      tc(ASK_USER_TOOL_NAME, '{"question": "B?", "options": ["1"]}'),
    ]);
    expect(out.map((q) => q.question)).toEqual(["A?", "B?"]);
    expect(out[1]?.options).toEqual(["1"]);
  });

  it("returns [] for undefined/empty", () => {
    expect(askUserQuestions(undefined)).toEqual([]);
    expect(askUserQuestions([])).toEqual([]);
  });
});
