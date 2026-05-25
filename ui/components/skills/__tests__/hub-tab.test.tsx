/**
 * `<HubTab>` tests (W2.2).
 *
 * Coverage:
 *   - Empty search calls `listHubFeatured(sort)`; non-empty calls
 *     `searchHubSkills` after ~300ms debounce.
 *   - Sort dropdown change re-runs the featured call with the new sort.
 *   - `response.offline === true` → renders banner + Retry; Retry triggers
 *     `query.refetch()` so the api function is called again.
 *
 * react-i18next is stubbed to pass keys through verbatim. Real timers are
 * used so `waitFor` polls work; the debounce is asserted by waiting for
 * the search call to land after the input fires.
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
import * as React from "react";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
}));

vi.mock("@/lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    listHubFeatured: vi.fn(),
    searchHubSkills: vi.fn(),
    getHubSkill: vi.fn(),
    postHubInstall: vi.fn(),
    streamHubInstallEvents: vi.fn(),
  };
});

import { listHubFeatured, searchHubSkills } from "@/lib/api";
import { HubTab } from "../hub-tab";

const mockedFeatured = vi.mocked(listHubFeatured);
const mockedSearch = vi.mocked(searchHubSkills);

function makeSummary(slug: string) {
  return {
    slug,
    name: slug,
    description: `${slug} desc`,
    stars: 12,
    downloads: 34,
    latest_version: "1.0.0",
    updated_at: new Date(Date.now() - 60_000).toISOString(),
  };
}

function renderTab() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <HubTab />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockedFeatured.mockReset();
  mockedSearch.mockReset();
});

afterEach(() => {
  cleanup();
});

describe("<HubTab>", () => {
  it("lists featured trending by default and re-runs on sort change", async () => {
    mockedFeatured.mockResolvedValue({
      rows: [makeSummary("alpha")],
      offline: false,
      next_cursor: null,
    });

    renderTab();

    await waitFor(() =>
      expect(screen.getByTestId("hub-skill-card-alpha")).toBeInTheDocument(),
    );
    expect(mockedFeatured.mock.calls[0]?.[0]).toBe("trending");

    // Sort change → call featured again with the new key.
    mockedFeatured.mockResolvedValue({
      rows: [makeSummary("beta")],
      offline: false,
      next_cursor: null,
    });
    fireEvent.change(screen.getByTestId("hub-sort-select"), {
      target: { value: "stars" },
    });

    await waitFor(() => {
      const stars = mockedFeatured.mock.calls.find(
        (call) => call[0] === "stars",
      );
      expect(stars).toBeTruthy();
    });
    await waitFor(() =>
      expect(screen.getByTestId("hub-skill-card-beta")).toBeInTheDocument(),
    );
  });

  it("debounces the search input by ~300ms before calling searchHubSkills", async () => {
    mockedFeatured.mockResolvedValue({
      rows: [],
      offline: false,
      next_cursor: null,
    });
    mockedSearch.mockResolvedValue({
      rows: [makeSummary("hit")],
      offline: false,
    });

    renderTab();

    // Let the initial featured call settle.
    await waitFor(() =>
      expect(mockedFeatured.mock.calls.length).toBeGreaterThanOrEqual(1),
    );

    const input = screen.getByTestId("hub-search-input");
    fireEvent.change(input, { target: { value: "ide" } });
    // Search should not fire immediately — the debounce blocks it.
    expect(mockedSearch).not.toHaveBeenCalled();

    // After ~300ms the search fires.
    await waitFor(
      () => expect(mockedSearch).toHaveBeenCalled(),
      { timeout: 1000 },
    );
    expect(mockedSearch.mock.calls[0]?.[0]).toBe("ide");
    // The hit card lands once data resolves.
    await waitFor(() =>
      expect(screen.getByTestId("hub-skill-card-hit")).toBeInTheDocument(),
    );
  });

  it("renders the offline banner and retries on click", async () => {
    mockedFeatured.mockResolvedValueOnce({
      rows: [],
      offline: true,
      next_cursor: null,
    });

    renderTab();

    await waitFor(() =>
      expect(screen.getByTestId("hub-offline-banner")).toBeInTheDocument(),
    );

    // Retry → second call to listHubFeatured, this time online with data.
    mockedFeatured.mockResolvedValueOnce({
      rows: [makeSummary("gamma")],
      offline: false,
      next_cursor: null,
    });

    fireEvent.click(screen.getByTestId("hub-offline-retry"));

    await waitFor(() =>
      expect(screen.getByTestId("hub-skill-card-gamma")).toBeInTheDocument(),
    );
    expect(mockedFeatured.mock.calls.length).toBeGreaterThanOrEqual(2);
  });
});
