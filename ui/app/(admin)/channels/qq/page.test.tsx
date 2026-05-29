import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
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
});
