import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";

// Mocked BEFORE the page import so the page picks up the vi.fn() fetchers
// rather than the real ones (which would hit the gateway).
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    fetchQqStatus: vi.fn(),
    reconnectQq: vi.fn(),
    updateQqKeywords: vi.fn(),
  };
});

import { fetchQqStatus, type QqStatus } from "@/lib/api";
import QqChannelPage from "./page";

const mockedStatus = vi.mocked(fetchQqStatus);

const STATUS: QqStatus = {
  configured: true,
  enabled: true,
  ws_url: "ws://127.0.0.1:6700",
  self_ids: [10001],
  group_keywords: { "12345": ["help", "bot"] },
  runtime: "connected",
  recent_messages: [],
  health_online: true,
  account_online: true,
  account_qq: 20002,
  config_keys: { self_ids: ["10001"] },
};

function renderWithClient(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

beforeEach(() => {
  mockedStatus.mockResolvedValue(STATUS);
});

describe("QqChannelPage", () => {
  it("renders the channel shell with the QQ title", async () => {
    renderWithClient(<QqChannelPage />);
    expect(
      await screen.findByRole("heading", { name: /qq/i }),
    ).toBeInTheDocument();
    expect(await screen.findByTestId("channel-shell-qq")).toBeInTheDocument();
  });

  it("renders the stats + account panels once status resolves", async () => {
    renderWithClient(<QqChannelPage />);
    expect(await screen.findByTestId("qq-account-panel")).toBeInTheDocument();
  });

  it("shows the detected bot QQ id as a managed read-only field", async () => {
    renderWithClient(<QqChannelPage />);
    const field = await screen.findByTestId("cc-list-self_ids");
    expect(field).toHaveValue("20002");
    expect(field).toHaveAttribute("readonly");
    expect(screen.getByTestId("qq-account-panel")).toHaveTextContent("20002");
  });

  it("does not label a configured fallback as auto-detected", async () => {
    mockedStatus.mockResolvedValueOnce({
      ...STATUS,
      account_qq: null,
      account_online: null,
      self_ids: [10001],
    });
    renderWithClient(<QqChannelPage />);
    const field = await screen.findByTestId("cc-list-self_ids");
    expect(field).toHaveValue("");
    expect(field).toHaveAttribute("placeholder", "登录 QQ 后自动识别");
    expect(screen.getByTestId("qq-account-panel")).not.toHaveTextContent("10001");
  });
});
