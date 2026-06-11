/**
 * `<UpdateBubble>` tests (W1.2).
 *
 * Coverage:
 *   - `available=false` → bubble renders nothing
 *   - `available=true` + no dismissed tag → chip with `vX.Y.Z` rendered
 *   - dismissed tag matches `latest` → bubble renders nothing
 *
 * Approach: stub `@/lib/api`'s `fetchSystemInfo` (the only call the
 * bubble makes) via `vi.mock`. localStorage is the real one (jsdom
 * provides a working implementation).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";

import type { UpdateStatus } from "@/lib/api";

// next/link is just a passthrough anchor in the tests — JSDom doesn't run
// the Next router, but the bubble only needs an <a> with the href.
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

// react-i18next stub — minimal translations of the keys the bubble
// uses so tests assert against stable strings without pulling in the
// real i18next runtime.
const STUB_BUNDLE: Record<string, string> = {
  "update.bubble.label": "Update available · {{version}}",
  "update.bubble.dismiss": "Dismiss",
  "update.bubble.tooltip": "Click for release notes",
};
vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, vars?: Record<string, string>) => {
      const template = STUB_BUNDLE[key] ?? key;
      if (!vars) return template;
      return Object.entries(vars).reduce(
        (acc, [k, v]) => acc.replace(`{{${k}}}`, String(v)),
        template,
      );
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
  };
});

import { fetchSystemInfo } from "@/lib/api";
import { DISMISS_KEY, UpdateBubble } from "../update-bubble";

const mockedFetch = vi.mocked(fetchSystemInfo);

function renderBubble() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <UpdateBubble />
    </QueryClientProvider>,
  );
}

function makeStatus(overrides: Partial<UpdateStatus> = {}): UpdateStatus {
  return {
    current: "1.0.0",
    latest: "v1.2.0",
    available: true,
    release_url: "https://example.test/release",
    release_notes_md: "# Notes",
    published_at: 1_700_000_000_000,
    last_checked_at: 1_700_000_000_000,
    prerelease_seen: [],
    ...overrides,
  };
}

beforeEach(() => {
  mockedFetch.mockReset();
  // Clear any leftover dismissed-tag stash between tests.
  window.localStorage.removeItem(DISMISS_KEY);
});

afterEach(() => cleanup());

describe("<UpdateBubble>", () => {
  it("renders nothing when no update is available", async () => {
    mockedFetch.mockResolvedValue(
      makeStatus({ available: false, latest: null }),
    );
    const { container } = renderBubble();
    await waitFor(() => expect(mockedFetch).toHaveBeenCalled());
    expect(
      container.querySelector("[data-testid='update-bubble']"),
    ).toBeNull();
  });

  it("renders the chip with the latest tag when an update is available", async () => {
    mockedFetch.mockResolvedValue(makeStatus({ latest: "v1.2.0" }));
    renderBubble();
    const chip = await screen.findByTestId("update-bubble");
    expect(chip.textContent).toContain("v1.2.0");
    // The chip is an anchor pointing at /system (the updates page) so a
    // click navigates. /admin/* is the API namespace, not a page route.
    expect(chip.getAttribute("href")).toBe("/system");
    // Aria label includes the version for screen-reader users.
    expect(chip.getAttribute("aria-label")).toContain("v1.2.0");
  });

  it("renders nothing when the latest tag is dismissed in localStorage", async () => {
    window.localStorage.setItem(DISMISS_KEY, "v1.2.0");
    mockedFetch.mockResolvedValue(makeStatus({ latest: "v1.2.0" }));
    const { container } = renderBubble();
    await waitFor(() => expect(mockedFetch).toHaveBeenCalled());
    expect(
      container.querySelector("[data-testid='update-bubble']"),
    ).toBeNull();
  });
});
