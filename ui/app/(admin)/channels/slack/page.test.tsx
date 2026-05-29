import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";

vi.mock("@/lib/api/slack", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/slack")>("@/lib/api/slack");
  return {
    ...actual,
    fetchSlackStatus: vi.fn(),
    fetchSlackMessages: vi.fn(),
    sendSlackTestMessage: vi.fn(),
  };
});

import {
  fetchSlackStatus,
  fetchSlackMessages,
  type SlackMessage,
  type SlackStatusResponse,
} from "@/lib/api/slack";
import SlackChannelPage from "./page";

const mockedStatus = vi.mocked(fetchSlackStatus);
const mockedMessages = vi.mocked(fetchSlackMessages);

const STATUS: SlackStatusResponse = {
  configured: true,
  enabled: true,
  online: true,
  last_event_at_ms: Date.now() - 8_000,
  received: 9,
  sent: 4,
  errors: 0,
  error_message: null,
  config_keys: {
    allowed_channel_ids: ["C0123ABCD"],
    keyword_filter: [],
    respond_to_all: "True",
  },
};

const MSG: SlackMessage = {
  id: "s-1",
  kind: "group",
  chat_id: "C0123ABCD",
  chat_title: "general",
  from_username: "bob",
  content: "standup in 5",
  timestamp_ms: Date.now() - 7_000,
  routing: "queued",
  mention_reason: "none",
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

describe("SlackChannelPage", () => {
  it("renders the hero, config panel, and a message row", async () => {
    renderWithClient(<SlackChannelPage />);

    expect(
      await screen.findByRole("heading", { name: /slack/i }),
    ).toBeInTheDocument();
    expect(await screen.findByTestId("slack-stream-pill")).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByTestId("slack-config-panel")).toBeInTheDocument();
    });
    expect(screen.getByText("allowed_channel_ids")).toBeInTheDocument();

    expect(await screen.findByTestId("slack-message-s-1")).toBeInTheDocument();
    expect(screen.getByText(/standup in 5/)).toBeInTheDocument();
  });
});
