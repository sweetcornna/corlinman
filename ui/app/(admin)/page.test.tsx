import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { I18nextProvider } from "react-i18next";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { i18next, initI18n } from "@/lib/i18n";
import { AdminSessionProvider } from "@/components/admin/admin-session-context";

vi.mock("framer-motion", async () => {
  const React = await vi.importActual<typeof import("react")>("react");
  type MotionStubProps = React.HTMLAttributes<HTMLElement> & {
    children?: React.ReactNode;
    variants?: unknown;
    initial?: unknown;
    animate?: unknown;
    whileHover?: unknown;
    whileTap?: unknown;
    transition?: unknown;
  };
  const make = (tag: string) => {
    const Component = ({
      children,
      variants: _variants,
      initial: _initial,
      animate: _animate,
      whileHover: _whileHover,
      whileTap: _whileTap,
      transition: _transition,
      ...props
    }: MotionStubProps) =>
      React.createElement(tag, props, children);
    Component.displayName = `motion.${tag}`;
    return Component;
  };
  return {
    motion: {
      div: make("div"),
      section: make("section"),
    },
  };
});

vi.mock("@/components/cmdk-palette", () => ({
  useCommandPalette: () => ({ open: false, setOpen: vi.fn(), toggle: vi.fn() }),
}));

vi.mock("@/lib/motion", () => ({
  useMotionVariants: () => ({ fadeUp: {}, liquidStagger: {}, liquidRise: {} }),
}));

vi.mock("@/lib/sse", () => ({
  openEventStream: vi.fn(() => vi.fn()),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    apiFetch: vi.fn(async (path: string) => {
      if (path === "/admin/plugins") {
        return [{ name: "core", status: "loaded" }];
      }
      if (path === "/admin/agents") {
        return [{ name: "assistant", file_path: "assistant.md", bytes: 12, last_modified: "2026-06-13T00:00:00Z" }];
      }
      throw new Error(`unexpected apiFetch path: ${path}`);
    }),
    fetchRagStats: vi.fn(async () => ({ ready: true, files: 1, chunks: 42, tags: 2 })),
    fetchHealth: vi.fn(async () => ({
      checks: [
        { name: "gateway", ok: true },
        { name: "providers", ok: true },
      ],
    })),
    listPendingApprovals: vi.fn(async () => []),
  };
});

import DashboardPage from "./page";

function renderDashboard() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
      mutations: { retry: false },
    },
  });
  return render(
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18next}>
        <AdminSessionProvider
          session={{
            user: "cornna",
            created_at: "2026-06-13T00:00:00Z",
            expires_at: "2026-06-14T00:00:00Z",
          }}
        >
          <DashboardPage />
        </AdminSessionProvider>
      </I18nextProvider>
    </QueryClientProvider>,
  );
}

describe("DashboardPage", () => {
  beforeEach(() => {
    initI18n();
    void i18next.changeLanguage("zh-CN");
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("greets the authenticated admin instead of the old hard-coded name", async () => {
    renderDashboard();

    expect(await screen.findByText(/cornna/)).toBeInTheDocument();
    expect(screen.queryByText(/Ian/)).not.toBeInTheDocument();
  });

  it("keeps dashboard hero and status copy concise", async () => {
    renderDashboard();

    await screen.findByText(/cornna/);
    const copy = document.body.textContent ?? "";
    expect(copy).not.toContain("Ian");
    expect(copy).not.toContain("发布 2 天前");
    expect(copy).not.toContain("含超时工具调用");
    expect(copy).not.toContain("等你判断");
  });
});
