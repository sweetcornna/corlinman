import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";

vi.mock("@/lib/api/wechat_official", async () => {
  const actual = await vi.importActual<
    typeof import("@/lib/api/wechat_official")
  >("@/lib/api/wechat_official");
  return {
    ...actual,
    fetchWechatOfficialStatus: vi.fn(),
  };
});

import {
  fetchWechatOfficialStatus,
  type WechatOfficialStatusResponse,
} from "@/lib/api/wechat_official";
import WechatOfficialChannelPage from "./page";

const mockedStatus = vi.mocked(fetchWechatOfficialStatus);

const STATUS: WechatOfficialStatusResponse = {
  configured: true,
  enabled: true,
  online: false,
  last_event_at_ms: null,
  error_message: null,
  config_keys: { app_id: "wx_app_001" },
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

describe("WechatOfficialChannelPage", () => {
  it("renders the channel shell, hero, and config panel with app_id", async () => {
    renderWithClient(<WechatOfficialChannelPage />);

    expect(
      await screen.findByTestId("channel-shell-wechat_official"),
    ).toBeInTheDocument();
    expect(await screen.findByTestId("wechat_official-hero")).toBeInTheDocument();

    await waitFor(() => {
      expect(
        screen.getByTestId("wechat_official-config-panel"),
      ).toBeInTheDocument();
    });
    expect(screen.getByText("app_id")).toBeInTheDocument();
    expect(screen.getByText("wx_app_001")).toBeInTheDocument();
  });
});
