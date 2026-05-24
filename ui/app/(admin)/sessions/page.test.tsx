/**
 * SessionsPage tests (Phase 4 W2 4-2D).
 *
 * Covers:
 *   - List rendering — rows, message count, formatted timestamp
 *   - Empty state when the API returns zero sessions
 *   - 503 sessions_disabled banner mirrors the W1 4-1B `tenants_disabled` shape
 *   - Replay button opens the dialog (we mock `replaySession` so the dialog
 *     paints synchronously)
 *
 * Mocks the Sessions API client at module scope; mirrors the discipline used
 * by the existing admin page tests under `app/(admin)/.../page.test.tsx`.
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
import { I18nextProvider } from "react-i18next";
import * as React from "react";

import { i18next, initI18n } from "@/lib/i18n";
import type {
  DeleteAllSessionsResult,
  DeleteSessionResult,
  ReplayResult,
  SessionsListResult,
} from "@/lib/api/sessions";

// Sonner ships its own polyfill but jsdom doesn't host the toaster, so stub
// the toast surface — we only need to know whether the success/error path
// ran, not how it visually rendered.
vi.mock("sonner", () => ({
  toast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

// ---------------------------------------------------------------------------
// Module mock — install before importing the page.
// ---------------------------------------------------------------------------

const fetchMock: ReturnType<typeof vi.fn> = vi.fn(
  async (): Promise<SessionsListResult> => {
    throw new Error("fetchMock not configured");
  },
);
const replayMock: ReturnType<typeof vi.fn> = vi.fn(
  async (): Promise<ReplayResult> => {
    throw new Error("replayMock not configured");
  },
);
const deleteMock: ReturnType<typeof vi.fn> = vi.fn(
  async (): Promise<DeleteSessionResult> => {
    throw new Error("deleteMock not configured");
  },
);
const deleteAllMock: ReturnType<typeof vi.fn> = vi.fn(
  async (): Promise<DeleteAllSessionsResult> => {
    throw new Error("deleteAllMock not configured");
  },
);

vi.mock("@/lib/api/sessions", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api/sessions")>(
    "@/lib/api/sessions",
  );
  return {
    ...actual,
    fetchSessions: () => fetchMock(),
    replaySession: (key: string, opts?: { mode?: "transcript" | "rerun" }) =>
      replayMock(key, opts),
    deleteSession: (key: string) => deleteMock(key),
    deleteAllSessions: () => deleteAllMock(),
  };
});

// next/navigation — page itself doesn't use it directly but the dialog +
// breadcrumbs layer might. Stub for safety.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), refresh: vi.fn() }),
  usePathname: () => "/sessions",
  useSearchParams: () => new URLSearchParams(),
}));

import SessionsPage from "./page";

// ---------------------------------------------------------------------------

beforeEach(() => {
  initI18n();
  i18next.changeLanguage("en");
  fetchMock.mockReset();
  replayMock.mockReset();
  deleteMock.mockReset();
  deleteAllMock.mockReset();
});

afterEach(() => {
  cleanup();
});

function Harness({ children }: { children: React.ReactNode }) {
  const [client] = React.useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { retry: false, refetchOnWindowFocus: false },
          mutations: { retry: false },
        },
      }),
  );
  return (
    <QueryClientProvider client={client}>
      <I18nextProvider i18n={i18next}>{children}</I18nextProvider>
    </QueryClientProvider>
  );
}

describe("SessionsPage", () => {
  it("renders rows for each session returned by the API", async () => {
    fetchMock.mockResolvedValueOnce({
      kind: "ok",
      sessions: [
        {
          session_key: "qq:1234",
          last_message_at: 1_777_593_600_000,
          message_count: 12,
        },
        {
          session_key: "telegram:9001",
          last_message_at: 1_777_500_000_000,
          message_count: 6,
        },
      ],
    });

    render(
      <Harness>
        <SessionsPage />
      </Harness>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("session-row-qq:1234")).toBeInTheDocument();
      expect(
        screen.getByTestId("session-row-telegram:9001"),
      ).toBeInTheDocument();
    });
    // Message-count cell renders as a plain number.
    const row = screen.getByTestId("session-row-qq:1234");
    expect(row.textContent).toMatch(/12/);
  });

  it("renders the empty state when the API returns no sessions", async () => {
    fetchMock.mockResolvedValueOnce({ kind: "ok", sessions: [] });

    render(
      <Harness>
        <SessionsPage />
      </Harness>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("sessions-empty")).toBeInTheDocument();
    });
  });

  it("renders the 'session storage is off' banner on 503 sessions_disabled", async () => {
    fetchMock.mockResolvedValueOnce({ kind: "disabled" });

    render(
      <Harness>
        <SessionsPage />
      </Harness>,
    );

    await waitFor(() => {
      expect(
        screen.getByTestId("sessions-disabled-banner"),
      ).toBeInTheDocument();
    });
    expect(screen.getByTestId("sessions-disabled-row")).toBeInTheDocument();
  });

  it("opens the replay dialog when the Replay button is clicked", async () => {
    fetchMock.mockResolvedValueOnce({
      kind: "ok",
      sessions: [
        {
          session_key: "qq:1234",
          last_message_at: 1_777_593_600_000,
          message_count: 1,
        },
      ],
    });
    replayMock.mockResolvedValueOnce({
      kind: "ok",
      replay: {
        session_key: "qq:1234",
        mode: "transcript",
        transcript: [
          { role: "user", content: "hello", ts: "2026-04-30T01:02:03Z" },
        ],
        summary: { message_count: 1, tenant_id: "default" },
      },
    });

    render(
      <Harness>
        <SessionsPage />
      </Harness>,
    );

    const button = await screen.findByTestId("session-replay-qq:1234");
    fireEvent.click(button);
    // Dialog body is rendered into a portal; the breadcrumb is the cheapest
    // marker that the dialog opened.
    await waitFor(() => {
      expect(screen.getByTestId("replay-dialog")).toBeInTheDocument();
    });
    expect(replayMock).toHaveBeenCalledWith("qq:1234", { mode: "transcript" });
  });

  it("renders the load-failed cell when the query rejects", async () => {
    fetchMock.mockRejectedValueOnce(new Error("network down"));

    render(
      <Harness>
        <SessionsPage />
      </Harness>,
    );

    await waitFor(() => {
      expect(screen.getByTestId("sessions-load-failed")).toBeInTheDocument();
    });
  });

  it("renders the operator-friendly empty-state copy when no sessions are returned", async () => {
    fetchMock.mockResolvedValueOnce({ kind: "ok", sessions: [] });

    render(
      <Harness>
        <SessionsPage />
      </Harness>,
    );

    const cell = await screen.findByTestId("sessions-empty");
    expect(cell.textContent ?? "").toMatch(/once you chat with the bot/i);
  });

  it("renders the 'last seen' column when last_seen_at_ms is supplied", async () => {
    fetchMock.mockResolvedValueOnce({
      kind: "ok",
      sessions: [
        {
          session_key: "qq:1234",
          last_message_at: 1_777_500_000_000,
          last_seen_at_ms: 1_777_593_600_000,
          message_count: 3,
        },
      ],
    });

    render(
      <Harness>
        <SessionsPage />
      </Harness>,
    );

    await waitFor(() => {
      expect(
        screen.getByTestId("session-last-seen-qq:1234"),
      ).toBeInTheDocument();
    });
  });

  it("delete button removes the session optimistically on success", async () => {
    fetchMock.mockResolvedValueOnce({
      kind: "ok",
      sessions: [
        {
          session_key: "qq:1234",
          last_message_at: 1_777_593_600_000,
          message_count: 12,
        },
        {
          session_key: "telegram:9001",
          last_message_at: 1_777_500_000_000,
          message_count: 6,
        },
      ],
    });
    deleteMock.mockResolvedValueOnce({ kind: "ok", deleted: 1 });

    render(
      <Harness>
        <SessionsPage />
      </Harness>,
    );

    const delBtn = await screen.findByTestId("session-delete-qq:1234");
    fireEvent.click(delBtn);
    // Confirm dialog opens — confirm it.
    const confirm = await screen.findByTestId(
      "sessions-delete-confirm-confirm",
    );
    fireEvent.click(confirm);

    await waitFor(() => {
      expect(
        screen.queryByTestId("session-row-qq:1234"),
      ).not.toBeInTheDocument();
    });
    expect(screen.getByTestId("session-row-telegram:9001")).toBeInTheDocument();
    expect(deleteMock).toHaveBeenCalledWith("qq:1234");
  });

  it("delete button treats 404 (not_found) as success and removes the row", async () => {
    fetchMock.mockResolvedValueOnce({
      kind: "ok",
      sessions: [
        {
          session_key: "qq:1234",
          last_message_at: 1_777_593_600_000,
          message_count: 12,
        },
      ],
    });
    deleteMock.mockResolvedValueOnce({
      kind: "not_found",
      session_key: "qq:1234",
    });

    render(
      <Harness>
        <SessionsPage />
      </Harness>,
    );

    const delBtn = await screen.findByTestId("session-delete-qq:1234");
    fireEvent.click(delBtn);
    fireEvent.click(
      await screen.findByTestId("sessions-delete-confirm-confirm"),
    );

    await waitFor(() => {
      expect(
        screen.queryByTestId("session-row-qq:1234"),
      ).not.toBeInTheDocument();
    });
  });

  it("delete button restores the row when the backend errors", async () => {
    fetchMock.mockResolvedValueOnce({
      kind: "ok",
      sessions: [
        {
          session_key: "qq:1234",
          last_message_at: 1_777_593_600_000,
          message_count: 12,
        },
      ],
    });
    deleteMock.mockRejectedValueOnce(new Error("network down"));

    render(
      <Harness>
        <SessionsPage />
      </Harness>,
    );

    const delBtn = await screen.findByTestId("session-delete-qq:1234");
    fireEvent.click(delBtn);
    fireEvent.click(
      await screen.findByTestId("sessions-delete-confirm-confirm"),
    );

    // The row should reappear after the optimistic removal is reverted.
    await waitFor(() => {
      expect(screen.getByTestId("session-row-qq:1234")).toBeInTheDocument();
    });
  });

  it("cancelling the delete dialog keeps the row visible", async () => {
    fetchMock.mockResolvedValueOnce({
      kind: "ok",
      sessions: [
        {
          session_key: "qq:1234",
          last_message_at: 1_777_593_600_000,
          message_count: 12,
        },
      ],
    });

    render(
      <Harness>
        <SessionsPage />
      </Harness>,
    );

    const delBtn = await screen.findByTestId("session-delete-qq:1234");
    fireEvent.click(delBtn);
    fireEvent.click(
      await screen.findByTestId("sessions-delete-confirm-cancel"),
    );

    expect(deleteMock).not.toHaveBeenCalled();
    expect(screen.getByTestId("session-row-qq:1234")).toBeInTheDocument();
  });

  it("clear-all button calls deleteAllSessions and empties the list", async () => {
    fetchMock.mockResolvedValueOnce({
      kind: "ok",
      sessions: [
        {
          session_key: "qq:1234",
          last_message_at: 1_777_593_600_000,
          message_count: 12,
        },
        {
          session_key: "telegram:9001",
          last_message_at: 1_777_500_000_000,
          message_count: 6,
        },
      ],
    });
    // After the clear-all the invalidate triggers a refetch — return empty.
    fetchMock.mockResolvedValueOnce({ kind: "ok", sessions: [] });
    deleteAllMock.mockResolvedValueOnce({ kind: "ok", deleted: 2 });

    render(
      <Harness>
        <SessionsPage />
      </Harness>,
    );

    // Wait for the query to resolve so the Clear-all button is enabled.
    await screen.findByTestId("session-row-qq:1234");
    const clearBtn = screen.getByTestId("sessions-clear-all");
    expect(clearBtn).not.toBeDisabled();
    fireEvent.click(clearBtn);
    fireEvent.click(
      await screen.findByTestId("sessions-clear-all-confirm-confirm"),
    );

    await waitFor(() => {
      expect(
        screen.queryByTestId("session-row-qq:1234"),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByTestId("session-row-telegram:9001"),
      ).not.toBeInTheDocument();
    });
    expect(deleteAllMock).toHaveBeenCalledTimes(1);
    // Empty state takes over.
    expect(screen.getByTestId("sessions-empty")).toBeInTheDocument();
  });

  it("clear-all button is disabled while the list is empty", async () => {
    fetchMock.mockResolvedValueOnce({ kind: "ok", sessions: [] });

    render(
      <Harness>
        <SessionsPage />
      </Harness>,
    );

    const clearBtn = await screen.findByTestId("sessions-clear-all");
    expect(clearBtn).toBeDisabled();
  });
});
