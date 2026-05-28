import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

import { ChatSidebar } from "@/components/chat/chat-sidebar";
import type { ChatConversation } from "@/lib/chat/types";

const MS = 86_400_000;

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

function mk(overrides: Partial<ChatConversation>): ChatConversation {
  return {
    sessionKey: "corlinman:1",
    title: "Conv",
    pinned: false,
    archived: false,
    lastMessageAt: Date.now(),
    messageCount: 2,
    ...overrides,
  };
}

describe("ChatSidebar", () => {
  it("renders an empty state when no conversations", () => {
    render(
      <ChatSidebar
        conversations={[]}
        activeSessionKey={null}
        onNew={vi.fn()}
        onRename={vi.fn()}
        onTogglePin={vi.fn()}
        onToggleArchive={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    expect(screen.getByText("暂无对话")).toBeInTheDocument();
  });

  it("groups conversations by recency and surfaces Pinned first", () => {
    const now = Date.now();
    const conversations: ChatConversation[] = [
      mk({ sessionKey: "a", title: "Today A", lastMessageAt: now - 60_000 }),
      mk({ sessionKey: "b", title: "Yesterday B", lastMessageAt: now - MS * 1.2 }),
      mk({ sessionKey: "c", title: "Pinned C", pinned: true, lastMessageAt: now - MS * 5 }),
      mk({ sessionKey: "d", title: "Archived D", archived: true, lastMessageAt: now }),
    ];
    render(
      <ChatSidebar
        conversations={conversations}
        activeSessionKey="a"
        onNew={vi.fn()}
        onRename={vi.fn()}
        onTogglePin={vi.fn()}
        onToggleArchive={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    expect(screen.getByText("已置顶")).toBeInTheDocument();
    expect(screen.getByText("今天")).toBeInTheDocument();
    expect(screen.getByText("昨天")).toBeInTheDocument();
    expect(screen.getByText("已归档")).toBeInTheDocument();
  });

  it("calls onNew when the New chat button is clicked", () => {
    const onNew = vi.fn();
    render(
      <ChatSidebar
        conversations={[]}
        activeSessionKey={null}
        onNew={onNew}
        onRename={vi.fn()}
        onTogglePin={vi.fn()}
        onToggleArchive={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId("chat-sidebar-new"));
    expect(onNew).toHaveBeenCalledOnce();
  });

  it("filters by fuzzy match against title", () => {
    const conversations: ChatConversation[] = [
      mk({ sessionKey: "a", title: "Cooking pasta" }),
      mk({ sessionKey: "b", title: "Deploy script" }),
    ];
    render(
      <ChatSidebar
        conversations={conversations}
        activeSessionKey={null}
        onNew={vi.fn()}
        onRename={vi.fn()}
        onTogglePin={vi.fn()}
        onToggleArchive={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByTestId("chat-sidebar-search"), {
      target: { value: "dep" },
    });
    expect(screen.getByText("Deploy script")).toBeInTheDocument();
    expect(screen.queryByText("Cooking pasta")).not.toBeInTheDocument();
  });

  it("highlights the active row via data-active", () => {
    render(
      <ChatSidebar
        conversations={[mk({ sessionKey: "x", title: "X" })]}
        activeSessionKey="x"
        onNew={vi.fn()}
        onRename={vi.fn()}
        onTogglePin={vi.fn()}
        onToggleArchive={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    expect(
      screen.getByTestId("chat-sidebar-row").getAttribute("data-active"),
    ).toBe("true");
  });

  it("collapsed mode hides the search/list and exposes minimal actions", () => {
    render(
      <ChatSidebar
        conversations={[mk({ sessionKey: "x" })]}
        activeSessionKey={null}
        onNew={vi.fn()}
        onRename={vi.fn()}
        onTogglePin={vi.fn()}
        onToggleArchive={vi.fn()}
        onDelete={vi.fn()}
        collapsed
        onToggleCollapsed={vi.fn()}
      />,
    );
    expect(screen.getByTestId("chat-sidebar-collapsed")).toBeInTheDocument();
    expect(screen.queryByTestId("chat-sidebar-search")).not.toBeInTheDocument();
  });
});
