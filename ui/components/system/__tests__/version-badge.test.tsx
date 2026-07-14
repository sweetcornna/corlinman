/**
 * `<VersionBadge>` tests — the always-visible version chip + dropdown
 * panel that replaced `<UpdateBubble>`.
 *
 * Coverage:
 *   - chip renders `v{current}` even when up to date (no dot)
 *   - update available → amber dot; dismissed tag hides the dot but the
 *     chip stays
 *   - panel: up-to-date state vs update state (priority-ordered)
 *   - "Update now" POSTs the upgrade (no typed_confirmation) and routes
 *     to `/system?upgrade=<id>`
 *   - old import path still works (`UpdateBubble` re-export)
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";

import type { UpdateStatus } from "@/lib/api";

const pushMock = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock, replace: vi.fn(), prefetch: vi.fn() }),
}));

vi.mock("next/link", () => ({
  default: ({
    href,
    children,
    ...rest
  }: {
    href: string;
    children: React.ReactNode;
  } & React.AnchorHTMLAttributes<HTMLAnchorElement>) => (
    <a href={href} {...rest}>
      {children}
    </a>
  ),
}));

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, vars?: Record<string, string>) => {
      if (!vars) return key;
      return `${key}:${Object.values(vars).join(",")}`;
    },
  }),
}));

vi.mock("@/lib/api", async () => {
  const actual = await vi.importActual<typeof import("@/lib/api")>(
    "@/lib/api",
  );
  return {
    ...actual,
    fetchSystemInfo: vi.fn(),
    checkForUpdates: vi.fn(),
    startSystemUpgrade: vi.fn(),
  };
});

import {
  fetchSystemInfo,
  startSystemUpgrade,
} from "@/lib/api";
import { DISMISS_KEY, VersionBadge } from "../version-badge";
import { UpdateBubble } from "../update-bubble";

const mockedFetch = vi.mocked(fetchSystemInfo);
const mockedStart = vi.mocked(startSystemUpgrade);

function renderBadge(Component: React.ComponentType = VersionBadge) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <Component />
    </QueryClientProvider>,
  );
}

function makeStatus(overrides: Partial<UpdateStatus> = {}): UpdateStatus {
  return {
    current: "1.27.0",
    latest: "v1.28.0",
    available: true,
    release_url: "https://example.test/release",
    release_notes_md: "# Notes\nline two",
    published_at: 1_700_000_000_000,
    last_checked_at: 1_700_000_000_000,
    prerelease_seen: [],
    ...overrides,
  };
}

beforeEach(() => {
  mockedFetch.mockReset();
  mockedStart.mockReset();
  pushMock.mockReset();
  window.localStorage.removeItem(DISMISS_KEY);
});

afterEach(() => cleanup());

describe("<VersionBadge>", () => {
  it("renders the current version even when up to date, without a dot", async () => {
    mockedFetch.mockResolvedValue(
      makeStatus({ available: false, latest: null }),
    );
    renderBadge();
    const chip = await screen.findByTestId("version-badge");
    expect(chip.textContent).toContain("v1.27.0");
    expect(screen.queryByTestId("version-badge-dot")).toBeNull();
  });

  it("shows the pulsing dot when an update is available", async () => {
    mockedFetch.mockResolvedValue(makeStatus());
    renderBadge();
    await screen.findByTestId("version-badge");
    expect(screen.getByTestId("version-badge-dot")).toBeTruthy();
  });

  it("hides the dot (but keeps the chip) when the tag is dismissed", async () => {
    window.localStorage.setItem(DISMISS_KEY, "v1.28.0");
    mockedFetch.mockResolvedValue(makeStatus({ latest: "v1.28.0" }));
    renderBadge();
    const chip = await screen.findByTestId("version-badge");
    expect(chip.textContent).toContain("v1.27.0");
    expect(screen.queryByTestId("version-badge-dot")).toBeNull();
  });

  it("opens the panel with the up-to-date state", async () => {
    mockedFetch.mockResolvedValue(
      makeStatus({ available: false, latest: null }),
    );
    renderBadge();
    fireEvent.click(await screen.findByTestId("version-badge"));
    expect(screen.getByTestId("version-badge-panel")).toBeTruthy();
    expect(screen.getByTestId("version-badge-uptodate")).toBeTruthy();
    expect(screen.queryByTestId("version-badge-update")).toBeNull();
  });

  it("opens the panel with the update state and starts the upgrade one-click", async () => {
    mockedFetch.mockResolvedValue(makeStatus({ latest: "v1.28.0" }));
    mockedStart.mockResolvedValue({
      request_id: "req-42",
      state: "queued",
      mode: "docker",
      tag: "v1.28.0",
    });
    renderBadge();
    fireEvent.click(await screen.findByTestId("version-badge"));
    expect(screen.getByTestId("version-badge-update")).toBeTruthy();

    fireEvent.click(screen.getByTestId("version-badge-update-now"));

    await waitFor(() =>
      expect(mockedStart).toHaveBeenCalledWith("v1.28.0"),
    );
    // One-click: no typed_confirmation argument.
    expect(mockedStart.mock.calls[0]?.[1]).toBeUndefined();
    await waitFor(() =>
      expect(pushMock).toHaveBeenCalledWith("/system?upgrade=req-42"),
    );
  });

  it("keeps the legacy UpdateBubble import path working", async () => {
    mockedFetch.mockResolvedValue(
      makeStatus({ available: false, latest: null }),
    );
    renderBadge(UpdateBubble);
    expect(await screen.findByTestId("version-badge")).toBeTruthy();
  });
});
