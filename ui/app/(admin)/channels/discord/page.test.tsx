import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";

// Mocked BEFORE the page import so the page picks up the vi.fn() fetchers
// rather than the real ones (which would hit the gateway).
vi.mock("@/lib/api/discord", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/discord")>(
      "@/lib/api/discord",
    );
  return {
    ...actual,
    fetchDiscordStatus: vi.fn(),
    fetchDiscordMessages: vi.fn(),
    sendDiscordTestMessage: vi.fn(),
  };
});

import {
  fetchDiscordStatus,
  fetchDiscordMessages,
  type DiscordMessage,
  type DiscordStatusResponse,
} from "@/lib/api/discord";
import DiscordChannelPage from "./page";

const mockedStatus = vi.mocked(fetchDiscordStatus);
const mockedMessages = vi.mocked(fetchDiscordMessages);

const STATUS: DiscordStatusResponse = {
  configured: true,
  enabled: true,
  online: true,
  last_event_at_ms: Date.now() - 5_000,
  received: 42,
  sent: 17,
  errors: 0,
  error_message: null,
  config_keys: {
    allowed_channel_ids: ["111", "222"],
    keyword_filter: ["deploy"],
    respond_to_all: "False",
  },
};

const MSG: DiscordMessage = {
  id: "d-1",
  kind: "mention",
  chat_id: "111",
  chat_title: "ops",
  from_username: "alice",
  content: "ship it?",
  timestamp_ms: Date.now() - 4_000,
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

describe("DiscordChannelPage", () => {
  it("renders the hero title, stat row, config panel, and a message row", async () => {
    renderWithClient(<DiscordChannelPage />);

    expect(
      await screen.findByRole("heading", { name: /discord/i }),
    ).toBeInTheDocument();
    expect(await screen.findByTestId("discord-stream-pill")).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByTestId("discord-config-panel")).toBeInTheDocument();
    });
    // The non-secret config keys are surfaced in the config panel.
    expect(screen.getByText("allowed_channel_ids")).toBeInTheDocument();

    expect(await screen.findByTestId("discord-message-d-1")).toBeInTheDocument();
    expect(screen.getByText(/ship it\?/)).toBeInTheDocument();
  });

  it("surfaces the error banner when status.error_message is present", async () => {
    mockedStatus.mockResolvedValue({
      ...STATUS,
      online: false,
      errors: 3,
      error_message: "gateway disconnected (1011)",
    });
    renderWithClient(<DiscordChannelPage />);

    const banner = await screen.findByTestId("discord-last-error-banner");
    expect(banner).toHaveAttribute("role", "alert");
    expect(banner.textContent).toMatch(/gateway disconnected/);
  });
});
