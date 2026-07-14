/**
 * /models — "Models & Keys" hub composition tests (PR4 model-hub
 * consolidation).
 *
 * The section components are mocked; these tests only cover the thin
 * composition itself: ?tab= resolution, active-tab-only mounting, tab
 * clicks driving router.replace, and the onCustomProvidersChanged →
 * credentials invalidation wiring that used to live on /credentials.
 */

import * as React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { replaceMock, searchParamsRef } = vi.hoisted(() => ({
  replaceMock: vi.fn(),
  searchParamsRef: { current: new URLSearchParams() },
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({
    push: vi.fn(),
    replace: replaceMock,
    refresh: vi.fn(),
    back: vi.fn(),
    forward: vi.fn(),
    prefetch: vi.fn(),
  }),
  usePathname: () => "/models",
  useSearchParams: () => searchParamsRef.current,
  useParams: () => ({}),
}));

vi.mock("@/components/model-hub/providers-admin-content", () => ({
  ProvidersAdminContent: ({
    onCustomProvidersChanged,
  }: {
    onCustomProvidersChanged?: () => void;
  }) => (
    <button
      type="button"
      data-testid="mock-providers-admin-content"
      onClick={() => onCustomProvidersChanged?.()}
    >
      providers admin
    </button>
  ),
}));
vi.mock("@/components/model-hub/oauth-panel", () => ({
  OAuthPanel: () => <div data-testid="mock-oauth-panel" />,
}));
vi.mock("@/components/model-hub/routing-section", () => ({
  RoutingSection: () => <div data-testid="mock-routing-section" />,
}));
vi.mock("@/components/model-hub/credentials-advanced", () => ({
  CredentialsAdvanced: () => <div data-testid="mock-credentials-advanced" />,
}));

import ModelsPage from "./page";

let queryClient: QueryClient;

function renderPage() {
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={queryClient}>
      <ModelsPage />
    </QueryClientProvider>,
  );
}

describe("ModelsPage (Models & Keys hub)", () => {
  beforeEach(() => {
    replaceMock.mockClear();
    searchParamsRef.current = new URLSearchParams();
  });

  afterEach(() => {
    cleanup();
  });

  it("defaults to the providers tab (content + oauth panel, others unmounted)", () => {
    renderPage();
    expect(
      screen.getByTestId("mock-providers-admin-content"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("mock-oauth-panel")).toBeInTheDocument();
    expect(screen.queryByTestId("mock-routing-section")).toBeNull();
    expect(screen.queryByTestId("mock-credentials-advanced")).toBeNull();
    expect(screen.getByTestId("model-hub-tab-providers")).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("mounts only the routing section when ?tab=routing", () => {
    searchParamsRef.current = new URLSearchParams("tab=routing");
    renderPage();
    expect(screen.getByTestId("mock-routing-section")).toBeInTheDocument();
    expect(screen.queryByTestId("mock-providers-admin-content")).toBeNull();
    expect(screen.queryByTestId("mock-credentials-advanced")).toBeNull();
  });

  it("mounts only the advanced section when ?tab=advanced", () => {
    searchParamsRef.current = new URLSearchParams("tab=advanced");
    renderPage();
    expect(screen.getByTestId("mock-credentials-advanced")).toBeInTheDocument();
    expect(screen.queryByTestId("mock-providers-admin-content")).toBeNull();
    expect(screen.queryByTestId("mock-routing-section")).toBeNull();
  });

  it("falls back to the providers tab for an unknown ?tab value", () => {
    searchParamsRef.current = new URLSearchParams("tab=bogus");
    renderPage();
    expect(
      screen.getByTestId("mock-providers-admin-content"),
    ).toBeInTheDocument();
  });

  it("clicking a tab replaces the URL with the ?tab deep link", () => {
    renderPage();
    fireEvent.click(screen.getByTestId("model-hub-tab-routing"));
    expect(replaceMock).toHaveBeenCalledWith("/models?tab=routing", {
      scroll: false,
    });
    fireEvent.click(screen.getByTestId("model-hub-tab-advanced"));
    expect(replaceMock).toHaveBeenCalledWith("/models?tab=advanced", {
      scroll: false,
    });
  });

  it("invalidates the credentials cache when custom providers change", () => {
    renderPage();
    const spy = vi.spyOn(queryClient, "invalidateQueries");
    fireEvent.click(screen.getByTestId("mock-providers-admin-content"));
    expect(spy).toHaveBeenCalledWith({ queryKey: ["admin", "credentials"] });
  });
});
