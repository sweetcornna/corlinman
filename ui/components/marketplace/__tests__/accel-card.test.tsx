/**
 * `<AccelCard>` tests — read-only settings render + probe button.
 *
 * Coverage:
 *   - Renders the current mode/preset/enabled + accelerated index URL once
 *     the settings query resolves.
 *   - Clicking "Test acceleration" POSTs `testMarketplaceAccel` and renders
 *     both ProbeLeg result cards (direct vs accelerated).
 *
 * react-i18next is stubbed to pass keys through verbatim; the api functions
 * are mocked so no real network is hit.
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
  useTranslation: () => ({
    t: (key: string, vars?: Record<string, unknown>) =>
      vars ? `${key}:${JSON.stringify(vars)}` : key,
  }),
}));

vi.mock("sonner", () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock("@/lib/api", async () => {
  const actual =
    await vi.importActual<typeof import("@/lib/api")>("@/lib/api");
  return {
    ...actual,
    getMarketplaceSettings: vi.fn(),
    testMarketplaceAccel: vi.fn(),
  };
});

import {
  getMarketplaceSettings,
  testMarketplaceAccel,
  type MarketplaceSettings,
} from "@/lib/api";
import { AccelCard } from "../accel-card";

const mockedSettings = vi.mocked(getMarketplaceSettings);
const mockedTest = vi.mocked(testMarketplaceAccel);

function makeSettings(): MarketplaceSettings {
  return {
    registry_repo: "openclaw/registry",
    registry_ref: "main",
    default_source: "github",
    clawhub_enabled: true,
    github_token_set: true,
    index_url: "https://raw.githubusercontent.com/openclaw/registry/main/index.json",
    accelerated_index_url:
      "https://ghproxy.example/raw/openclaw/registry/main/index.json",
    accel: {
      mode: "auto",
      preset: "ghproxy",
      base: "https://ghproxy.example",
      mirror_host: "",
      assume_region: "CN",
      enabled: true,
    },
  };
}

function renderCard() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <AccelCard />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  mockedSettings.mockReset();
  mockedTest.mockReset();
});

afterEach(() => {
  cleanup();
});

describe("<AccelCard>", () => {
  it("renders mode/preset/enabled + accelerated index URL", async () => {
    mockedSettings.mockResolvedValue(makeSettings());

    renderCard();

    await waitFor(() =>
      expect(screen.getByTestId("accel-card")).toBeInTheDocument(),
    );
    expect(screen.getByTestId("accel-mode")).toHaveTextContent("auto");
    expect(screen.getByTestId("accel-preset")).toHaveTextContent("ghproxy");
    expect(screen.getByTestId("accel-index-url")).toHaveTextContent(
      "ghproxy.example",
    );
  });

  it("runs the probe and renders both direct + accelerated legs", async () => {
    mockedSettings.mockResolvedValue(makeSettings());
    mockedTest.mockResolvedValue({
      enabled: true,
      direct: {
        url: "https://raw.githubusercontent.com/openclaw/registry/main/index.json",
        ok: true,
        status: 200,
        ms: 412,
        error: null,
      },
      accelerated: {
        url: "https://ghproxy.example/raw/openclaw/registry/main/index.json",
        ok: false,
        status: 503,
        ms: null,
        error: "upstream 503",
      },
    });

    renderCard();

    await waitFor(() =>
      expect(screen.getByTestId("accel-test-button")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByTestId("accel-test-button"));

    await waitFor(() =>
      expect(screen.getByTestId("accel-test-results")).toBeInTheDocument(),
    );
    expect(mockedTest).toHaveBeenCalledTimes(1);

    const direct = screen.getByTestId("accel-probe-direct");
    const accelerated = screen.getByTestId("accel-probe-accelerated");
    expect(direct).toHaveAttribute("data-ok", "true");
    expect(accelerated).toHaveAttribute("data-ok", "false");
    expect(accelerated).toHaveTextContent("upstream 503");
  });

  it("renders the offline block when settings fail to load", async () => {
    mockedSettings.mockRejectedValue(new Error("boom"));

    renderCard();

    await waitFor(() =>
      expect(screen.getByTestId("accel-card-offline")).toBeInTheDocument(),
    );
  });
});
