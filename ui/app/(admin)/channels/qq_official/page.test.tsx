import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";

vi.mock("@/lib/api/qq_official", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api/qq_official")>(
      "@/lib/api/qq_official",
    );
  return {
    ...actual,
    fetchQqOfficialStatus: vi.fn(),
  };
});

import {
  fetchQqOfficialStatus,
  type QqOfficialStatusResponse,
} from "@/lib/api/qq_official";
import QqOfficialChannelPage from "./page";

const mockedStatus = vi.mocked(fetchQqOfficialStatus);

const STATUS: QqOfficialStatusResponse = {
  configured: true,
  enabled: true,
  online: false,
  last_event_at_ms: null,
  error_message: null,
  config_keys: {
    app_id: "qq_app_777",
    intents: ["GUILD_MESSAGES", "DIRECT_MESSAGE"],
    sandbox: "True",
  },
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

describe("QqOfficialChannelPage", () => {
  it("renders the channel shell, hero, and config panel with app_id + intents", async () => {
    renderWithClient(<QqOfficialChannelPage />);

    expect(
      await screen.findByTestId("channel-shell-qq_official"),
    ).toBeInTheDocument();
    expect(await screen.findByTestId("qq_official-hero")).toBeInTheDocument();

    await waitFor(() => {
      expect(
        screen.getByTestId("qq_official-config-panel"),
      ).toBeInTheDocument();
    });
    expect(screen.getByText("app_id")).toBeInTheDocument();
    expect(screen.getByText("qq_app_777")).toBeInTheDocument();
    expect(screen.getByText("intents")).toBeInTheDocument();
    expect(screen.getByText("GUILD_MESSAGES")).toBeInTheDocument();
  });
});
