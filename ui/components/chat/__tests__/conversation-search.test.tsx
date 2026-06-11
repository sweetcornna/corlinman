import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { ConversationSearch } from "@/components/chat/conversation-search";
import type { ChatMessage } from "@/lib/chat/types";

const messages: ChatMessage[] = [
  { id: "m1", role: "user", content: "Hello world", createdAt: 0 },
  { id: "m2", role: "assistant", content: "Hi there", createdAt: 0 },
  { id: "m3", role: "user", content: "Hello again — second occurrence", createdAt: 0 },
];

/** Stand-ins for the bubble `<li>`s the highlight reaches by id. */
function mountBubbleStubs() {
  for (const m of messages) {
    const el = document.createElement("div");
    el.id = `chat-msg-${m.id}`;
    document.body.appendChild(el);
  }
}

afterEach(() => {
  document
    .querySelectorAll('[id^="chat-msg-"]')
    .forEach((el) => el.remove());
});

describe("ConversationSearch", () => {
  it("is closed by default and opens via Cmd+F", async () => {
    render(<ConversationSearch messages={messages} onJump={vi.fn()} bindHotkey />);
    expect(screen.queryByTestId("conversation-search")).not.toBeInTheDocument();
    fireEvent.keyDown(window, { key: "f", metaKey: true });
    await waitFor(() => {
      expect(screen.getByTestId("conversation-search")).toBeInTheDocument();
    });
  });

  it("counts matches and jumps to the active one", async () => {
    const onJump = vi.fn();
    render(<ConversationSearch messages={messages} onJump={onJump} bindHotkey />);
    fireEvent.keyDown(window, { key: "f", ctrlKey: true });
    const input = await screen.findByTestId("conversation-search-input");
    fireEvent.change(input, { target: { value: "hello" } });
    await waitFor(() =>
      expect(screen.getByTestId("conversation-search")).toHaveTextContent("1/2"),
    );
    // Two hello matches → first jump on initial render of matches.
    expect(onJump).toHaveBeenCalledWith("m1");

    // Next match
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() =>
      expect(screen.getByTestId("conversation-search")).toHaveTextContent("2/2"),
    );
    expect(onJump).toHaveBeenLastCalledWith("m3");
  });

  it("Escape closes the overlay", async () => {
    render(<ConversationSearch messages={messages} onJump={vi.fn()} bindHotkey />);
    fireEvent.keyDown(window, { key: "f", metaKey: true });
    const input = await screen.findByTestId("conversation-search-input");
    fireEvent.keyDown(input, { key: "Escape" });
    await waitFor(() =>
      expect(screen.queryByTestId("conversation-search")).not.toBeInTheDocument(),
    );
  });

  describe("highlight + focus", () => {
    beforeEach(() => {
      mountBubbleStubs();
      vi.useFakeTimers();
    });
    afterEach(() => {
      vi.runOnlyPendingTimers();
      vi.useRealTimers();
    });

    it("rings the jumped-to bubble, then clears the highlight after 2s", async () => {
      render(<ConversationSearch messages={messages} onJump={vi.fn()} bindHotkey />);
      fireEvent.keyDown(window, { key: "f", metaKey: true });
      const input = screen.getByTestId("conversation-search-input");
      fireEvent.change(input, { target: { value: "hello" } });

      // First match (m1) gets the highlight ring.
      const first = document.getElementById("chat-msg-m1")!;
      expect(first.className).toContain("sg-search-hit");

      // Advancing to the next match moves the ring and clears the old one.
      fireEvent.keyDown(input, { key: "Enter" });
      const second = document.getElementById("chat-msg-m3")!;
      expect(first.className).not.toContain("sg-search-hit");
      expect(second.className).toContain("sg-search-hit");

      // The ring is temporary — gone after 2s.
      vi.advanceTimersByTime(2000);
      expect(second.className).not.toContain("sg-search-hit");
    });
  });

  it("restores focus to the previously focused element on close", async () => {
    vi.useRealTimers();
    const trigger = document.createElement("button");
    document.body.appendChild(trigger);
    trigger.focus();
    expect(document.activeElement).toBe(trigger);

    render(<ConversationSearch messages={messages} onJump={vi.fn()} bindHotkey />);
    fireEvent.keyDown(window, { key: "f", metaKey: true });
    const input = await screen.findByTestId("conversation-search-input");
    fireEvent.keyDown(input, { key: "Escape" });

    await waitFor(() => expect(document.activeElement).toBe(trigger));
    trigger.remove();
  });
});
