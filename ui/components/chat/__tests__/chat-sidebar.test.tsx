import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";

import { ChatSidebar } from "@/components/chat/chat-sidebar";
import type { ChatConversation } from "@/lib/chat/types";

const MS = 86_400_000;

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
}));

// jsdom has no `matchMedia`; framer-motion then treats reduced-motion as
// false and runs full-duration exit transitions, which would hold the
// AnimatePresence drawer mounted indefinitely under the test renderer.
// Force prefers-reduced-motion so exits collapse to `{ duration: 0 }` and
// the drawer unmounts on the next tick.
beforeEach(() => {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: query.includes("prefers-reduced-motion"),
    media: query,
    onchange: null,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    addListener: vi.fn(),
    removeListener: vi.fn(),
    dispatchEvent: vi.fn(),
  }));
});

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
    // The collapsed rail's inline search is not rendered (only the drawer
    // body carries one, and the drawer is closed by default).
    expect(screen.queryByTestId("chat-sidebar-search")).not.toBeInTheDocument();
  });

  it("opens and closes the mobile off-canvas drawer", async () => {
    render(
      <ChatSidebar
        conversations={[mk({ sessionKey: "x", title: "Drawer Conv" })]}
        activeSessionKey={null}
        onNew={vi.fn()}
        onRename={vi.fn()}
        onTogglePin={vi.fn()}
        onToggleArchive={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    // Drawer hidden by default.
    expect(screen.queryByTestId("chat-sidebar-drawer")).not.toBeInTheDocument();

    // Hamburger trigger opens it.
    fireEvent.click(screen.getByTestId("chat-sidebar-trigger"));
    expect(screen.getByTestId("chat-sidebar-drawer")).toBeInTheDocument();
    expect(screen.getByTestId("chat-sidebar-drawer-panel")).toBeInTheDocument();

    // Clicking the scrim overlay closes it.
    fireEvent.click(screen.getByTestId("chat-sidebar-overlay"));
    await waitFor(() =>
      expect(screen.queryByTestId("chat-sidebar-drawer")).not.toBeInTheDocument(),
    );
  });

  it("closes the drawer on Escape", async () => {
    render(
      <ChatSidebar
        conversations={[mk({ sessionKey: "x" })]}
        activeSessionKey={null}
        onNew={vi.fn()}
        onRename={vi.fn()}
        onTogglePin={vi.fn()}
        onToggleArchive={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByTestId("chat-sidebar-trigger"));
    expect(screen.getByTestId("chat-sidebar-drawer")).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    await waitFor(() =>
      expect(screen.queryByTestId("chat-sidebar-drawer")).not.toBeInTheDocument(),
    );
  });

  it("distinguishes no-search-results from a truly empty list", () => {
    render(
      <ChatSidebar
        conversations={[mk({ sessionKey: "a", title: "Cooking pasta" })]}
        activeSessionKey={null}
        onNew={vi.fn()}
        onRename={vi.fn()}
        onTogglePin={vi.fn()}
        onToggleArchive={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    // Query that matches nothing → "no matching results" + clear affordance.
    fireEvent.change(screen.getByTestId("chat-sidebar-search"), {
      target: { value: "zzzzz" },
    });
    expect(screen.getByText("无匹配结果")).toBeInTheDocument();
    const clear = screen.getByTestId("chat-sidebar-clear-search");
    expect(clear).toBeInTheDocument();

    // Clearing restores the list (no "no conversations" state, rows return).
    fireEvent.click(clear);
    expect(screen.getByText("Cooking pasta")).toBeInTheDocument();
  });

  it("shows the truly-empty copy when there are no conversations at all", () => {
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
    expect(screen.queryByText("无匹配结果")).not.toBeInTheDocument();
  });

  it("keeps the rename input open when onRename rejects", async () => {
    const onRename = vi.fn().mockRejectedValue(new Error("boom"));
    render(
      <ChatSidebar
        conversations={[mk({ sessionKey: "x", title: "Old title" })]}
        activeSessionKey={null}
        onNew={vi.fn()}
        onRename={onRename}
        onTogglePin={vi.fn()}
        onToggleArchive={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    // Enter rename mode.
    fireEvent.click(screen.getByLabelText("重命名"));
    const input = screen.getByTestId("chat-rename-input");
    fireEvent.change(input, { target: { value: "New title" } });

    await act(async () => {
      fireEvent.keyDown(input, { key: "Enter" });
    });

    // Rename was attempted but, because it rejected, the input stays open.
    expect(onRename).toHaveBeenCalledWith("x", "New title");
    expect(screen.getByTestId("chat-rename-input")).toBeInTheDocument();
  });

  it("exits rename mode after a successful onRename", async () => {
    const onRename = vi.fn().mockResolvedValue(undefined);
    render(
      <ChatSidebar
        conversations={[mk({ sessionKey: "x", title: "Old title" })]}
        activeSessionKey={null}
        onNew={vi.fn()}
        onRename={onRename}
        onTogglePin={vi.fn()}
        onToggleArchive={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByLabelText("重命名"));
    const input = screen.getByTestId("chat-rename-input");
    fireEvent.change(input, { target: { value: "New title" } });

    await act(async () => {
      fireEvent.keyDown(input, { key: "Enter" });
    });

    await waitFor(() =>
      expect(screen.queryByTestId("chat-rename-input")).not.toBeInTheDocument(),
    );
    expect(onRename).toHaveBeenCalledTimes(1);
  });

  it("does not double-submit a rename on Enter followed by blur", async () => {
    const onRename = vi.fn().mockResolvedValue(undefined);
    render(
      <ChatSidebar
        conversations={[mk({ sessionKey: "x", title: "Old title" })]}
        activeSessionKey={null}
        onNew={vi.fn()}
        onRename={onRename}
        onTogglePin={vi.fn()}
        onToggleArchive={vi.fn()}
        onDelete={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByLabelText("重命名"));
    const input = screen.getByTestId("chat-rename-input");
    fireEvent.change(input, { target: { value: "New title" } });

    await act(async () => {
      fireEvent.keyDown(input, { key: "Enter" });
      fireEvent.blur(input);
    });

    expect(onRename).toHaveBeenCalledTimes(1);
  });
});
