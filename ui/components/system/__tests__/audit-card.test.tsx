/**
 * Smoke tests for <AuditCard> — 3 cases:
 *  1. Renders rows from the first page with the right columns.
 *  2. "Load more" appears only when next_before_ts is non-null and
 *     appends a second page when clicked.
 *  3. Empty state renders when entries=[].
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";

import { AuditCard } from "../audit-card";
import * as api from "@/lib/api";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (k: string, fallback?: string) => fallback ?? k,
  }),
}));

function makeClient() {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
}

function renderCard() {
  const qc = makeClient();
  return render(
    <QueryClientProvider client={qc}>
      <AuditCard />
    </QueryClientProvider>,
  );
}

describe("AuditCard", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });
  afterEach(() => {
    cleanup();
  });

  it("renders rows from the first page", async () => {
    vi.spyOn(api, "listSystemAudit").mockResolvedValueOnce({
      entries: [
        {
          ts: new Date().toISOString(),
          event: "system.upgrade.completed",
          tag: "v1.2.1",
          actor: "ops",
          details: { before: "1.2.0", after: "1.2.1" },
        },
        {
          ts: new Date(Date.now() - 60_000).toISOString(),
          event: "system.upgrade.requested",
          tag: "v1.2.1",
          actor: "ops",
          details: {},
        },
      ],
      next_before_ts: null,
    });

    renderCard();
    await waitFor(() => {
      expect(screen.getAllByTestId("system-audit-row")).toHaveLength(2);
    });
    expect(screen.getAllByText("v1.2.1")[0]).toBeInTheDocument();
  });

  it("paginates via Load more cursor", async () => {
    const spy = vi.spyOn(api, "listSystemAudit");
    spy.mockResolvedValueOnce({
      entries: [
        {
          ts: new Date().toISOString(),
          event: "system.upgrade.completed",
          tag: "v1.2.1",
          actor: "ops",
          details: {},
        },
      ],
      next_before_ts: "2026-05-01T00:00:00Z",
    });
    spy.mockResolvedValueOnce({
      entries: [
        {
          ts: new Date(Date.now() - 86_400_000).toISOString(),
          event: "system.upgrade.completed",
          tag: "v1.1.1",
          actor: "ops",
          details: {},
        },
      ],
      next_before_ts: null,
    });

    renderCard();
    const loadMore = await screen.findByTestId("system-audit-load-more");
    fireEvent.click(loadMore);
    await waitFor(() => {
      expect(screen.getAllByTestId("system-audit-row")).toHaveLength(2);
    });
    // Cursor exhausted — button gone.
    expect(screen.queryByTestId("system-audit-load-more")).toBeNull();
    expect(spy).toHaveBeenCalledTimes(2);
  });

  it("renders empty state when entries=[]", async () => {
    vi.spyOn(api, "listSystemAudit").mockResolvedValueOnce({
      entries: [],
      next_before_ts: null,
    });
    renderCard();
    await waitFor(() => {
      expect(screen.getByTestId("system-audit-empty")).toBeInTheDocument();
    });
  });
});
