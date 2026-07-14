import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
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

// PR5 — the getting-started card reads the shared setup-status caches
// through fetchProviders / fetchModels. Route both through mutable refs so
// individual tests can flip the deployment between unconfigured (default)
// and configured.
const { providersRef, modelsRef } = vi.hoisted(() => ({
  providersRef: { current: [] as unknown[] },
  modelsRef: {
    current: { default: "", aliases: [], providers: [] } as unknown,
  },
}));

const CONFIGURED_PROVIDERS = [
  {
    name: "openai",
    kind: "openai",
    enabled: true,
    base_url: null,
    api_key_source: "env",
    api_key_env_name: "OPENAI_API_KEY",
    params: {},
    params_schema: { type: "object", properties: {} },
  },
];
const CONFIGURED_MODELS = {
  default: "gpt-4o",
  aliases: [
    {
      name: "gpt-4o",
      provider: "openai",
      model: "gpt-4o",
      params: {},
      effective_params_schema: {},
    },
  ],
  providers: [],
};

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
    fetchProviders: vi.fn(async () => providersRef.current),
    fetchModels: vi.fn(async () => modelsRef.current),
  };
});

import DashboardPage from "./page";
import { GETTING_STARTED_DISMISS_KEY } from "@/components/admin/getting-started-card";

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
    window.localStorage.removeItem(GETTING_STARTED_DISMISS_KEY);
    providersRef.current = [];
    modelsRef.current = { default: "", aliases: [], providers: [] };
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

  it("shows the getting-started card with live check states while unconfigured", async () => {
    renderDashboard();

    const card = await screen.findByTestId("getting-started-card");
    expect(card).toBeInTheDocument();
    // All three checklist items pending on a pristine gateway.
    expect(
      screen.getByTestId("getting-started-item-provider"),
    ).toHaveAttribute("data-done", "false");
    expect(screen.getByTestId("getting-started-item-models")).toHaveAttribute(
      "data-done",
      "false",
    );
    expect(screen.getByTestId("getting-started-item-default")).toHaveAttribute(
      "data-done",
      "false",
    );
    // CTA opens the guided flow in a dialog.
    fireEvent.click(screen.getByTestId("getting-started-cta"));
    expect(
      await screen.findByTestId("provider-setup-flow"),
    ).toBeInTheDocument();
  });

  it("hides the getting-started card once the deployment is configured", async () => {
    providersRef.current = CONFIGURED_PROVIDERS;
    modelsRef.current = CONFIGURED_MODELS;
    renderDashboard();

    // Wait for the queries to settle (hero paints), then assert absence.
    await screen.findByText(/cornna/);
    await waitFor(() => {
      expect(screen.queryByTestId("getting-started-card")).toBeNull();
    });
  });

  it("stays hidden after a localStorage dismiss", async () => {
    window.localStorage.setItem(GETTING_STARTED_DISMISS_KEY, "1");
    renderDashboard();

    await screen.findByText(/cornna/);
    expect(screen.queryByTestId("getting-started-card")).toBeNull();
  });

  it("dismiss button hides the card and persists the choice", async () => {
    renderDashboard();

    await screen.findByTestId("getting-started-card");
    fireEvent.click(screen.getByTestId("getting-started-dismiss"));
    expect(screen.queryByTestId("getting-started-card")).toBeNull();
    expect(
      window.localStorage.getItem(GETTING_STARTED_DISMISS_KEY),
    ).toBe("1");
  });
});
