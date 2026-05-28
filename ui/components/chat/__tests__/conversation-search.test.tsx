import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { ConversationSearch } from "@/components/chat/conversation-search";
import type { ChatMessage } from "@/lib/chat/types";

const messages: ChatMessage[] = [
  { id: "m1", role: "user", content: "Hello world", createdAt: 0 },
  { id: "m2", role: "assistant", content: "Hi there", createdAt: 0 },
  { id: "m3", role: "user", content: "Hello again — second occurrence", createdAt: 0 },
];

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
});
