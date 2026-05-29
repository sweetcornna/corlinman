/**
 * Streaming re-render isolation (R4-D5).
 *
 * Repro: during streaming, every token delta updates `pendingMessage`,
 * which re-renders `<MessageList>`. Each settled historical assistant
 * bubble must NOT re-render (and must not re-run its markdown parse) when
 * only the pending message changes. Without `React.memo` on
 * `MessageBubble`, every settled bubble re-parses markdown on every delta
 * (O(H × markdown-size) per token).
 *
 * We spy on `MarkdownMessage` and count renders keyed by message content.
 * Settled assistant bubbles have stable content, so their parse count must
 * stay at 1 across a pending-only re-render.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render } from "@testing-library/react";

import type { ChatMessage } from "@/lib/chat/types";

// Count how many times the markdown renderer runs for each content string.
// `MarkdownMessage` is the expensive node (full markdown re-parse), so its
// render count is the proxy for "did this settled bubble re-render".
const markdownRenderCounts = new Map<string, number>();

vi.mock("@/components/chat/markdown-message", () => ({
  MarkdownMessage: ({ content }: { content: string }) => {
    markdownRenderCounts.set(content, (markdownRenderCounts.get(content) ?? 0) + 1);
    return <div data-testid="markdown">{content}</div>;
  },
}));

import { MessageList } from "@/components/chat/message-list";

function assistant(id: string, content: string): ChatMessage {
  return { id, role: "assistant", content, createdAt: 1_700_000_000_000 };
}

afterEach(() => {
  cleanup();
  markdownRenderCounts.clear();
});

describe("MessageList streaming re-render isolation", () => {
  it("does not re-render settled assistant bubbles when only pendingMessage changes", () => {
    // Three settled historical assistant messages — stable identity.
    const settled: ChatMessage[] = [
      assistant("a1", "first settled answer"),
      assistant("a2", "second settled answer"),
      assistant("a3", "third settled answer"),
    ];

    const pendingV1: ChatMessage = {
      id: "pending",
      role: "assistant",
      content: "stream",
      createdAt: 1_700_000_000_001,
      pending: true,
    };

    const { rerender } = render(
      <MessageList messages={settled} pendingMessage={pendingV1} />,
    );

    // Each settled bubble parsed its markdown exactly once on first mount.
    for (const m of settled) {
      expect(markdownRenderCounts.get(m.content)).toBe(1);
    }

    // Simulate a streaming token delta: same `messages` array + element
    // identities, only `pendingMessage` gets a new content/identity.
    const pendingV2: ChatMessage = { ...pendingV1, content: "stream more" };
    rerender(<MessageList messages={settled} pendingMessage={pendingV2} />);

    // The settled bubbles' markdown must NOT have re-parsed.
    for (const m of settled) {
      expect(markdownRenderCounts.get(m.content)).toBe(1);
    }
  });
});
