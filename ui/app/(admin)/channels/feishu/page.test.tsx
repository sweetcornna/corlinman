import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";

vi.mock("@/lib/api/feishu", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/feishu")>(
      "@/lib/api/feishu",
    );
  return {
    ...actual,
    fetchFeishuStatus: vi.fn(),
    fetchFeishuMessages: vi.fn(),
    sendFeishuTestMessage: vi.fn(),
  };
});

import {
  fetchFeishuStatus,
  fetchFeishuMessages,
  type FeishuMessage,
  type FeishuStatusResponse,
} from "@/lib/api/feishu";
import FeishuChannelPage from "./page";

const mockedStatus = vi.mocked(fetchFeishuStatus);
const mockedMessages = vi.mocked(fetchFeishuMessages);

const STATUS: FeishuStatusResponse = {
  configured: true,
  enabled: true,
  online: true,
  last_event_at_ms: Date.now() - 3_000,
  received: 5,
  sent: 2,
  errors: 1,
  error_message: null,
  config_keys: {
    app_id: "cli_abc123",
    allowed_chat_ids: ["oc_xyz"],
    keyword_filter: ["help"],
    respond_to_all: "False",
  },
};

const MSG: FeishuMessage = {
  id: "f-1",
  kind: "mention",
  chat_id: "oc_xyz",
  chat_title: "dev",
  from_username: "carol",
  content: "need a review",
  timestamp_ms: Date.now() - 2_000,
  routing: "responded",
  mention_reason: "mention",
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
  mockedMessages.mockResolvedValue([MSG]);
});

describe("FeishuChannelPage", () => {
  it("renders the hero, config panel with app_id, and a message row", async () => {
    renderWithClient(<FeishuChannelPage />);

    expect(await screen.findByTestId("feishu-stream-pill")).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByTestId("feishu-config-panel")).toBeInTheDocument();
    });
    expect(screen.getByText("app_id")).toBeInTheDocument();
    expect(screen.getByText("cli_abc123")).toBeInTheDocument();

    expect(await screen.findByTestId("feishu-message-f-1")).toBeInTheDocument();
    expect(screen.getByText(/need a review/)).toBeInTheDocument();
  });
});
