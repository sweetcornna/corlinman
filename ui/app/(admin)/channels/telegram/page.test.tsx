import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";

// Mocked BEFORE the module imports below so the page picks up the vi.fn()
// versions rather than the real fetchers (which would hit the gateway).
vi.mock("@/lib/api/telegram", async () => {
  const actual = await vi.importActual<
    typeof import("@/lib/api/telegram")
  >("@/lib/api/telegram");
  return {
    ...actual,
    fetchTelegramStatus: vi.fn(),
    fetchTelegramMessages: vi.fn(),
    sendTelegramTestMessage: vi.fn(),
  };
});

// Stub the underlying apiFetch so the 404-fallback integration test below can
// drive the branch directly from `fetchTelegramStatus` / `fetchTelegramMessages`
// when we call `vi.importActual`-backed copies inside a child describe.
vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>(
    "@/lib/api",
  );
  return {
    ...actual,
    apiFetch: vi.fn(),
  };
});

import {
  fetchTelegramStatus,
  fetchTelegramMessages,
  sendTelegramTestMessage,
  __resetTelegramFallbackLog,
  type TelegramMessage,
  type TelegramStatusResponse,
} from "@/lib/api/telegram";
import TelegramChannelPage from "./page";

const mockedStatus = vi.mocked(fetchTelegramStatus);
const mockedMessages = vi.mocked(fetchTelegramMessages);
const mockedSend = vi.mocked(sendTelegramTestMessage);
const TOKEN = "__INVALID_TEST_TELEGRAM_BOT_TOKEN_SENTINEL__";

const STATUS: TelegramStatusResponse = {
  config: {
    bot_token: TOKEN,
    webhook_url: "https://example.com/tg/webhook",
    secret_token: "secret-token-abcdef",
    drop_pending_updates: true,
  },
  stats: {
    messages_today: 248,
    messages_week: 1867,
    latency_p50_ms: 142,
    latency_p95_ms: 389,
    active_chats: 12,
  },
  connected: true,
  runtime: "connected",
  last_error: null,
  last_webhook_payload: { update_id: 1 },
};

const BASE_MSG: TelegramMessage = {
  id: "m-1",
  kind: "group",
  chat_id: "-100123",
  chat_title: "dev-chat",
  from_username: "@alice",
  content: "help me with the k8s manifest",
  timestamp_ms: Date.parse("2026-04-20T14:32:00Z"),
  reply_deadline_ms: 12_000,
  reply_total_ms: 15_000,
  routing: "responded",
  mention_reason: "mention",
};

const PHOTO_MSG: TelegramMessage = {
  id: "m-photo",
  kind: "private",
  chat_id: "42",
  from_username: "@bob",
  content: "here's the whiteboard shot",
  media: {
    kind: "photo",
    local_path: "/var/cache/tg/photo-m-photo.jpg",
    mime: "image/jpeg",
    size_bytes: 204_800,
  },
  timestamp_ms: Date.parse("2026-04-20T14:28:00Z"),
  routing: "responded",
  mention_reason: "dm",
};

function renderWithClient(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  __resetTelegramFallbackLog();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("TelegramChannelPage", () => {
  it("renders header, stats, and at least one message row", async () => {
    mockedStatus.mockResolvedValue(STATUS);
    mockedMessages.mockResolvedValue([BASE_MSG]);

    renderWithClient(<TelegramChannelPage />);

    expect(
      await screen.findByRole("heading", { name: /telegram channel/i }),
    ).toBeInTheDocument();

    await waitFor(() => {
      expect(screen.getByText(/messages today/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/active chats/i)).toBeInTheDocument();
    expect(screen.getByText(/avg latency/i)).toBeInTheDocument();

    expect(await screen.findByTestId("tg-message-m-1")).toBeInTheDocument();
    // Sender now appears in both the list row AND the hero prose ("last
    // update from @alice …"); loosen to existence.
    expect(screen.getAllByText(/@alice/).length).toBeGreaterThan(0);
    expect(
      screen.getByText(/help me with the k8s manifest/),
    ).toBeInTheDocument();
  });

  it("reveals the full bot token when the reveal button is clicked", async () => {
    mockedStatus.mockResolvedValue(STATUS);
    mockedMessages.mockResolvedValue([BASE_MSG]);

    renderWithClient(<TelegramChannelPage />);

    const tokenEl = await screen.findByTestId("tg-bot-token");
    expect(tokenEl.textContent).not.toContain(TOKEN);
    expect(tokenEl.textContent).toMatch(/•+.{4}$/);

    const revealBtn = screen.getByTestId("tg-reveal-token");
    fireEvent.click(revealBtn);

    await waitFor(() => {
      expect(screen.getByTestId("tg-bot-token").textContent).toBe(TOKEN);
    });
    expect(revealBtn.getAttribute("aria-pressed")).toBe("true");
  });

  // ----- B4-FE1 new coverage -------------------------------------------

  it("renders routing badges for mention / reply / ignored / private rows", async () => {
    const msgs: TelegramMessage[] = [
      { ...BASE_MSG, id: "m-mention", mention_reason: "mention" },
      {
        ...BASE_MSG,
        id: "m-reply",
        mention_reason: "reply_to_bot",
        routing: "responded",
      },
      {
        ...BASE_MSG,
        id: "m-ignored",
        mention_reason: "none",
        routing: "ignored",
      },
      {
        ...BASE_MSG,
        id: "m-private",
        kind: "private",
        mention_reason: "dm",
        chat_title: undefined,
      },
    ];
    mockedStatus.mockResolvedValue(STATUS);
    mockedMessages.mockResolvedValue(msgs);

    renderWithClient(<TelegramChannelPage />);

    expect(
      await screen.findByTestId("tg-route-mention-m-mention"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("tg-route-reply-m-reply")).toBeInTheDocument();
    expect(screen.getByTestId("tg-route-ignored-m-ignored")).toBeInTheDocument();
    expect(screen.getByTestId("tg-route-private-m-private")).toBeInTheDocument();

    // Ignored rows dim to 60% via `opacity-60`.
    const ignoredRow = screen.getByTestId("tg-message-m-ignored");
    expect(ignoredRow.className).toMatch(/opacity-60/);
  });

  it("surfaces a pulse-glow banner when status.last_error is present", async () => {
    mockedStatus.mockResolvedValue({
      ...STATUS,
      last_error: "429 Too Many Requests — retry after 3s",
    });
    mockedMessages.mockResolvedValue([BASE_MSG]);

    renderWithClient(<TelegramChannelPage />);

    const banner = await screen.findByTestId("tg-last-error-banner");
    expect(banner).toHaveAttribute("role", "alert");
    expect(banner.textContent).toMatch(/429 Too Many Requests/);
  });

  it("opens the photo-preview drawer with the expected local path on click", async () => {
    mockedStatus.mockResolvedValue(STATUS);
    mockedMessages.mockResolvedValue([PHOTO_MSG]);

    renderWithClient(<TelegramChannelPage />);

    const thumb = await screen.findByTestId("tg-photo-thumb-m-photo");
    fireEvent.click(thumb);

    const dialog = await screen.findByRole("dialog");
    // Test locale is forced to zh-CN (vitest.setup.ts); the drawer title is
    // i18n'd, so assert the Chinese string.
    expect(within(dialog).getByText("图片预览")).toBeInTheDocument();
    const previewImg = await screen.findByTestId("tg-media-preview-img");
    expect(previewImg.getAttribute("src")).toBe(
      "/var/cache/tg/photo-m-photo.jpg",
    );
    expect(screen.getByTestId("tg-media-path").textContent).toBe(
      "/var/cache/tg/photo-m-photo.jpg",
    );
  });

  it("keeps the send-test Send button disabled until chat_id is non-empty", async () => {
    mockedStatus.mockResolvedValue(STATUS);
    mockedMessages.mockResolvedValue([BASE_MSG]);
    mockedSend.mockResolvedValue({ status: "ok", message_id: 7 });

    renderWithClient(<TelegramChannelPage />);

    fireEvent.click(await screen.findByTestId("tg-send-test-open"));

    const submit = (await screen.findByTestId(
      "tg-send-test-submit",
    )) as HTMLButtonElement;
    expect(submit.disabled).toBe(true);

    const chatIdInput = screen.getByTestId("tg-send-chat-id");
    // Whitespace alone still disabled — the button only flips on trimmed text.
    fireEvent.change(chatIdInput, { target: { value: "   " } });
    expect(submit.disabled).toBe(true);

    fireEvent.change(chatIdInput, { target: { value: "-100123" } });
    await waitFor(() => {
      expect(submit.disabled).toBe(false);
    });
  });

  // The "falls back to the mock when the admin endpoint 404s" case was
  // dropped along with the page-level mock fixtures. The page now shows
  // an empty/disconnected state instead of fake messages on 404 — see
  // `ui/lib/mocks/telegram.ts` (returns `runtime: "disconnected"`).
});
