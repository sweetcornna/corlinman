/**
 * MessageList a11y + long-thread containment (W6 ② chat-enterprise-parity).
 *
 * Covers:
 *  - the scroll container is an aria `log` region (screen readers track new
 *    messages) and keeps `aria-live="polite"`;
 *  - the W5 "load earlier" pill still renders (regression — it must survive
 *    the containment + a11y changes);
 *  - CSS containment only engages on long threads, never marks the
 *    last (latest / streaming) bubble as skippable, and never leaks to short
 *    threads.
 */

import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";

import type { ChatMessage } from "@/lib/chat/types";

// Keep the markdown node cheap — we only assert structure/attributes here.
import { vi } from "vitest";
vi.mock("@/components/chat/markdown-message", () => ({
  MarkdownMessage: ({ content }: { content: string }) => (
    <div data-testid="markdown">{content}</div>
  ),
}));

import { MessageList } from "@/components/chat/message-list";

function user(id: string, content: string): ChatMessage {
  return { id, role: "user", content, createdAt: 1_700_000_000_000 };
}

function makeThread(n: number): ChatMessage[] {
  return Array.from({ length: n }, (_, i) => user(`m${i}`, `message ${i}`));
}

afterEach(cleanup);

describe("MessageList a11y", () => {
  it("renders the scroll container as a polite aria log region", () => {
    render(<MessageList messages={makeThread(3)} pendingMessage={null} />);
    const log = screen.getByTestId("message-list");
    expect(log).toHaveAttribute("role", "log");
    expect(log).toHaveAttribute("aria-live", "polite");
    // The whole log must not be re-announced on each new message.
    expect(log).toHaveAttribute("aria-atomic", "false");
    expect(log).toHaveAccessibleName();
  });

  it("still renders the load-earlier pill (W5 regression)", () => {
    render(
      <MessageList
        messages={makeThread(3)}
        pendingMessage={null}
        hasEarlier
        onLoadEarlier={() => {}}
      />,
    );
    expect(screen.getByTestId("load-earlier")).toBeInTheDocument();
  });
});

describe("MessageList containment", () => {
  it("does not engage containment on short threads", () => {
    const { container } = render(
      <MessageList messages={makeThread(5)} pendingMessage={null} />,
    );
    const list = container.querySelector("ol");
    expect(list).not.toHaveAttribute("data-contain", "on");
  });

  it("engages containment past the threshold but never on the last bubble", () => {
    const { container } = render(
      <MessageList messages={makeThread(60)} pendingMessage={null} />,
    );
    const list = container.querySelector("ol");
    expect(list).toHaveAttribute("data-contain", "on");

    // Every bubble is still in the DOM (no virtualization) — search jump and
    // anchoring depend on that.
    const bubbles = container.querySelectorAll("li[data-message-id]");
    expect(bubbles.length).toBe(60);

    // The CSS rule targets `li[data-message-id]:not(:last-child)`, so the
    // latest bubble is never content-skipped. We assert the scoping attribute
    // and the presence of a non-leaking scoped <style>.
    const style = container.querySelector('style');
    expect(style?.innerHTML).toContain('content-visibility:auto');
    expect(style?.innerHTML).toContain(':not(:last-child)');
    expect(style?.innerHTML).toContain('[data-contain="on"]');
  });
});
