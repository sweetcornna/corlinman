/**
 * `<McpDetailDrawer>` tests — env-collection gate around Install.
 *
 * Coverage:
 *   - Fetches detail on open and renders one input per `requires_env` key.
 *   - Install is disabled until every required env value is filled in.
 *   - Clicking Install POSTs `installMcpServer` with the collected `env`
 *     map (and the slug).
 *
 * react-i18next is stubbed to pass keys through verbatim; the api functions
 * are mocked so no real network is hit.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, vars?: Record<string, unknown>) =>
      vars ? `${key}:${JSON.stringify(vars)}` : key,
  }),
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock("@/lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    getMcpMarketItem: vi.fn(),
    installMcpServer: vi.fn(),
  };
});

import { getMcpMarketItem, installMcpServer, type McpMarketItem } from "@/lib/api";
import { McpDetailDrawer } from "../mcp-detail-drawer";

const mockedDetail = vi.mocked(getMcpMarketItem);
const mockedInstall = vi.mocked(installMcpServer);

function makeItem(overrides: Partial<McpMarketItem> = {}): McpMarketItem {
  return {
    slug: "github",
    name: "GitHub MCP",
    description: "GitHub tools over MCP",
    latest_version: "1.2.0",
    emoji: "🐙",
    transport: "stdio",
    stars: 100,
    downloads: 200,
    updated_at: new Date(Date.now() - 60_000).toISOString(),
    tags: ["git", "vcs"],
    requires_env: ["GITHUB_TOKEN", "GITHUB_ORG"],
    ...overrides,
  };
}

function renderDrawer(item: McpMarketItem) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <McpDetailDrawer item={item} open onOpenChange={() => {}} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockedDetail.mockReset();
  mockedInstall.mockReset();
});

afterEach(() => {
  cleanup();
});

describe("<McpDetailDrawer>", () => {
  it("renders one input per required env var and gates install until filled", async () => {
    const item = makeItem();
    mockedDetail.mockResolvedValue(item);

    renderDrawer(item);

    // env inputs render from requires_env once the detail resolves.
    await waitFor(() =>
      expect(screen.getByTestId("mcp-detail-env-GITHUB_TOKEN")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("mcp-detail-env-GITHUB_ORG")).toBeInTheDocument();

    // Install is disabled while env values are empty.
    const installBtn = screen.getByTestId("mcp-detail-install");
    expect(installBtn).toBeDisabled();

    // Fill only one → still disabled.
    fireEvent.change(screen.getByTestId("mcp-detail-env-GITHUB_TOKEN"), {
      target: { value: "tok_123" },
    });
    expect(installBtn).toBeDisabled();

    // Fill the second → enabled.
    fireEvent.change(screen.getByTestId("mcp-detail-env-GITHUB_ORG"), {
      target: { value: "acme" },
    });
    await waitFor(() => expect(installBtn).not.toBeDisabled());
  });

  it("POSTs installMcpServer with the collected env map", async () => {
    const item = makeItem();
    mockedDetail.mockResolvedValue(item);
    mockedInstall.mockResolvedValue({
      name: "github",
      source: "market",
      version: "1.2.0",
      enabled: false,
      transport: "stdio",
      status: "stopped",
      tools: 0,
      error: null,
      installed_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });

    renderDrawer(item);

    await waitFor(() =>
      expect(screen.getByTestId("mcp-detail-env-GITHUB_TOKEN")).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByTestId("mcp-detail-env-GITHUB_TOKEN"), {
      target: { value: "tok_123" },
    });
    fireEvent.change(screen.getByTestId("mcp-detail-env-GITHUB_ORG"), {
      target: { value: "acme" },
    });

    const installBtn = screen.getByTestId("mcp-detail-install");
    await waitFor(() => expect(installBtn).not.toBeDisabled());
    fireEvent.click(installBtn);

    await waitFor(() => expect(mockedInstall).toHaveBeenCalledTimes(1));
    expect(mockedInstall.mock.calls[0]?.[0]).toEqual({
      slug: "github",
      env: { GITHUB_TOKEN: "tok_123", GITHUB_ORG: "acme" },
    });
  });

  it("installs immediately when no env is required", async () => {
    const item = makeItem({ requires_env: [] });
    mockedDetail.mockResolvedValue(item);
    mockedInstall.mockResolvedValue({
      name: "github",
      source: "market",
      version: "1.2.0",
      enabled: false,
      transport: "stdio",
      status: "stopped",
      tools: 0,
      error: null,
      installed_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });

    renderDrawer(item);

    const installBtn = screen.getByTestId("mcp-detail-install");
    // No env form renders.
    expect(screen.queryByTestId("mcp-detail-env-form")).not.toBeInTheDocument();
    // Enabled once the detail resolves (button needs `detail`).
    await waitFor(() => expect(installBtn).not.toBeDisabled());
    fireEvent.click(installBtn);

    await waitFor(() => expect(mockedInstall).toHaveBeenCalledTimes(1));
    // No env key sent when requires_env is empty.
    expect(mockedInstall.mock.calls[0]?.[0]).toEqual({
      slug: "github",
      env: undefined,
    });
  });
});
