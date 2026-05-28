import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import {
  ComposerMentionMenu,
  detectMentionQuery,
} from "@/components/chat/composer-mention-menu";

describe("detectMentionQuery", () => {
  it("returns null when no `@` precedes the caret", () => {
    expect(detectMentionQuery("hello world", 5)).toBeNull();
  });

  it("returns query when caret is inside a fresh @token", () => {
    const out = detectMentionQuery("ping @cl", 8);
    expect(out).toEqual({ query: "cl", start: 5, end: 8 });
  });

  it("aborts on whitespace inside the token", () => {
    expect(detectMentionQuery("ping @cl ic", 11)).toBeNull();
  });

  it("requires whitespace immediately before the `@`", () => {
    expect(detectMentionQuery("foo@bar", 7)).toBeNull();
  });

  it("works for caret at end of string", () => {
    const out = detectMentionQuery("hello @claude", 13);
    expect(out).toEqual({ query: "claude", start: 6, end: 13 });
  });
});

describe("ComposerMentionMenu", () => {
  const candidates = [
    { id: "claude", name: "claude", description: "Anthropic Claude" },
    { id: "gpt", name: "gpt", description: "OpenAI" },
    { id: "explore", name: "explore", description: "Search agent", kind: "agent" as const },
  ];

  it("filters by name + description", () => {
    render(
      <ComposerMentionMenu
        query="ant"
        candidates={candidates}
        onPick={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    // "Anthropic" → claude row
    expect(screen.getByTestId("mention-menu")).toHaveTextContent("@claude");
    expect(screen.getByTestId("mention-menu")).not.toHaveTextContent("@gpt");
  });

  it("returns null when no candidates match", () => {
    const { container } = render(
      <ComposerMentionMenu
        query="zzznomatch"
        candidates={candidates}
        onPick={vi.fn()}
        onClose={vi.fn()}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("fires onPick on click", () => {
    const onPick = vi.fn();
    render(
      <ComposerMentionMenu
        query=""
        candidates={candidates}
        onPick={onPick}
        onClose={vi.fn()}
      />,
    );
    const items = screen.getAllByRole("option");
    fireEvent.click(items[1]);
    expect(onPick).toHaveBeenCalledWith(candidates[1]);
  });
});
