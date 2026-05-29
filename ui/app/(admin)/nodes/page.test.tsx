/**
 * NodesPage honesty tests (R5 honest-align).
 *
 * Background: `/nodes` was wired to poll `fetchRunnersMock()` (which always
 * returns `[]`) every 5s and render the generic "No runners registered" empty
 * state — silently implying a working-but-empty runner registry even though
 * NO backend endpoint exists (there is no GET /wstool/runners in python/).
 *
 * These tests pin the honest end-state:
 *   - the page renders an explicit "not yet available (no backend)" block,
 *     NOT the misleading empty-registry block and NOT the topology viz;
 *   - the dead 5s poll of the empty mock is gone (the mock is never called).
 *
 * Mirrors the harness discipline of the sibling admin page tests under
 * `app/(admin)/.../page.test.tsx`.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { I18nextProvider } from "react-i18next";
import * as React from "react";

import { i18next, initI18n } from "@/lib/i18n";

// Spy on the mock so we can prove the dead poll is gone. We import the actual
// module and replace `fetchRunnersMock` with a tracked stub.
const fetchRunnersSpy = vi.fn(async () => [] as never[]);

vi.mock("@/lib/mocks/nodes", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/mocks/nodes")>(
      "@/lib/mocks/nodes",
    );
  return {
    ...actual,
    fetchRunnersMock: () => fetchRunnersSpy(),
  };
});

import NodesPage from "./page";

beforeEach(() => {
  initI18n();
  i18next.changeLanguage("en");
  fetchRunnersSpy.mockClear();
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

describe("NodesPage honesty", () => {
  it("renders an explicit not-available block, not the misleading empty/topology state", () => {
    render(
      <Harness>
        <NodesPage />
      </Harness>,
    );

    // Honest state must be present.
    expect(
      screen.getByTestId("nodes-not-implemented-block"),
    ).toBeInTheDocument();

    // It must NOT pretend the registry works-but-is-empty, and must NOT
    // render the live topology / side rail viz.
    expect(screen.queryByTestId("nodes-empty-block")).toBeNull();
    expect(screen.queryByTestId("nodes-viz-panel")).toBeNull();
    expect(screen.queryByTestId("nodes-viz-skeleton")).toBeNull();
  });

  it("does not poll the empty mock (the dead 5s poll is removed)", () => {
    render(
      <Harness>
        <NodesPage />
      </Harness>,
    );

    expect(fetchRunnersSpy).not.toHaveBeenCalled();
  });
});
